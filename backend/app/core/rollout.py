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

Phase 7 scope: this module is **read-side only**. It exposes
:func:`effective_mode` (what mode applies to a class at a stage) and
:func:`current_stage` (the stage a room is at). Stage *advancement* —
the QAP-gated transition between stages — is Phase 9.

v0.1 ships every room at the report-only stage
(:data:`app.models.room_runtime.DEFAULT_ROLLOUT_STAGE`), so in practice
:func:`effective_mode` returns ``report_only`` for every class until an
admin advances the room.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

import structlog

from app.models.runtime_adjustment import AdjustmentMode

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
