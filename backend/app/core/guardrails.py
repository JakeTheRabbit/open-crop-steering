"""Bounded action validator — the Phase 9 guardrail decision tree.

This is the safety-critical heart of bounded auto-adjust (plan locked
decisions #13, #14). When the supervisor resolves a proposed change to
``bounded_auto_adjust`` mode, the change passes through
:func:`validate_all`, which decides — deterministically — whether the AI
may auto-apply it.

The decision tree (plan locked decision #13, evaluated *in this order*)
-----------------------------------------------------------------------
1. **Class E** — the AI never writes admin/control state. Reject outright.
2. **Saturation + anti-pattern** — the change trips an anti-pattern
   coupled to a fired ``SAT-*`` indicator (e.g. lowering temp while the
   AC is saturated = ``SAT-AC`` + ``AP-02``). Reject; ``reason_codes``
   carries both ids; activate the cool-down.
3. **Anti-pattern (uncoupled)** — the change trips any other
   anti-pattern. Reject; activate the cool-down.
4. **Coupling rule** — the change violates an ``EC-*`` coupling rule.
   Reject; ``reason_codes`` carries the ``EC-*`` id; activate the
   cool-down.
5. **Absolute bounds** — the change would move the parameter outside its
   risk class's absolute bounds. Reject (``guardrail_rejection``);
   activate the cool-down.
6. **Cool-down / no-touch** — the parameter is inside a cool-down window,
   or ``now`` is inside a no-touch window. Defer (no cool-down change).
7. **Cumulative cap** — the change would push the rolling 24h or 7d sum
   for the parameter past its cap. Reject; activate the cool-down.
8. **In the allowed action set**, within bounds + caps, clean of AP/EC →
   **apply** (``controlled_adjustment``).
9. **Not in the allowed action set** but within bounds + caps + clean →
   **apply with ``novel_proposal=true``** and **halve** the remaining
   cumulative budget for that parameter/room.

State writes
------------
* On **apply** / **apply_novel** — the parameter's
  :class:`~app.models.cumulative_delta.CumulativeDelta` row has the
  applied delta added to both the 24h and 7d sums.
* On **reject** — the parameter's cool-down (``cooldown_until`` /
  ``cooldown_reason``) is set. The default cool-down is 60 minutes,
  120 minutes for Class D (tighter — plan locked decision #14).
* **Defer** writes no cool-down (a deferral is not a rejection).

Pattern detection
-----------------
:func:`detect_rejection_pattern` implements the plan's
formal-deviation criterion: >= 3 ``guardrail_rejection`` events citing
the *same* ``AP-*`` id for one room within the last hour is escalated to
a ``formal_deviation`` event + audit row (it suggests AI drift — the
model is repeatedly attempting a known-bad action).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func, select

from app.core.anti_patterns import check_anti_patterns
from app.core.audit import log_audit
from app.core.coupling_rules import RoomConfig, check_coupling
from app.core.rollout import ParamClass, class_for_param
from app.models.audit_event import AuditEvent, AuditEventType
from app.models.cumulative_delta import CumulativeDelta
from app.models.event_log import EventLogEntry, EventSeverity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.schemas.llm_decision import ProposedChange

log = structlog.get_logger(__name__)

#: Worker identity stamped on guardrail-written audit / event rows.
_ACTOR = "supervisor"

#: How far back :func:`detect_rejection_pattern` looks for repeated
#: same-AP rejections (plan formal-deviation criterion: 1 hour).
PATTERN_WINDOW = dt.timedelta(hours=1)

#: Number of same-AP ``guardrail_rejection`` events within
#: :data:`PATTERN_WINDOW` that escalates to a ``formal_deviation``.
PATTERN_THRESHOLD = 3

#: Float tolerance for matching a free-form proposal delta against an
#: allowed-action delta (the action-set deltas come from tolerance
#: bands, so an exact float equality is brittle).
_DELTA_MATCH_EPSILON = 1e-6


# ---------------------------------------------------------------------------
# Parameter risk classes + per-class bounds / caps / cool-downs.
# Public defaults — a facility tightens these in its private overrides
# (plan: guardrails_overrides.py). Re-exported PARAM_CLASSES is the same
# mapping app.core.rollout owns, surfaced here so the validator module is
# a one-stop import for the guardrail-config surface.
# ---------------------------------------------------------------------------

#: Re-export of the canonical param -> risk-class map (plan decision #11).
#: ``class_for_param`` (also re-exported) is the safe accessor — an
#: unknown parameter resolves to Class A (report-only, never written).
from app.core.rollout import PARAM_CLASSES  # noqa: E402,F401 — re-export


@dataclass(frozen=True, slots=True)
class ClassGuardrail:
    """Per-risk-class guardrail bounds, cumulative caps and cool-down.

    Attributes:
        param_class: The :class:`~app.core.rollout.ParamClass` these
            bounds apply to.
        max_cumulative_delta_24h: Cap on the absolute rolling 24h sum of
            applied deltas for any one parameter in the class.
        max_cumulative_delta_7d: Cap on the absolute rolling 7d sum.
        cooldown: Cool-down applied after a guardrail rejection.
    """

    param_class: ParamClass
    max_cumulative_delta_24h: float
    max_cumulative_delta_7d: float
    cooldown: dt.timedelta


#: Default cool-down after a guardrail rejection (plan locked
#: decision #14: 60 min default).
DEFAULT_COOLDOWN = dt.timedelta(minutes=60)

#: Class D cool-down — nutrient/chemistry changes get the tighter
#: 120-minute cool-down (plan locked decision #14).
CLASS_D_COOLDOWN = dt.timedelta(minutes=120)


#: Per-class guardrail config. Class A / E never auto-apply, but a
#: defensive entry keeps every class addressable. The 24h / 7d caps are
#: conservative public floors; a facility tightens them.
CLASS_GUARDRAILS: dict[ParamClass, ClassGuardrail] = {
    ParamClass.a: ClassGuardrail(
        ParamClass.a, 0.0, 0.0, DEFAULT_COOLDOWN
    ),
    ParamClass.b: ClassGuardrail(
        ParamClass.b, 2.0, 5.0, DEFAULT_COOLDOWN
    ),
    ParamClass.c: ClassGuardrail(
        ParamClass.c, 5.0, 12.0, DEFAULT_COOLDOWN
    ),
    ParamClass.d: ClassGuardrail(
        ParamClass.d, 0.6, 1.5, CLASS_D_COOLDOWN
    ),
    ParamClass.e: ClassGuardrail(
        ParamClass.e, 0.0, 0.0, DEFAULT_COOLDOWN
    ),
}


@dataclass(frozen=True, slots=True)
class ParamBounds:
    """Absolute lower / upper bound for a single parameter's setpoint.

    A proposal whose *resulting* value falls outside ``[lo, hi]`` is
    rejected (``guardrail_rejection``) — these are the hard bounds the AI
    can never cross, however small the step (plan locked decision #13).

    Attributes:
        lo: Inclusive lower bound on the resulting setpoint value.
        hi: Inclusive upper bound on the resulting setpoint value.
    """

    lo: float
    hi: float

    def contains(self, value: float) -> bool:
        """Return whether ``value`` is within ``[lo, hi]`` inclusive."""
        return self.lo <= value <= self.hi


#: Absolute per-parameter bounds (public defaults — a facility tightens
#: them). Bounds are on the *resulting* setpoint value, not the delta.
#: A parameter absent here has no absolute-bound check (only its class
#: caps + the AP / EC checks apply).
PARAM_ABSOLUTE_BOUNDS: dict[str, ParamBounds] = {
    # Class B — environment.
    "temp": ParamBounds(15.0, 35.0),
    "temp_day": ParamBounds(15.0, 35.0),
    "temp_night": ParamBounds(12.0, 32.0),
    "rh": ParamBounds(30.0, 80.0),
    "rh_day": ParamBounds(30.0, 80.0),
    "rh_night": ParamBounds(30.0, 80.0),
    "co2": ParamBounds(400.0, 1600.0),
    "co2_day": ParamBounds(400.0, 1600.0),
    "vpd_day": ParamBounds(0.4, 1.8),
    "ppfd": ParamBounds(100.0, 1200.0),
    "ppfd_day": ParamBounds(100.0, 1200.0),
    "air_velocity": ParamBounds(0.0, 1.5),
    "photoperiod_hours": ParamBounds(0.0, 24.0),
    # Class C — irrigation.
    "vwc_target": ParamBounds(20.0, 80.0),
    "ec_target": ParamBounds(0.5, 5.0),
    "dryback_pct": ParamBounds(5.0, 65.0),
    "shot_size": ParamBounds(0.5, 10.0),
    "drain_pct": ParamBounds(0.0, 40.0),
    "irrigation_freq": ParamBounds(1.0, 48.0),
    # Class D — nutrient / chemistry.
    "tank_ph": ParamBounds(5.0, 7.0),
    "tank_ec": ParamBounds(0.5, 4.0),
    "leaf_temp": ParamBounds(18.0, 32.0),
}


# ---------------------------------------------------------------------------
# The guardrail decision.
# ---------------------------------------------------------------------------

#: Outcome literal values for :class:`GuardrailDecision`.
OUTCOME_APPLY = "apply"
OUTCOME_APPLY_NOVEL = "apply_novel"
OUTCOME_REJECT = "reject"
OUTCOME_DEFER = "defer"


@dataclass(slots=True)
class GuardrailDecision:
    """The validator's verdict on one proposed change.

    Attributes:
        outcome: One of :data:`OUTCOME_APPLY`, :data:`OUTCOME_APPLY_NOVEL`,
            :data:`OUTCOME_REJECT`, :data:`OUTCOME_DEFER`.
        reason_codes: Machine-actionable ids supporting the verdict —
            ``SAT-*`` / ``AP-*`` / ``EC-*`` on a rejection, a structural
            tag (``out_of_bounds``, ``cumulative_cap``, ``cooldown``,
            ``no_touch``, ``class_e``) otherwise. Empty on a clean apply.
        detail: Human-readable explanation for logs / audit / the
            operator-facing rejection.
        param_name: The parameter the decision concerns.
        applied_delta: The delta that was (or would be) applied — set on
            apply / apply_novel, ``0.0`` otherwise.
        novel_proposal: ``True`` on :data:`OUTCOME_APPLY_NOVEL` — the
            change was outside the allowed action set; its cumulative
            budget was halved.
        cooldown_until: When a cool-down was activated by a rejection,
            the instant it expires; ``None`` on apply / defer.
    """

    outcome: str
    param_name: str
    detail: str
    reason_codes: list[str] = field(default_factory=list)
    applied_delta: float = 0.0
    novel_proposal: bool = False
    cooldown_until: dt.datetime | None = None

    @property
    def applied(self) -> bool:
        """Return whether the decision resulted in an applied change."""
        return self.outcome in (OUTCOME_APPLY, OUTCOME_APPLY_NOVEL)

    @property
    def rejected(self) -> bool:
        """Return whether the decision rejected the change."""
        return self.outcome == OUTCOME_REJECT

    @property
    def deferred(self) -> bool:
        """Return whether the decision deferred the change."""
        return self.outcome == OUTCOME_DEFER


# ---------------------------------------------------------------------------
# cumulative_delta row helpers.
# ---------------------------------------------------------------------------


async def _get_or_create_cumulative(
    session: AsyncSession, room_id: str, param_name: str
) -> CumulativeDelta:
    """Return the ``cumulative_delta`` row for ``(room, param)``, creating it.

    A parameter that has never been adjusted has no row; the first call
    creates a zero-sum row so the validator always has somewhere to
    record state.

    Args:
        session: Active async session.
        room_id: Room the parameter belongs to.
        param_name: Parameter name.

    Returns:
        The :class:`~app.models.cumulative_delta.CumulativeDelta` row.
    """
    result = await session.execute(
        select(CumulativeDelta).where(
            CumulativeDelta.room_id == room_id,
            CumulativeDelta.param_name == param_name,
        )
    )
    row = result.scalar_one_or_none()
    if row is not None:
        return row
    row = CumulativeDelta(
        room_id=room_id,
        param_name=param_name,
        sum_delta_24h=0.0,
        sum_delta_7d=0.0,
    )
    session.add(row)
    await session.flush()
    return row


def _cooldown_active(row: CumulativeDelta, *, now: dt.datetime) -> bool:
    """Return whether a parameter's cool-down window is still open."""
    return row.cooldown_until is not None and row.cooldown_until > now


def _budget_remaining(
    row: CumulativeDelta, guardrail: ClassGuardrail, delta: float
) -> tuple[float, float]:
    """Return remaining 24h / 7d cumulative budget *in the delta's direction*.

    Budget is one-directional: a positive delta consumes positive-side
    headroom, a negative delta negative-side. The returned headroom is
    the absolute room left before the cap is breached if ``delta`` is
    applied. A negative result means the cap would be exceeded.

    Args:
        row: The parameter's cumulative-delta row.
        guardrail: The parameter class's guardrail config.
        delta: The signed delta being considered.

    Returns:
        ``(headroom_24h, headroom_7d)`` — absolute headroom in the
        direction of ``delta``.
    """
    sign = 1.0 if delta >= 0 else -1.0
    # Resulting sums if the delta is applied.
    next_24h = row.sum_delta_24h + delta
    next_7d = row.sum_delta_7d + delta
    # Headroom is how far the resulting sum is inside the cap, measured
    # in the delta's direction.
    headroom_24h = guardrail.max_cumulative_delta_24h - abs(next_24h)
    headroom_7d = guardrail.max_cumulative_delta_7d - abs(next_7d)
    # Direction sign keeps a same-direction-as-delta semantics for the
    # caller; magnitude is what matters for the cap test.
    return sign * headroom_24h, sign * headroom_7d


def _would_breach_cap(
    row: CumulativeDelta, guardrail: ClassGuardrail, delta: float
) -> bool:
    """Return whether applying ``delta`` breaches the 24h or 7d cap."""
    next_24h = abs(row.sum_delta_24h + delta)
    next_7d = abs(row.sum_delta_7d + delta)
    return (
        next_24h > guardrail.max_cumulative_delta_24h
        or next_7d > guardrail.max_cumulative_delta_7d
    )


def _halved_budget_breached(
    row: CumulativeDelta, guardrail: ClassGuardrail, delta: float
) -> bool:
    """Return whether ``delta`` breaches the *halved* cumulative cap.

    A novel proposal (outside the allowed action set) gets only half the
    remaining cumulative budget (plan locked decision #13) — this checks
    the resulting sum against the halved caps.
    """
    next_24h = abs(row.sum_delta_24h + delta)
    next_7d = abs(row.sum_delta_7d + delta)
    return (
        next_24h > guardrail.max_cumulative_delta_24h / 2.0
        or next_7d > guardrail.max_cumulative_delta_7d / 2.0
    )


# ---------------------------------------------------------------------------
# Helpers over the proposed change + allowed action set.
# ---------------------------------------------------------------------------


def _resulting_value(change: ProposedChange, snapshot: Any) -> float | None:
    """Return the setpoint value the change would produce, if computable.

    Reads the parameter's current value from ``snapshot.payload['setpoints']``
    and adds the change delta. ``None`` when the snapshot does not carry
    a current value for the parameter (no absolute-bound check is then
    possible).
    """
    payload: dict[str, Any] = getattr(snapshot, "payload", {}) or {}
    setpoints: dict[str, Any] = payload.get("setpoints", {}) or {}
    current = setpoints.get(change.param_name)
    if not isinstance(current, (int, float)):
        return None
    return float(current) + change.delta


def _change_in_action_set(
    change: ProposedChange, allowed_action_set: list[Any] | None
) -> bool:
    """Return whether a free-form change matches an allowed action.

    A change is "in the set" when the action set contains an action for
    the same parameter and direction whose delta matches the change's
    delta (within a small tolerance — the action set's deltas are
    derived from tolerance bands, so an exact float match is brittle).

    Args:
        change: The proposed change.
        allowed_action_set: The deterministic action set
            (:class:`~app.core.action_set.Action` objects), or ``None`` /
            empty.

    Returns:
        ``True`` iff a matching action exists in the set.
    """
    if not allowed_action_set:
        return False
    for action in allowed_action_set:
        if getattr(action, "param_name", None) != change.param_name:
            continue
        action_delta = getattr(action, "delta", None)
        if action_delta is None:
            continue
        if abs(float(action_delta) - change.delta) <= _DELTA_MATCH_EPSILON:
            return True
    return False


# ---------------------------------------------------------------------------
# Cool-down activation.
# ---------------------------------------------------------------------------


def _activate_cooldown(
    row: CumulativeDelta,
    param_class: ParamClass,
    reason: str,
    *,
    now: dt.datetime,
) -> dt.datetime:
    """Set the cool-down on a cumulative-delta row after a rejection.

    The cool-down duration is the parameter class's configured cool-down
    (60 min default, 120 min for Class D — plan locked decision #14).

    Args:
        row: The parameter's cumulative-delta row.
        param_class: The parameter's risk class.
        reason: Short cool-down reason (stored on the row).
        now: The decision time.

    Returns:
        The instant the cool-down expires.
    """
    guardrail = CLASS_GUARDRAILS[param_class]
    until = now + guardrail.cooldown
    row.cooldown_until = until
    row.cooldown_reason = reason[:128]
    return until


# ---------------------------------------------------------------------------
# The validator.
# ---------------------------------------------------------------------------


async def validate_all(  # noqa: PLR0911, PLR0915 — the locked-decision-#13 tree
    session: AsyncSession,
    *,
    change: ProposedChange,
    snapshot: Any,
    room_config: RoomConfig | dict[str, Any] | None,
    allowed_action_set: list[Any] | None,
    room_id: str | None = None,
    now: dt.datetime | None = None,
    no_touch: bool = False,
) -> GuardrailDecision:
    """Run the full Phase-9 guardrail decision tree for one proposed change.

    Implements plan locked decision #13's tree *exactly* — see the
    module docstring for the ordered steps. The function is the single
    gate between an AI proposal and an auto-applied control change.

    On a terminal verdict it also writes the parameter's
    :class:`~app.models.cumulative_delta.CumulativeDelta` state:

    * **apply / apply_novel** — the applied delta is added to the 24h +
      7d rolling sums.
    * **reject** — the cool-down (``cooldown_until`` / ``cooldown_reason``)
      is set.
    * **defer** — no state write.

    Args:
        session: Active async session — the ``cumulative_delta`` row is
            read / created / updated (the caller commits).
        change: The free-form :class:`~app.schemas.llm_decision.ProposedChange`
            the LLM proposed.
        snapshot: The :class:`~app.models.sensor_snapshot.SensorSnapshot`
            (or compatible double) the proposal was reasoned about —
            source of equipment-saturation flags + current setpoints.
        room_config: The room's static equipment / coupling map
            (:class:`~app.core.coupling_rules.RoomConfig` / mapping /
            ``None``).
        allowed_action_set: The deterministic ``allowed_action_set``
            (:class:`~app.core.action_set.Action` objects). A change that
            matches one is "in the set"; one that does not — but is
            otherwise clean and in-bounds — is applied as a *novel*
            proposal with a halved budget.
        room_id: The room id. Defaults to the snapshot's ``room_id``
            payload field; required (one of the two) for the
            ``cumulative_delta`` lookup.
        now: The decision time (injectable for tests; defaults to UTC
            now).
        no_touch: ``True`` if the tick time falls inside a no-touch
            window — the validator then defers (plan locked decision
            #14).

    Returns:
        The :class:`GuardrailDecision` verdict.

    Raises:
        ValueError: If neither ``room_id`` nor a snapshot ``room_id`` is
            available (the cumulative-delta row cannot be keyed).
    """
    now = now or dt.datetime.now(dt.UTC)
    payload: dict[str, Any] = getattr(snapshot, "payload", {}) or {}
    resolved_room = room_id or payload.get("room_id")
    if not resolved_room:
        raise ValueError(
            "validate_all needs a room_id (argument or snapshot payload)"
        )

    param_class = class_for_param(change.param_name)
    delta = change.delta

    # --- Step 1: Class E — the AI never writes admin/control state. ----
    if param_class is ParamClass.e:
        log.warning(
            "guardrail_class_e_refused",
            room_id=resolved_room,
            param_name=change.param_name,
        )
        row = await _get_or_create_cumulative(
            session, resolved_room, change.param_name
        )
        until = _activate_cooldown(row, param_class, "class_e", now=now)
        return GuardrailDecision(
            outcome=OUTCOME_REJECT,
            param_name=change.param_name,
            reason_codes=["class_e"],
            detail=(
                f"{change.param_name} is a Class-E (admin/control-state) "
                "parameter — the AI never writes it."
            ),
            cooldown_until=until,
        )

    row = await _get_or_create_cumulative(
        session, resolved_room, change.param_name
    )

    # --- Steps 2 + 3: anti-pattern checks (saturation-coupled first). --
    ap_hits = check_anti_patterns(change, snapshot)
    if ap_hits:
        hit = ap_hits[0]
        until = _activate_cooldown(
            row, param_class, hit.ap_id, now=now
        )
        log.warning(
            "guardrail_anti_pattern_reject",
            room_id=resolved_room,
            param_name=change.param_name,
            reason_codes=hit.reason_codes,
        )
        return GuardrailDecision(
            outcome=OUTCOME_REJECT,
            param_name=change.param_name,
            reason_codes=list(hit.reason_codes),
            detail=hit.reason,
            cooldown_until=until,
        )

    # --- Step 4: coupling-rule checks. ---------------------------------
    ec_violations = check_coupling(change, snapshot, room_config)
    if ec_violations:
        violation = ec_violations[0]
        until = _activate_cooldown(
            row, param_class, violation.ec_id, now=now
        )
        log.warning(
            "guardrail_coupling_reject",
            room_id=resolved_room,
            param_name=change.param_name,
            reason_codes=violation.reason_codes,
        )
        return GuardrailDecision(
            outcome=OUTCOME_REJECT,
            param_name=change.param_name,
            reason_codes=list(violation.reason_codes),
            detail=violation.reason,
            cooldown_until=until,
        )

    # --- Step 5: absolute bounds. --------------------------------------
    bounds = PARAM_ABSOLUTE_BOUNDS.get(change.param_name)
    resulting = _resulting_value(change, snapshot)
    if bounds is not None and resulting is not None and not bounds.contains(
        resulting
    ):
        until = _activate_cooldown(
            row, param_class, "out_of_bounds", now=now
        )
        log.warning(
            "guardrail_out_of_bounds_reject",
            room_id=resolved_room,
            param_name=change.param_name,
            resulting=resulting,
        )
        return GuardrailDecision(
            outcome=OUTCOME_REJECT,
            param_name=change.param_name,
            reason_codes=["out_of_bounds"],
            detail=(
                f"Proposed change to {change.param_name} ({delta:+g}) "
                f"yields {resulting:g}, outside the absolute bounds "
                f"[{bounds.lo:g}, {bounds.hi:g}]."
            ),
            cooldown_until=until,
        )

    # --- Step 6: cool-down / no-touch -> defer. ------------------------
    if _cooldown_active(row, now=now):
        log.info(
            "guardrail_cooldown_defer",
            room_id=resolved_room,
            param_name=change.param_name,
            cooldown_until=row.cooldown_until.isoformat()
            if row.cooldown_until
            else None,
        )
        return GuardrailDecision(
            outcome=OUTCOME_DEFER,
            param_name=change.param_name,
            reason_codes=["cooldown"],
            detail=(
                f"{change.param_name} is in a cool-down window until "
                f"{row.cooldown_until.isoformat() if row.cooldown_until else '?'}"
                f" (reason: {row.cooldown_reason or 'n/a'})."
            ),
        )
    if no_touch:
        log.info(
            "guardrail_no_touch_defer",
            room_id=resolved_room,
            param_name=change.param_name,
        )
        return GuardrailDecision(
            outcome=OUTCOME_DEFER,
            param_name=change.param_name,
            reason_codes=["no_touch"],
            detail=(
                f"Tick is inside a no-touch window — {change.param_name} "
                "change deferred."
            ),
        )

    # --- Step 7: cumulative cap. ---------------------------------------
    guardrail = CLASS_GUARDRAILS[param_class]
    if _would_breach_cap(row, guardrail, delta):
        until = _activate_cooldown(
            row, param_class, "cumulative_cap", now=now
        )
        log.warning(
            "guardrail_cumulative_cap_reject",
            room_id=resolved_room,
            param_name=change.param_name,
            sum_24h=row.sum_delta_24h,
            sum_7d=row.sum_delta_7d,
            delta=delta,
        )
        return GuardrailDecision(
            outcome=OUTCOME_REJECT,
            param_name=change.param_name,
            reason_codes=["cumulative_cap"],
            detail=(
                f"Proposed change to {change.param_name} ({delta:+g}) "
                f"would push the rolling sum past its cap "
                f"(24h cap {guardrail.max_cumulative_delta_24h:g}, "
                f"7d cap {guardrail.max_cumulative_delta_7d:g}; "
                f"current 24h={row.sum_delta_24h:g}, 7d={row.sum_delta_7d:g})."
            ),
            cooldown_until=until,
        )

    # --- Steps 8 + 9: apply (in-set) vs apply_novel (out-of-set). ------
    in_set = _change_in_action_set(change, allowed_action_set)
    if in_set:
        row.sum_delta_24h += delta
        row.sum_delta_7d += delta
        row.last_updated_at = now
        await session.flush()
        log.info(
            "guardrail_apply",
            room_id=resolved_room,
            param_name=change.param_name,
            delta=delta,
        )
        return GuardrailDecision(
            outcome=OUTCOME_APPLY,
            param_name=change.param_name,
            reason_codes=[],
            detail=(
                f"Change to {change.param_name} ({delta:+g}) is in the "
                "allowed action set, within bounds and caps, and clean of "
                "anti-patterns / coupling violations — applied."
            ),
            applied_delta=delta,
        )

    # Novel proposal: not in the allowed action set. Apply only if it
    # also fits the *halved* cumulative budget (plan locked decision
    # #13 — halve the remaining budget for a novel proposal).
    if _halved_budget_breached(row, guardrail, delta):
        until = _activate_cooldown(
            row, param_class, "cumulative_cap", now=now
        )
        log.warning(
            "guardrail_novel_halved_budget_reject",
            room_id=resolved_room,
            param_name=change.param_name,
            delta=delta,
        )
        return GuardrailDecision(
            outcome=OUTCOME_REJECT,
            param_name=change.param_name,
            reason_codes=["cumulative_cap"],
            detail=(
                f"Novel proposal for {change.param_name} ({delta:+g}) "
                "exceeds the halved cumulative budget a non-allowed-set "
                "proposal is limited to."
            ),
            cooldown_until=until,
        )

    row.sum_delta_24h += delta
    row.sum_delta_7d += delta
    row.last_updated_at = now
    await session.flush()
    log.info(
        "guardrail_apply_novel",
        room_id=resolved_room,
        param_name=change.param_name,
        delta=delta,
    )
    return GuardrailDecision(
        outcome=OUTCOME_APPLY_NOVEL,
        param_name=change.param_name,
        reason_codes=["novel_proposal"],
        detail=(
            f"Change to {change.param_name} ({delta:+g}) is NOT in the "
            "allowed action set but is within bounds, within the halved "
            "novel-proposal budget, and clean of anti-patterns / coupling "
            "violations — applied as a novel proposal."
        ),
        applied_delta=delta,
        novel_proposal=True,
    )


# ---------------------------------------------------------------------------
# Pattern detection — repeated same-AP rejections -> formal_deviation.
# ---------------------------------------------------------------------------


async def detect_rejection_pattern(
    session: AsyncSession, room_id: str, ap_id: str
) -> bool:
    """Escalate >= 3 same-``AP-*`` rejections in 1h to a ``formal_deviation``.

    Plan formal-deviation criterion: ">= 3 guardrail rejections same
    param 1h (pattern indicating drift or AI runaway)". A run of
    rejections all citing the *same* anti-pattern id for one room means
    the model keeps trying a known-bad action — model drift or a context
    misunderstanding — and is escalated.

    The function counts ``guardrail_rejection`` :class:`AuditEvent` rows
    for ``room_id`` whose ``reason_codes`` contains ``ap_id`` within the
    last :data:`PATTERN_WINDOW`. At / above :data:`PATTERN_THRESHOLD` it
    writes a ``formal_deviation`` :class:`EventLogEntry` *and* a
    ``formal_deviation`` audit row, then returns ``True``.

    Idempotency: if an *unacknowledged* ``formal_deviation`` event for
    this room + ``ap_id`` already exists, no duplicate is written (the
    one open deviation already covers the pattern); the function still
    returns ``True``.

    Args:
        session: Active async session.
        room_id: The room whose rejection history is examined.
        ap_id: The anti-pattern id the rejection cited (``None`` /
            non-``AP-*`` ids are ignored — only anti-pattern runs
            escalate).

    Returns:
        ``True`` if a formal deviation is now open for this pattern
        (whether just escalated or already open); ``False`` otherwise.
    """
    if not ap_id or not ap_id.startswith("AP-"):
        return False

    now = dt.datetime.now(dt.UTC)
    since = now - PATTERN_WINDOW

    # Count guardrail_rejection audit rows for the room citing this AP id
    # within the window. reason_codes is JSONB; ``.contains`` does a
    # JSONB containment match on the array.
    count = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.event_type == AuditEventType.guardrail_rejection,
            AuditEvent.room_id == room_id,
            AuditEvent.occurred_at >= since,
            AuditEvent.reason_codes.contains([ap_id]),
        )
    )
    rejection_count = int(count or 0)
    if rejection_count < PATTERN_THRESHOLD:
        return False

    # Already an open (unacknowledged) deviation for this pattern? Don't
    # duplicate — one open deviation covers the run.
    existing = await session.scalar(
        select(func.count())
        .select_from(EventLogEntry)
        .where(
            EventLogEntry.event_type == AuditEventType.formal_deviation,
            EventLogEntry.room_id == room_id,
            EventLogEntry.acknowledged_at.is_(None),
            EventLogEntry.reason_codes.contains([ap_id]),
        )
    )
    if int(existing or 0) > 0:
        log.info(
            "guardrail_pattern_already_open",
            room_id=room_id,
            ap_id=ap_id,
        )
        return True

    summary = (
        f"Formal deviation: {rejection_count} guardrail rejections citing "
        f"{ap_id} for room {room_id} within {int(PATTERN_WINDOW.total_seconds() // 3600)}h "
        "— possible AI drift / runaway (repeated known-bad proposal)."
    )
    audit = await log_audit(
        session,
        event_type=AuditEventType.formal_deviation,
        actor_id=_ACTOR,
        room_id=room_id,
        summary=summary,
        params={
            "pattern": "repeated_anti_pattern_rejection",
            "ap_id": ap_id,
            "rejection_count": rejection_count,
            "window_hours": int(PATTERN_WINDOW.total_seconds() // 3600),
        },
        reason_codes=[ap_id, "formal_deviation"],
    )
    session.add(
        EventLogEntry(
            event_type=AuditEventType.formal_deviation,
            severity=EventSeverity.critical,
            room_id=room_id,
            summary=summary,
            payload={
                "pattern": "repeated_anti_pattern_rejection",
                "ap_id": ap_id,
                "rejection_count": rejection_count,
            },
            reason_codes=[ap_id, "formal_deviation"],
            audit_event_id=audit.id,
        )
    )
    await session.flush()
    log.warning(
        "guardrail_formal_deviation_escalated",
        room_id=room_id,
        ap_id=ap_id,
        rejection_count=rejection_count,
    )
    return True
