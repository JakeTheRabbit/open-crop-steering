"""Rollout stages and the per-class effective-mode mapping.

The facility advances through a graduated rollout (plan: 8-week
graduated rollout, gated on QAP sign-off + health metrics). Each
*rollout stage* loosens what the AI is allowed to do with each
*parameter class*:

* **Class A** (read/report params) — always ``report_only``.
* **Class B** (environment: temp, RH, CO2, VPD, PPFD, photoperiod, air
  velocity) — ``bounded_auto_adjust`` permitted from rollout stage 2.
* **Class C** (irrigation: shot sizes, VWC/EC targets, dryback, drain%,
  freq) — from rollout stage 4.
* **Class D** (nutrient/chem: tank pH/EC, leaf temp) — from rollout
  stage 6.
* **Class E** (admin/control state) — the AI **never** writes; always
  ``report_only`` here, and Phase 9's validator additionally refuses any
  Class-E proposal outright.

Below a class's ``bounded_auto_adjust`` stage there is an intermediate
``supervised_approval`` (SFW) band — one stage before auto-adjust the
class moves from report-only to supervised approval. Stage 0/1 is
report-only for everything.

Read-side: :func:`effective_mode` (what mode applies to a class at a
stage) and :func:`current_stage` (the stage a room is at).

Phase 9 adds the **write side** — the QAP-gated stage advancement:

* :func:`can_advance` — evaluates the rollout gates for a room; the
  load-bearing one (REQ-010) is "no unresolved formal deviation".
* :func:`advance_stage` — bumps ``room_runtime.rollout_stage`` by one
  rung when :func:`can_advance` passes, writing a ``rollout_advanced``
  audit row.

v0.1 ships every room at the report-only stage
(:data:`app.models.room_runtime.DEFAULT_ROLLOUT_STAGE`), so in practice
:func:`effective_mode` returns ``report_only`` for every class until a
QAP advances the room.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select

from app.core.audit import log_audit
from app.models.audit_event import AuditEvent, AuditEventType
from app.models.event_log import EventLogEntry
from app.models.room_runtime import RoomRuntime
from app.models.runtime_adjustment import AdjustmentMode

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


class ParamClass(enum.StrEnum):
    """Risk class of a cultivation parameter (plan locked decision #11)."""

    #: Read / report only — never written by the AI.
    a = "A"
    #: Environment setpoints (temp, RH, CO2, VPD, PPFD, photoperiod, air
    #: velocity).
    b = "B"
    #: Irrigation (shot sizes, VWC/EC targets, dryback, drain%, freq).
    c = "C"
    #: Nutrient / chemistry (tank pH/EC, leaf temp).
    d = "D"
    #: Admin / control state (rollout stage, cycle day, no-touch windows,
    #: guardrail bounds, role mappings). The AI never writes Class E.
    e = "E"


#: Maps a cultivation parameter name to its risk class. Parameters not
#: listed default to Class A (report-only) — failing safe.
PARAM_CLASSES: dict[str, ParamClass] = {
    # --- Class B: environment ------------------------------------------
    "temp_day": ParamClass.b,
    "temp_night": ParamClass.b,
    "rh_day": ParamClass.b,
    "rh_night": ParamClass.b,
    "co2_day": ParamClass.b,
    "vpd_day": ParamClass.b,
    "ppfd": ParamClass.b,
    "photoperiod_hours": ParamClass.b,
    "air_velocity": ParamClass.b,
    # --- Class C: irrigation -------------------------------------------
    "vwc_target": ParamClass.c,
    "ec_target": ParamClass.c,
    "dryback_pct": ParamClass.c,
    "shot_size": ParamClass.c,
    "drain_pct": ParamClass.c,
    "irrigation_freq": ParamClass.c,
    # --- Class D: nutrient / chemistry ---------------------------------
    "tank_ph": ParamClass.d,
    "tank_ec": ParamClass.d,
    "leaf_temp": ParamClass.d,
    # --- Class E: admin / control state (the AI never writes these) ----
    "rollout_stage": ParamClass.e,
    "cycle_start": ParamClass.e,
    "cycle_start_date": ParamClass.e,
    "no_touch_window": ParamClass.e,
    "guardrail_bounds": ParamClass.e,
    "role_mapping": ParamClass.e,
}


def class_for_param(param_name: str) -> ParamClass:
    """Return the risk class of a parameter (Class A if unknown — fail safe)."""
    return PARAM_CLASSES.get(param_name, ParamClass.a)


@dataclass(frozen=True, slots=True)
class RolloutStage:
    """One rung of the graduated rollout.

    Attributes:
        index: 0-based stage number. Higher = more AI autonomy.
        name: Stable identifier persisted in ``room_runtime.rollout_stage``.
        description: Human-readable summary for the admin UI.
    """

    index: int
    name: str
    description: str


#: Ordered rollout ladder. Stage 0 is the v0.1 default — report-only for
#: every class. The ``bounded_auto_adjust`` stage thresholds below
#: (2 / 4 / 6 for B / C / D) index into this ladder.
ROLLOUT_STAGES: tuple[RolloutStage, ...] = (
    RolloutStage(0, "report_only", "Report only — AI observes, never proposes to apply"),
    RolloutStage(1, "stage_1", "Report only — extended evidence period"),
    RolloutStage(2, "stage_2", "Class B bounded auto-adjust enabled"),
    RolloutStage(3, "stage_3", "Class B auto-adjust; Class C supervised approval"),
    RolloutStage(4, "stage_4", "Class C bounded auto-adjust enabled"),
    RolloutStage(5, "stage_5", "Class C auto-adjust; Class D supervised approval"),
    RolloutStage(6, "stage_6", "Class D bounded auto-adjust enabled (tighter caps)"),
)

#: Stage name -> :class:`RolloutStage`, for lookups from ``room_runtime``.
_STAGES_BY_NAME: dict[str, RolloutStage] = {s.name: s for s in ROLLOUT_STAGES}

#: First rollout-stage *index* at which a class reaches
#: ``bounded_auto_adjust`` (plan locked decision #11). One stage earlier
#: the class is at ``supervised_approval``; earlier still, ``report_only``.
_AUTO_ADJUST_STAGE: dict[ParamClass, int] = {
    ParamClass.b: 2,
    ParamClass.c: 4,
    ParamClass.d: 6,
}

#: The report-only stage every room starts at (index 0).
REPORT_ONLY_STAGE: RolloutStage = ROLLOUT_STAGES[0]


def stage_by_name(name: str) -> RolloutStage:
    """Resolve a stage name to its :class:`RolloutStage`.

    An unrecognised name falls back to :data:`REPORT_ONLY_STAGE` — an
    unknown stage must never grant *more* autonomy than report-only.

    Args:
        name: Stage name (as stored in ``room_runtime.rollout_stage``).

    Returns:
        The matching :class:`RolloutStage`, or :data:`REPORT_ONLY_STAGE`.
    """
    stage = _STAGES_BY_NAME.get(name)
    if stage is None:
        log.warning("rollout_unknown_stage", name=name)
        return REPORT_ONLY_STAGE
    return stage


def current_stage(room_runtime: object) -> RolloutStage:
    """Return the :class:`RolloutStage` a room is currently at.

    Reads ``rollout_stage`` off a
    :class:`~app.models.room_runtime.RoomRuntime` row (typed loosely so
    this module does not import the model and create a cycle). An
    unset / unknown stage resolves to :data:`REPORT_ONLY_STAGE`.

    Args:
        room_runtime: A ``RoomRuntime``-like object with a
            ``rollout_stage`` string attribute.

    Returns:
        The room's current rollout stage.
    """
    name = getattr(room_runtime, "rollout_stage", REPORT_ONLY_STAGE.name)
    return stage_by_name(str(name))


def effective_mode(
    stage: RolloutStage | str | int, param_class: ParamClass | str
) -> AdjustmentMode:
    """Return the AI mode that applies to a parameter class at a stage.

    The mapping (plan locked decision #11):

    * Class A and Class E — always :attr:`AdjustmentMode.report_only`.
    * Class B / C / D — :attr:`~AdjustmentMode.bounded_auto_adjust` once
      the rollout reaches the class's auto-adjust stage (2 / 4 / 6);
      :attr:`~AdjustmentMode.supervised_approval` exactly one stage
      before that; :attr:`~AdjustmentMode.report_only` earlier.

    Args:
        stage: A :class:`RolloutStage`, a stage name, or a stage index.
        param_class: A :class:`ParamClass` or its value (``"A"`` ...
            ``"E"``).

    Returns:
        The :class:`~app.models.runtime_adjustment.AdjustmentMode` the AI
        operates in for that class at that stage.
    """
    resolved_stage = _coerce_stage(stage)
    pclass = (
        param_class
        if isinstance(param_class, ParamClass)
        else ParamClass(param_class)
    )

    # Class A (read-only) and Class E (admin) are never AI-writable.
    if pclass in (ParamClass.a, ParamClass.e):
        return AdjustmentMode.report_only

    auto_stage = _AUTO_ADJUST_STAGE[pclass]
    if resolved_stage.index >= auto_stage:
        return AdjustmentMode.bounded_auto_adjust
    if resolved_stage.index >= auto_stage - 1:
        return AdjustmentMode.supervised_approval
    return AdjustmentMode.report_only


def _coerce_stage(stage: RolloutStage | str | int) -> RolloutStage:
    """Normalise a stage given as an object, a name, or an index."""
    if isinstance(stage, RolloutStage):
        return stage
    if isinstance(stage, int):
        if 0 <= stage < len(ROLLOUT_STAGES):
            return ROLLOUT_STAGES[stage]
        log.warning("rollout_unknown_stage_index", index=stage)
        return REPORT_ONLY_STAGE
    return stage_by_name(stage)


# ---------------------------------------------------------------------------
# Phase 9 — QAP-gated rollout-stage advancement.
# ---------------------------------------------------------------------------

#: Minimum days a room must sit at a rollout stage before it may advance
#: (plan Phase 14 gate: ">= 5 days at stage"). The supervisor's
#: ``last_tick_at`` is not a stage-entry timestamp, so the per-stage dwell
#: gate is *recorded* here and *checked* once a stage-entry timestamp
#: lands (see :class:`AdvanceCheck.min_days_at_stage_met`).
MIN_DAYS_AT_STAGE = 5

#: Maximum SFW (supervised-approval) rejections in the trailing 7 days
#: that still permits an advance (plan Phase 14 gate: "< 3 SFW
#: rejections last 7 days").
MAX_SFW_REJECTIONS_7D = 3

#: Trailing window for the SFW-rejection gate.
_SFW_REJECTION_WINDOW = dt.timedelta(days=7)


@dataclass(slots=True)
class AdvanceCheck:
    """The outcome of evaluating a room's rollout-advance gates.

    The advance is permitted only when :attr:`can_advance` is ``True``.
    Plan locked decision #16 / Phase 14 lists several gates; Phase 9
    *enforces* the formal-deviation gate (REQ-010) and *records* the
    others as fields so the UI can show the full picture and later
    phases can wire the remaining checks without changing this contract.

    Attributes:
        room_id: The room the check is for.
        can_advance: ``True`` iff every *enforced* gate passes — Phase 9
            enforces :attr:`no_open_deviations`. The advisory gates
            (dwell time, SFW rejections, QAP approval) are surfaced but
            do not by themselves block in Phase 9.
        current_stage: The room's current rollout stage.
        next_stage: The stage an advance would move to (``None`` if the
            room is already at the final stage).
        no_open_deviations: ``True`` iff the room has **no** unresolved
            formal deviation — the load-bearing REQ-010 gate. ``False``
            blocks the advance outright.
        open_deviation_count: Number of unresolved formal deviations for
            the room.
        sfw_rejections_7d: SFW rejections in the trailing 7 days.
        sfw_rejection_gate_met: ``True`` iff ``sfw_rejections_7d`` is
            below :data:`MAX_SFW_REJECTIONS_7D` (advisory in Phase 9).
        min_days_at_stage_met: ``True`` iff the dwell-time gate is
            satisfied. Phase 9 cannot compute stage dwell (no
            stage-entry timestamp yet), so this defaults to ``True`` and
            is documented as a later wiring point.
        qap_approval_recorded: ``True`` iff a QAP has recorded approval
            for this advance. In Phase 9 the QAP *calling* the advance
            endpoint **is** the recorded approval, so this is set by
            :func:`advance_stage`.
        blockers: Human-readable reasons the advance is blocked (empty
            when :attr:`can_advance`).
    """

    room_id: str
    can_advance: bool
    current_stage: RolloutStage
    next_stage: RolloutStage | None
    no_open_deviations: bool
    open_deviation_count: int = 0
    sfw_rejections_7d: int = 0
    sfw_rejection_gate_met: bool = True
    min_days_at_stage_met: bool = True
    qap_approval_recorded: bool = False
    blockers: list[str] = field(default_factory=list)


def next_stage(stage: RolloutStage) -> RolloutStage | None:
    """Return the stage one rung above ``stage`` (``None`` at the top)."""
    nxt = stage.index + 1
    if nxt < len(ROLLOUT_STAGES):
        return ROLLOUT_STAGES[nxt]
    return None


async def _open_deviation_count(session: AsyncSession, room_id: str) -> int:
    """Count unresolved (unacknowledged) formal deviations for a room."""
    count = await session.scalar(
        select(func.count())
        .select_from(EventLogEntry)
        .where(
            EventLogEntry.event_type == AuditEventType.formal_deviation,
            EventLogEntry.room_id == room_id,
            EventLogEntry.acknowledged_at.is_(None),
        )
    )
    return int(count or 0)


async def _sfw_rejections_7d(session: AsyncSession, room_id: str) -> int:
    """Count SFW (supervised-approval) rejections for a room in the last 7d.

    An SFW rejection on a failed re-check is audited as a
    ``guardrail_rejection`` carrying a ``pending_id`` in ``params``; a
    plain SFW reject is an ``info_event`` with ``decision == 'rejected'``.
    The advance gate counts the former — re-check failures are the
    quality signal the plan's gate cares about.
    """
    since = dt.datetime.now(dt.UTC) - _SFW_REJECTION_WINDOW
    count = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.event_type == AuditEventType.guardrail_rejection,
            AuditEvent.room_id == room_id,
            AuditEvent.occurred_at >= since,
            AuditEvent.params["pending_id"].astext.isnot(None),
        )
    )
    return int(count or 0)


async def can_advance(session: AsyncSession, room_id: str) -> AdvanceCheck:
    """Evaluate whether a room may advance its rollout stage.

    The **enforced** gate (plan REQ-010) is: a room with any unresolved
    formal deviation may not advance. Plan Phase 14 lists further gates
    (>= 5 days at stage, < 3 SFW rejections / 7d, QAP-recorded approval);
    Phase 9 *records* those on the returned :class:`AdvanceCheck` —
    ``sfw_rejections_7d`` is queried live, the dwell gate is documented
    as a later wiring point, QAP approval is supplied by the advance
    call itself — but only the formal-deviation gate blocks here.

    Args:
        session: Active async session.
        room_id: The room to evaluate.

    Returns:
        An :class:`AdvanceCheck` — ``can_advance`` is ``True`` only when
        every enforced gate passes and the room is not already at the
        final stage.
    """
    runtime = await session.get(RoomRuntime, room_id)
    stage = (
        current_stage(runtime)
        if runtime is not None
        else REPORT_ONLY_STAGE
    )
    nxt = next_stage(stage)

    open_count = await _open_deviation_count(session, room_id)
    no_open = open_count == 0
    sfw_rejections = await _sfw_rejections_7d(session, room_id)
    sfw_gate_met = sfw_rejections < MAX_SFW_REJECTIONS_7D

    blockers: list[str] = []
    if not no_open:
        blockers.append(
            f"{open_count} unresolved formal deviation(s) — must be "
            "acknowledged by a QAP before advancing (REQ-010)"
        )
    if nxt is None:
        blockers.append(
            f"room is already at the final rollout stage ({stage.name})"
        )

    permitted = no_open and nxt is not None

    check = AdvanceCheck(
        room_id=room_id,
        can_advance=permitted,
        current_stage=stage,
        next_stage=nxt,
        no_open_deviations=no_open,
        open_deviation_count=open_count,
        sfw_rejections_7d=sfw_rejections,
        sfw_rejection_gate_met=sfw_gate_met,
        blockers=blockers,
    )
    log.info(
        "rollout_advance_checked",
        room_id=room_id,
        can_advance=permitted,
        current_stage=stage.name,
        open_deviations=open_count,
        sfw_rejections_7d=sfw_rejections,
    )
    return check


async def advance_stage(
    session: AsyncSession, room_id: str, *, qap_user: str
) -> RoomRuntime:
    """Advance a room one rollout-stage rung — QAP-gated.

    Runs :func:`can_advance`; if every enforced gate passes, bumps
    ``room_runtime.rollout_stage`` to the next rung and writes a
    ``rollout_advanced`` audit event. The QAP making this call **is**
    the plan's "QAP-recorded approval" gate — their id is the audit
    ``actor_id``.

    Args:
        session: Active async session.
        room_id: The room to advance.
        qap_user: ``users.id`` of the advancing QAP (the caller has
            already role-checked them).

    Returns:
        The updated :class:`~app.models.room_runtime.RoomRuntime` row.

    Raises:
        ValueError: If the room cannot advance — an unresolved formal
            deviation, or the room is already at the final stage. The
            message names every blocker.
    """
    check = await can_advance(session, room_id)
    if not check.can_advance:
        raise ValueError(
            f"room {room_id} cannot advance rollout stage: "
            + "; ".join(check.blockers)
        )
    assert check.next_stage is not None  # can_advance guarantees this

    runtime = await session.get(RoomRuntime, room_id)
    if runtime is None:
        # A never-ticked room: create the row so the advance is durable.
        runtime = RoomRuntime(room_id=room_id)
        session.add(runtime)
        await session.flush()

    from_stage = check.current_stage
    runtime.rollout_stage = check.next_stage.name
    await session.flush()

    await log_audit(
        session,
        event_type=AuditEventType.rollout_advanced,
        actor_id=qap_user,
        actor_role="qap",
        room_id=room_id,
        summary=(
            f"Rollout advanced for {room_id}: {from_stage.name} -> "
            f"{check.next_stage.name} (by QAP {qap_user})"
        ),
        params={
            "from_stage": from_stage.name,
            "from_stage_index": from_stage.index,
            "to_stage": check.next_stage.name,
            "to_stage_index": check.next_stage.index,
        },
    )
    log.info(
        "rollout_advanced",
        room_id=room_id,
        from_stage=from_stage.name,
        to_stage=check.next_stage.name,
        qap_user=qap_user,
    )
    return runtime
