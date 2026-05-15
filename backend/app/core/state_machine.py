"""Per-room health state machine — HEALTHY / IMPAIRED / CRITICAL.

Phase 6 is deterministic only: this machine reads sensor patterns
(equipment saturation, tolerance drift, sensor staleness) and decides a
room's health state. There is no AI here — the supervisor (Phase 7+)
*consumes* the state to decide whether to even tick the LLM for a room.

State semantics
---------------
* **HEALTHY** — equipment within capacity, sensors fresh, parameters in
  band (or only mildly drifted). The supervisor may run the LLM.
* **IMPAIRED** — a warning-grade problem: an equipment item is saturated
  (``SAT-AC`` / ``SAT-DEHU`` / ``SAT-CO2`` / ``SAT-FAN`` / ``SAT-PRESS``),
  one or more sensors are stale, or a tolerance band is breached. The
  supervisor suppresses LLM ticks for the room (plan risk #14) — acting
  on a degraded room would be acting on bad inputs.
* **CRITICAL** — a critical-grade problem: ``SAT-IRRIG`` (irrigation
  unresponsive) or ``SAT-DEW`` (active condensation). These imply crop /
  process harm; later phases must block the relevant proposal classes
  and the room needs human attention now.

Transition table (:func:`next_state` is a pure function of its inputs —
the *current* state does not change the result; the inputs fully
determine the next state):

==================== ================ =============== ==========
any critical sat?    any warning sat? stale / breach? next state
==================== ================ =============== ==========
yes                  *                *               CRITICAL
no                   yes              *                IMPAIRED
no                   no               yes             IMPAIRED
no                   no               no              HEALTHY
==================== ================ =============== ==========

``current`` is still passed in (and surfaced on :class:`RoomEvaluation`
as ``previous``) so :func:`evaluate_room` can detect *edges* — a
recovery (``-> HEALTHY``) versus a fresh degradation — and log the right
event-taxonomy row for the transition.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from app.core.audit import log_audit
from app.core.equipment import RoomContext, SaturationResult, evaluate_all
from app.core.tolerance import ToleranceBreach, check_room_tolerances
from app.models.audit_event import AuditEventType
from app.models.event_log import EventLogEntry, EventSeverity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.equipment import _InfluxLike

log = structlog.get_logger(__name__)

#: Worker identity in the audit / event trail.
_ACTOR = "monitor"


class RoomState(enum.StrEnum):
    """Health state of a single cultivation room."""

    healthy = "healthy"
    impaired = "impaired"
    critical = "critical"


@dataclass(slots=True)
class RoomEvaluation:
    """The full result of evaluating one room.

    Attributes:
        room_id: Room evaluated.
        day_index: Cycle day the evaluation ran for.
        previous: The room's state before this evaluation.
        state: The room's state after this evaluation.
        saturation_results: Every :class:`SaturationResult` (all seven
            indicators, including unevaluated ones).
        tolerance_breaches: Out-of-band parameters found.
        stale_sensors: Sensor entity tags flagged stale by the caller.
        reason_codes: Distinct ``SAT-*`` / drift codes that drove the
            state — spliced into the event/audit rows.
    """

    room_id: str
    day_index: int
    previous: RoomState
    state: RoomState
    saturation_results: list[SaturationResult] = field(default_factory=list)
    tolerance_breaches: list[ToleranceBreach] = field(default_factory=list)
    stale_sensors: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """``True`` iff the evaluation moved the room to a new state."""
        return self.state is not self.previous

    @property
    def saturated(self) -> list[SaturationResult]:
        """Only the saturation results that actually fired."""
        return [r for r in self.saturation_results if r.saturated]


def next_state(
    current: RoomState,
    saturation_results: Sequence[SaturationResult],
    stale_sensors: Sequence[str],
    tolerance_breaches: Sequence[ToleranceBreach],
) -> RoomState:
    """Pure transition function for a room's health state.

    The next state is fully determined by the *inputs* — ``current`` is
    accepted for signature symmetry with edge-detecting callers but does
    not influence the result (the machine is memoryless; see the module
    docstring's transition table).

    Precedence: any critical-severity saturation wins outright; failing
    that, any warning-severity saturation OR any stale sensor OR any
    tolerance breach yields IMPAIRED; otherwise HEALTHY.

    Args:
        current: The room's current state (unused in the computation).
        saturation_results: Saturation predicate results for the room.
        stale_sensors: Sensor entity tags considered stale.
        tolerance_breaches: Out-of-band parameters for the room.

    Returns:
        The room's next :class:`RoomState`.
    """
    _ = current  # memoryless machine — kept only for caller symmetry

    fired = [r for r in saturation_results if r.saturated]
    if any(r.severity is EventSeverity.critical for r in fired):
        return RoomState.critical

    has_warning_sat = any(
        r.severity is EventSeverity.warning for r in fired
    )
    if has_warning_sat or stale_sensors or tolerance_breaches:
        return RoomState.impaired

    return RoomState.healthy


def _collect_reason_codes(
    saturation_results: Sequence[SaturationResult],
    tolerance_breaches: Sequence[ToleranceBreach],
    stale_sensors: Sequence[str],
) -> list[str]:
    """Build the distinct, ordered reason-code list for an evaluation."""
    codes: list[str] = []
    for result in saturation_results:
        if result.saturated and result.reason_code not in codes:
            codes.append(result.reason_code)
    for breach in tolerance_breaches:
        code = f"tolerance_{breach.result.drift.value}:{breach.param_name}"
        if code not in codes:
            codes.append(code)
    if stale_sensors and "stale_sensor" not in codes:
        codes.append("stale_sensor")
    return codes


async def evaluate_room(
    session: AsyncSession,
    influx: _InfluxLike,
    room_context: RoomContext,
    *,
    day_index: int,
    current_state: RoomState = RoomState.healthy,
    stale_sensors: Sequence[str] | None = None,
) -> RoomEvaluation:
    """Evaluate one room and write the matching event-taxonomy rows.

    Runs the three deterministic checks — equipment saturation
    (:func:`app.core.equipment.evaluate_all`), tolerance drift
    (:func:`app.core.tolerance.check_room_tolerances`) and the
    caller-supplied stale-sensor list — computes the new state with
    :func:`next_state`, then logs:

    * ``-> CRITICAL`` — a ``critical_incident`` :class:`EventLogEntry`
      *and* a chained ``critical_incident`` audit row (this is the only
      state that touches the audit chain here; it needs the legal
      record).
    * ``-> IMPAIRED`` — a ``system_warning`` event (no audit row;
      saturation/staleness is operational, not a deviation by itself).
    * ``-> HEALTHY`` from a worse state — an ``info_event`` recovery
      row.
    * no change — nothing is written (avoids event-log spam every tick).

    Args:
        session: Active async session. Must carry the audit HMAC GUCs if
            a CRITICAL row may be written (``open_session`` does this).
        influx: Sensor reader for the equipment + tolerance checks.
        room_context: The room's sensor entities + setpoints.
        day_index: Cycle day to evaluate tolerances for.
        current_state: The room's last known state, for edge detection.
        stale_sensors: Sensor entity tags the caller already determined
            to be stale (staleness detection itself is not Influx-query
            shaped, so it is provided rather than computed here).

    Returns:
        The :class:`RoomEvaluation`, including the new state.
    """
    stale = list(stale_sensors or [])
    room_id = room_context.room_id

    saturation_results = await evaluate_all(influx, room_context)
    tolerance_breaches = await check_room_tolerances(
        session, influx, room_id, day_index
    )

    new_state = next_state(
        current_state, saturation_results, stale, tolerance_breaches
    )
    reason_codes = _collect_reason_codes(
        saturation_results, tolerance_breaches, stale
    )

    evaluation = RoomEvaluation(
        room_id=room_id,
        day_index=day_index,
        previous=current_state,
        state=new_state,
        saturation_results=saturation_results,
        tolerance_breaches=tolerance_breaches,
        stale_sensors=stale,
        reason_codes=reason_codes,
    )

    log.info(
        "room_evaluated",
        room_id=room_id,
        previous=current_state.value,
        state=new_state.value,
        reason_codes=reason_codes,
    )

    if not evaluation.changed:
        return evaluation

    if new_state is RoomState.critical:
        await _log_critical(session, evaluation)
    elif new_state is RoomState.impaired:
        await _log_impaired(session, evaluation)
    elif new_state is RoomState.healthy:
        await _log_recovery(session, evaluation)

    return evaluation


def _evaluation_payload(evaluation: RoomEvaluation) -> dict[str, object]:
    """Build the JSON payload shared by the event-log rows."""
    return {
        "previous_state": evaluation.previous.value,
        "state": evaluation.state.value,
        "saturated_indicators": [r.indicator_id for r in evaluation.saturated],
        "tolerance_breaches": [
            {
                "param_name": b.param_name,
                "drift": b.result.drift.value,
                "actual": b.result.actual,
                "target": b.result.target,
            }
            for b in evaluation.tolerance_breaches
        ],
        "stale_sensors": evaluation.stale_sensors,
    }


async def _log_critical(
    session: AsyncSession, evaluation: RoomEvaluation
) -> None:
    """Write the ``critical_incident`` event + chained audit row."""
    detail = "; ".join(
        r.detail for r in evaluation.saturated
        if r.severity is EventSeverity.critical
    )
    summary = (
        f"Room {evaluation.room_id} entered CRITICAL: {detail}"
        if detail
        else f"Room {evaluation.room_id} entered CRITICAL"
    )
    audit = await log_audit(
        session,
        event_type=AuditEventType.critical_incident,
        actor_id=_ACTOR,
        room_id=evaluation.room_id,
        summary=summary,
        reason_codes=evaluation.reason_codes,
        params=_evaluation_payload(evaluation),
    )
    session.add(
        EventLogEntry(
            event_type=AuditEventType.critical_incident,
            severity=EventSeverity.critical,
            room_id=evaluation.room_id,
            summary=summary,
            payload=_evaluation_payload(evaluation),
            reason_codes=evaluation.reason_codes,
            audit_event_id=audit.id,
        )
    )
    await session.flush()
    log.error(
        "room_critical",
        room_id=evaluation.room_id,
        reason_codes=evaluation.reason_codes,
    )


async def _log_impaired(
    session: AsyncSession, evaluation: RoomEvaluation
) -> None:
    """Write the ``system_warning`` event for an IMPAIRED transition."""
    bits: list[str] = [
        r.detail for r in evaluation.saturated
        if r.severity is EventSeverity.warning
    ]
    if evaluation.stale_sensors:
        bits.append(f"stale sensors: {', '.join(evaluation.stale_sensors)}")
    if evaluation.tolerance_breaches:
        params = ", ".join(
            b.param_name for b in evaluation.tolerance_breaches
        )
        bits.append(f"out-of-band: {params}")
    detail = "; ".join(bits) or "degraded inputs"
    summary = f"Room {evaluation.room_id} entered IMPAIRED: {detail}"

    session.add(
        EventLogEntry(
            event_type=AuditEventType.system_warning,
            severity=EventSeverity.warning,
            room_id=evaluation.room_id,
            summary=summary,
            payload=_evaluation_payload(evaluation),
            reason_codes=evaluation.reason_codes,
        )
    )
    await session.flush()
    log.warning(
        "room_impaired",
        room_id=evaluation.room_id,
        reason_codes=evaluation.reason_codes,
    )


async def _log_recovery(
    session: AsyncSession, evaluation: RoomEvaluation
) -> None:
    """Write the ``info_event`` row for a recovery to HEALTHY."""
    summary = (
        f"Room {evaluation.room_id} recovered to HEALTHY "
        f"from {evaluation.previous.value.upper()}"
    )
    session.add(
        EventLogEntry(
            event_type=AuditEventType.info_event,
            severity=EventSeverity.info,
            room_id=evaluation.room_id,
            summary=summary,
            payload=_evaluation_payload(evaluation),
            reason_codes=[],
        )
    )
    await session.flush()
    log.info("room_recovered", room_id=evaluation.room_id)
