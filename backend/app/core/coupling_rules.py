"""Equipment-coupling rule checks — ``EC-001`` ... ``EC-015``.

Implements every coupling rule from `cultivation_knowledge.md`
Sections 2 and 6. A *coupling rule* encodes the truth that cultivation
equipment is not a set of independent sliders: changing one setpoint
loads (or fights) another piece of equipment. A proposal that ignores a
coupling is rejected by the Phase 9 validator (:mod:`app.core.guardrails`).

Hard-refusal vs fail-soft
-------------------------
The knowledge base draws a line the room-configuration wizard honours
(Section 6): some couplings are **hard refusals** (the config / proposal
cannot proceed) and some are **fail-soft warnings** (allowed, but
flagged). Each :class:`CouplingViolation` carries a ``hard`` flag so the
validator can apply that distinction — the Phase 9 validator rejects on
*any* violation, but the ``hard`` flag is preserved in ``reason_codes``
detail and is what the config wizard (Phase 11) keys off.

Check signature
---------------
Each check is a pure function
``(change, snapshot, room_config) -> CouplingViolation | None``:

* ``change`` — one :class:`~app.schemas.llm_decision.ProposedChange`.
* ``snapshot`` — the :class:`~app.models.sensor_snapshot.SensorSnapshot`
  the proposal was reasoned about (``equipment_status``, ``sensors``,
  ``setpoints``).
* ``room_config`` — the room's static equipment / coupling map: which
  entities are mapped, whether a reheat coil exists, the configured
  PPFD increase ceiling, etc. A :class:`RoomConfig` (or a plain dict).

The :data:`COUPLING_RULES` registry + :func:`check_coupling` run every
check and collect the violations.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from app.schemas.llm_decision import ProposedChange

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Coupling thresholds (from cultivation_knowledge.md Sections 2 / 6).
# Public defaults — a facility tightens them in its private overrides.
# ---------------------------------------------------------------------------

#: EC-001 / EC-002 — a PPFD increase of at least this fraction of the
#: current PPFD triggers the HVAC / dehum headroom review.
PPFD_HEADROOM_REVIEW_FRACTION = 0.10

#: EC-011 — rough mandatory-runoff minimum (% of feed volume). Cutting
#: drain% below this is a hard salt-accumulation refusal.
RUNOFF_MIN_PCT = 10.0

#: EC-013 — a last irrigation shot within this many minutes of
#: lights-off is a night-RH-spike refusal.
LATE_SHOT_MIN = 30


@dataclass(slots=True)
class RoomConfig:
    """Static per-room equipment / coupling map for the coupling checks.

    Mirrors what the room-configuration wizard records (plan Phase 11).
    Every field defaults to "absent / unconstrained" so a partially
    configured room fails *soft* rather than crashing a check.

    Attributes:
        room_id: Room identifier.
        has_reheat: A reheat coil is mapped (EC-004).
        has_exhaust: An exhaust fan is mapped (EC-005 / EC-007 / EC-015).
        has_intake_filter: The fresh-air intake is filtered (EC-015).
        has_under_canopy_airflow: Under-canopy circulation is mapped
            (EC-009).
        has_under_canopy_rh_probe: An under-canopy RH probe is mapped
            (EC-009).
        co2_enrichment_enabled: CO2 enrichment is configured (EC-005 /
            EC-007).
        hvac_headroom_source: Name of the cooling-headroom calculation
            source, or ``None`` if none is declared (EC-001).
        dehu_headroom_source: Name of the dehum-headroom source, or
            ``None`` (EC-002).
        max_ppfd_increase_pct: Configured ceiling on a single PPFD
            increase, as a fraction (``0.10`` == 10%); ``None`` == no
            ceiling declared (EC-001).
        sensor_placement_validated: The room's climate-sensor placement
            has been validated (EC-014).
    """

    room_id: str
    has_reheat: bool = False
    has_exhaust: bool = False
    has_intake_filter: bool = False
    has_under_canopy_airflow: bool = False
    has_under_canopy_rh_probe: bool = False
    co2_enrichment_enabled: bool = False
    hvac_headroom_source: str | None = None
    dehu_headroom_source: str | None = None
    max_ppfd_increase_pct: float | None = None
    sensor_placement_validated: bool = True

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> RoomConfig:
        """Build a :class:`RoomConfig` from a config mapping.

        Unknown keys are ignored; missing keys take the dataclass
        default. ``room_id`` defaults to an empty string when absent.

        Args:
            raw: A config mapping (or ``None`` for an all-defaults
                config).

        Returns:
            The parsed :class:`RoomConfig`.
        """
        raw = raw or {}
        return cls(
            room_id=str(raw.get("room_id", "")),
            has_reheat=bool(raw.get("has_reheat", False)),
            has_exhaust=bool(raw.get("has_exhaust", False)),
            has_intake_filter=bool(raw.get("has_intake_filter", False)),
            has_under_canopy_airflow=bool(
                raw.get("has_under_canopy_airflow", False)
            ),
            has_under_canopy_rh_probe=bool(
                raw.get("has_under_canopy_rh_probe", False)
            ),
            co2_enrichment_enabled=bool(
                raw.get("co2_enrichment_enabled", False)
            ),
            hvac_headroom_source=raw.get("hvac_headroom_source"),
            dehu_headroom_source=raw.get("dehu_headroom_source"),
            max_ppfd_increase_pct=raw.get("max_ppfd_increase_pct"),
            sensor_placement_validated=bool(
                raw.get("sensor_placement_validated", True)
            ),
        )


@dataclass(slots=True)
class CouplingViolation:
    """One coupling rule violated by a proposed change.

    Attributes:
        ec_id: The ``EC-*`` id (e.g. ``"EC-001"``).
        reason: Human-readable explanation for logs / audit / the
            operator-facing rejection detail.
        hard: ``True`` for a hard-refusal coupling, ``False`` for a
            fail-soft warning (the knowledge base's Section 6
            distinction). The Phase 9 validator rejects on either, but
            the flag is preserved for the config wizard.
        param_name: The parameter the offending change targeted.
        reason_codes: Machine-actionable ids — always contains
            ``ec_id``.
    """

    ec_id: str
    reason: str
    param_name: str
    hard: bool = True
    reason_codes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.ec_id not in self.reason_codes:
            self.reason_codes.append(self.ec_id)


# ---------------------------------------------------------------------------
# Snapshot-payload + room-config accessors (fail-safe on partial input).
# ---------------------------------------------------------------------------


def _payload(snapshot: Any) -> dict[str, Any]:
    """Return a snapshot's ``payload`` dict (empty if absent)."""
    return getattr(snapshot, "payload", {}) or {}


def _setpoints(snapshot: Any) -> dict[str, Any]:
    """Return ``payload["setpoints"]``."""
    return _payload(snapshot).get("setpoints", {}) or {}


def _sensors(snapshot: Any) -> dict[str, dict[str, Any]]:
    """Return ``payload["sensors"]``."""
    return _payload(snapshot).get("sensors", {}) or {}


def _equipment_status(snapshot: Any) -> dict[str, str]:
    """Return ``payload["equipment_status"]``."""
    return _payload(snapshot).get("equipment_status", {}) or {}


def _equipment_monitored(snapshot: Any, equipment_id: str) -> bool:
    """Return whether an equipment id is monitored (mapped) for the room."""
    return _equipment_status(snapshot).get(equipment_id) not in (
        None,
        "unmonitored",
    )


def _current_ppfd(snapshot: Any) -> float | None:
    """Return the current PPFD setpoint, if the snapshot carries one."""
    for key in ("ppfd", "ppfd_day"):
        value = _setpoints(snapshot).get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _sensor_value(snapshot: Any, token: str) -> float | None:
    """Return the first sensor value whose tag contains ``token``."""
    for tag, reading in _sensors(snapshot).items():
        if token in tag.lower():
            value = reading.get("value")
            if isinstance(value, (int, float)):
                return float(value)
    return None


def _as_config(room_config: RoomConfig | dict[str, Any] | None) -> RoomConfig:
    """Coerce a coupling-check ``room_config`` argument to :class:`RoomConfig`."""
    if isinstance(room_config, RoomConfig):
        return room_config
    return RoomConfig.from_mapping(room_config)


# ---------------------------------------------------------------------------
# Per-parameter helpers.
# ---------------------------------------------------------------------------

_TEMP_PARAMS = frozenset({"temp", "temp_day", "temp_night"})
_RH_PARAMS = frozenset({"rh", "rh_day", "rh_night"})
_CO2_PARAMS = frozenset({"co2", "co2_day"})
_PPFD_PARAMS = frozenset({"ppfd", "ppfd_day"})
_DRYBACK_PARAMS = frozenset({"dryback_pct"})
_RUNOFF_PARAMS = frozenset({"drain_pct", "runoff_pct"})
_EC_PARAMS = frozenset({"ec_target", "tank_ec"})
_VELOCITY_PARAMS = frozenset({"air_velocity"})
_SHOT_TIMING_PARAMS = frozenset({"last_shot_offset", "irrigation_freq"})


def _is_increase(change: ProposedChange) -> bool:
    """Return whether a change raises its parameter."""
    return change.direction == "increase" or change.delta > 0


def _is_decrease(change: ProposedChange) -> bool:
    """Return whether a change lowers its parameter."""
    return change.direction == "decrease" or change.delta < 0


def _ppfd_increase_fraction(
    change: ProposedChange, snapshot: Any
) -> float | None:
    """Return a PPFD increase as a fraction of the current PPFD setpoint."""
    current = _current_ppfd(snapshot)
    if current is None or current <= 0:
        return None
    return abs(change.delta) / current


# ---------------------------------------------------------------------------
# The fifteen coupling-rule checks.
# ---------------------------------------------------------------------------


def check_ec001(
    change: ProposedChange, snapshot: Any, room_config: Any
) -> CouplingViolation | None:
    """``EC-001`` — PPFD increase needs cooling headroom or HVAC review.

    A PPFD increase of >= :data:`PPFD_HEADROOM_REVIEW_FRACTION` of the
    current setpoint requires either a declared HVAC-headroom calculation
    source or an explicit increase ceiling that is not exceeded. Without
    one, the change demands an HVAC re-sizing review. Hard refusal.
    """
    if change.param_name not in _PPFD_PARAMS or not _is_increase(change):
        return None
    fraction = _ppfd_increase_fraction(change, snapshot)
    if fraction is None or fraction < PPFD_HEADROOM_REVIEW_FRACTION:
        return None
    cfg = _as_config(room_config)
    ceiling = cfg.max_ppfd_increase_pct
    if cfg.hvac_headroom_source and (ceiling is None or fraction <= ceiling):
        return None
    return CouplingViolation(
        ec_id="EC-001",
        param_name=change.param_name,
        hard=True,
        reason=(
            f"Proposed PPFD increase is {fraction * 100:.0f}% of the "
            f"current setpoint (>= {PPFD_HEADROOM_REVIEW_FRACTION * 100:.0f}"
            "%) but no HVAC-headroom source is declared / the increase "
            "ceiling is exceeded — requires HVAC re-sizing review "
            "(EC-001 PPFD<->HVAC)."
        ),
    )


def check_ec002(
    change: ProposedChange, snapshot: Any, room_config: Any
) -> CouplingViolation | None:
    """``EC-002`` — PPFD increase needs dehumidification headroom.

    Higher PPFD raises transpiration, which raises dehum load. A PPFD
    increase past the review fraction needs a declared dehum-headroom
    source. Hard refusal.
    """
    if change.param_name not in _PPFD_PARAMS or not _is_increase(change):
        return None
    fraction = _ppfd_increase_fraction(change, snapshot)
    if fraction is None or fraction < PPFD_HEADROOM_REVIEW_FRACTION:
        return None
    cfg = _as_config(room_config)
    if cfg.dehu_headroom_source:
        return None
    return CouplingViolation(
        ec_id="EC-002",
        param_name=change.param_name,
        hard=True,
        reason=(
            f"Proposed PPFD increase is {fraction * 100:.0f}% of the "
            "current setpoint but no dehumidification-headroom source is "
            "declared — higher PPFD raises transpiration and dehum load "
            "(EC-002 PPFD<->Dehum)."
        ),
    )


def check_ec003(
    change: ProposedChange, snapshot: Any, room_config: Any  # noqa: ARG001
) -> CouplingViolation | None:
    """``EC-003`` — PPFD increase must leave the air-temp setpoint leaf-temp room.

    Higher PPFD raises leaf temperature. If leaf temp is already at or
    above air temp (bleach risk) a PPFD increase makes it worse — the
    air-temp setpoint must give the leaf room. Hard refusal.
    """
    if change.param_name not in _PPFD_PARAMS or not _is_increase(change):
        return None
    leaf = _sensor_value(snapshot, "leaf_temp")
    air = _setpoints(snapshot).get("temp") or _setpoints(snapshot).get(
        "temp_day"
    )
    if leaf is None or not isinstance(air, (int, float)):
        return None
    if leaf < float(air):
        return None
    return CouplingViolation(
        ec_id="EC-003",
        param_name=change.param_name,
        hard=True,
        reason=(
            f"Proposed PPFD increase ({change.delta:+g}) while leaf temp "
            f"{leaf:g}degC is already at/above air temp {float(air):g}degC "
            "— higher PPFD raises leaf temp; bleach risk "
            "(EC-003 PPFD<->Leaf temp)."
        ),
    )


def check_ec004(
    change: ProposedChange, snapshot: Any, room_config: Any
) -> CouplingViolation | None:
    """``EC-004`` — dehum + AC without reheat fight each other on VPD.

    A standalone dehumidifier converts latent moisture to sensible heat;
    without a reheat coil the AC over-cools to dehumidify and the two
    units fight on VPD. Lowering RH in such a room is a fail-soft warning
    (the operator can still choose it, but should know the cost).
    """
    if change.param_name not in _RH_PARAMS or not _is_decrease(change):
        return None
    cfg = _as_config(room_config)
    if cfg.has_reheat:
        return None
    if not (
        _equipment_monitored(snapshot, "dehu")
        and _equipment_monitored(snapshot, "ac")
    ):
        return None
    return CouplingViolation(
        ec_id="EC-004",
        param_name=change.param_name,
        hard=False,
        reason=(
            f"Proposed RH cut ({change.delta:+g}) in a dehu+AC room with "
            "no reheat coil — the dehumidifier's sensible-heat conversion "
            "makes the AC over-cool; the two fight on VPD "
            "(EC-004 Dehum<->Reheat, fail-soft)."
        ),
    )


def check_ec005(
    change: ProposedChange, snapshot: Any, room_config: Any
) -> CouplingViolation | None:
    """``EC-005`` — CO2 enrichment with an active exhaust fights itself.

    Running CO2 enrichment while the exhaust is active vents the
    enrichment as fast as it is injected. Increasing CO2 in a room with
    a mapped exhaust is a hard refusal — suppress the exhaust first.
    """
    if change.param_name not in _CO2_PARAMS or not _is_increase(change):
        return None
    cfg = _as_config(room_config)
    exhaust_active = bool(_payload(snapshot).get("exhaust_active"))
    if not (cfg.has_exhaust and exhaust_active):
        return None
    return CouplingViolation(
        ec_id="EC-005",
        param_name=change.param_name,
        hard=True,
        reason=(
            f"Proposed CO2 increase ({change.delta:+g}) while the exhaust "
            "is active — enrichment is vented as fast as it is injected; "
            "suppress the exhaust first (EC-005 CO2<->Exhaust)."
        ),
    )


def check_ec006(
    change: ProposedChange, snapshot: Any, room_config: Any  # noqa: ARG001
) -> CouplingViolation | None:
    """``EC-006`` — CO2 enrichment only pays if PPFD is sufficient.

    Below a stage's PPFD threshold light is the limiting factor and CO2
    enrichment is wasted. Fail-soft warning (ROI, not safety).
    """
    if change.param_name not in _CO2_PARAMS or not _is_increase(change):
        return None
    ppfd = _current_ppfd(snapshot)
    threshold = _payload(snapshot).get("ppfd_co2_threshold")
    if ppfd is None or not isinstance(threshold, (int, float)):
        return None
    if ppfd >= float(threshold):
        return None
    return CouplingViolation(
        ec_id="EC-006",
        param_name=change.param_name,
        hard=False,
        reason=(
            f"Proposed CO2 increase ({change.delta:+g}) while PPFD "
            f"{ppfd:g} is below the {float(threshold):g} threshold — light "
            "is limiting; enrichment ROI is poor "
            "(EC-006 CO2<->Light, fail-soft)."
        ),
    )


def check_ec007(
    change: ProposedChange, snapshot: Any, room_config: Any  # noqa: ARG001
) -> CouplingViolation | None:
    """``EC-007`` — CO2 enrichment in a negative-pressure room leaks.

    A negative-pressure room (``SAT-PRESS``) leaks conditioned, enriched
    air; CO2 enrichment ROI suffers. Fail-soft warning.
    """
    if change.param_name not in _CO2_PARAMS or not _is_increase(change):
        return None
    status = _equipment_status(snapshot).get("pressure")
    if status != "SAT-PRESS":
        return None
    return CouplingViolation(
        ec_id="EC-007",
        param_name=change.param_name,
        hard=False,
        reason=(
            f"Proposed CO2 increase ({change.delta:+g}) in a "
            "negative-pressure room (SAT-PRESS) — conditioned, enriched "
            "air leaks out; enrichment ROI suffers "
            "(EC-007 CO2<->Negative pressure, fail-soft)."
        ),
    )


def check_ec008(
    change: ProposedChange, snapshot: Any, room_config: Any  # noqa: ARG001
) -> CouplingViolation | None:
    """``EC-008`` — low night temp + residual transpiration is a dew trap.

    Lowering the night-temp setpoint while transpiration is still
    residual can drop the coldest surface below the dew point even at an
    "acceptable" RH. The dew-point margin must be checked. Hard refusal
    when active condensation (``SAT-DEW``) is already flagged.
    """
    if (
        change.param_name not in ("temp_night", "temp")
        or not _is_decrease(change)
    ):
        return None
    status = _equipment_status(snapshot).get("condensation")
    if status != "SAT-DEW":
        return None
    return CouplingViolation(
        ec_id="EC-008",
        param_name=change.param_name,
        hard=True,
        reason=(
            f"Proposed night-temp cut ({change.delta:+g}) while active "
            "condensation is flagged (SAT-DEW) — a lower night temp drops "
            "the coldest surface further below the dew point "
            "(EC-008 RH<->Night temp)."
        ),
    )


def check_ec009(
    change: ProposedChange, snapshot: Any, room_config: Any  # noqa: ARG001
) -> CouplingViolation | None:
    """``EC-009`` — dense canopy without under-canopy airflow hides humidity.

    A dense canopy without under-canopy airflow traps humidity pockets
    the room sensor cannot see — a botrytis risk. Lowering RH "to fix
    it" is a fail-soft warning when the room lacks under-canopy airflow:
    the room sensor reading is not representative.
    """
    if change.param_name not in _RH_PARAMS or not _is_decrease(change):
        return None
    cfg = _as_config(room_config)
    if cfg.has_under_canopy_airflow:
        return None
    return CouplingViolation(
        ec_id="EC-009",
        param_name=change.param_name,
        hard=False,
        reason=(
            f"Proposed RH change ({change.delta:+g}) in a room with no "
            "under-canopy airflow — humidity pockets under a dense canopy "
            "are invisible to the room sensor; botrytis risk "
            "(EC-009 Airflow<->Disease, fail-soft)."
        ),
    )


def check_ec010(
    change: ProposedChange, snapshot: Any, room_config: Any  # noqa: ARG001
) -> CouplingViolation | None:
    """``EC-010`` — faster dryback concentrates substrate EC.

    Faster dryback concentrates substrate EC; if feed EC is constant,
    runoff EC drifts up. The proposal needs runoff EC data to be safe.
    Fail-soft warning when runoff EC is not in the snapshot.
    """
    if change.param_name not in _DRYBACK_PARAMS or not _is_increase(change):
        return None
    runoff_ec = _sensor_value(snapshot, "runoff_ec")
    if runoff_ec is not None:
        return None
    return CouplingViolation(
        ec_id="EC-010",
        param_name=change.param_name,
        hard=False,
        reason=(
            f"Proposed dryback increase ({change.delta:+g}) with no runoff "
            "EC data — faster dryback concentrates substrate EC; runoff EC "
            "is needed to confirm it is safe "
            "(EC-010 Dryback<->EC, fail-soft)."
        ),
    )


def check_ec011(
    change: ProposedChange, snapshot: Any, room_config: Any  # noqa: ARG001
) -> CouplingViolation | None:
    """``EC-011`` — insufficient runoff accumulates salt.

    Some runoff is mandatory regardless of feed EC; below the rough
    :data:`RUNOFF_MIN_PCT` minimum, salt accumulates. A drain%/runoff
    cut whose resulting value falls below the minimum is a hard refusal.
    """
    if change.param_name not in _RUNOFF_PARAMS or not _is_decrease(change):
        return None
    current = _setpoints(snapshot).get(change.param_name)
    if not isinstance(current, (int, float)):
        return None
    resulting = float(current) + change.delta
    if resulting >= RUNOFF_MIN_PCT:
        return None
    return CouplingViolation(
        ec_id="EC-011",
        param_name=change.param_name,
        hard=True,
        reason=(
            f"Proposed runoff/drain cut ({change.delta:+g}) takes it to "
            f"{resulting:g}% — below the rough {RUNOFF_MIN_PCT:g}% "
            "mandatory minimum; salt accumulates regardless of feed EC "
            "(EC-011 Runoff<->Salt accumulation)."
        ),
    )


def check_ec012(
    change: ProposedChange, snapshot: Any, room_config: Any  # noqa: ARG001
) -> CouplingViolation | None:
    """``EC-012`` — high VPD + high substrate EC is combined stress.

    Aggressive dryback (high VPD) and high feed EC together cause
    combined edge-burn / water-stress. Raising feed EC while VPD is
    already high is a hard refusal.
    """
    if change.param_name not in _EC_PARAMS or not _is_increase(change):
        return None
    vpd = _setpoints(snapshot).get("vpd_day") or _sensor_value(snapshot, "vpd")
    vpd_high = bool(_payload(snapshot).get("vpd_high"))
    if isinstance(vpd, (int, float)) and vpd >= 1.5:  # noqa: PLR2004
        vpd_high = True
    if not vpd_high:
        return None
    return CouplingViolation(
        ec_id="EC-012",
        param_name=change.param_name,
        hard=True,
        reason=(
            f"Proposed feed-EC increase ({change.delta:+g}) while VPD is "
            "already high — combined osmotic + water stress; cannot run "
            "aggressive dryback and high feed EC together "
            "(EC-012 VPD<->Osmotic stress)."
        ),
    )


def check_ec013(
    change: ProposedChange, snapshot: Any, room_config: Any  # noqa: ARG001
) -> CouplingViolation | None:
    """``EC-013`` — a late irrigation shot spikes night RH.

    An irrigation event near lights-off spikes RH during the dark
    period. A change that pushes a shot within :data:`LATE_SHOT_MIN`
    minutes of lights-off is a hard refusal.
    """
    if change.param_name not in _SHOT_TIMING_PARAMS or not _is_increase(
        change
    ):
        return None
    minutes = _payload(snapshot).get("lights_off_in_minutes")
    if not isinstance(minutes, (int, float)) or minutes > LATE_SHOT_MIN:
        return None
    return CouplingViolation(
        ec_id="EC-013",
        param_name=change.param_name,
        hard=True,
        reason=(
            f"Proposed change to {change.param_name} ({change.delta:+g}) "
            f"pushes a shot within {minutes:g} min of lights-off (< "
            f"{LATE_SHOT_MIN} min) — RH spikes during the dark period "
            "(EC-013 Late irrigation<->Night RH)."
        ),
    )


def check_ec014(
    change: ProposedChange, snapshot: Any, room_config: Any  # noqa: ARG001
) -> CouplingViolation | None:
    """``EC-014`` — climate-deadband tuning needs validated sensor placement.

    Air-temp / RH deadband tuning depends on sensor location; bad
    placement causes oscillation or hides problems. A temp/RH setpoint
    change in a room whose sensor placement has not been validated is a
    fail-soft warning.
    """
    if change.param_name not in (_TEMP_PARAMS | _RH_PARAMS):
        return None
    if change.delta == 0 or change.direction == "no_change":
        return None
    cfg = _as_config(room_config)
    if cfg.sensor_placement_validated:
        return None
    return CouplingViolation(
        ec_id="EC-014",
        param_name=change.param_name,
        hard=False,
        reason=(
            f"Proposed change to {change.param_name} ({change.delta:+g}) "
            "but the room's climate-sensor placement is not validated — "
            "deadband tuning on a badly-placed sensor oscillates or hides "
            "problems (EC-014 Sensor placement<->AC stability, fail-soft)."
        ),
    )


def check_ec015(
    change: ProposedChange, snapshot: Any, room_config: Any  # noqa: ARG001
) -> CouplingViolation | None:
    """``EC-015`` — unfiltered intake imports pathogens.

    An unfiltered fresh-air intake imports pathogens / spores. A change
    that increases fresh-air exchange (CO2 or air velocity up) in a room
    with an exhaust but no intake filter is a fail-soft biosecurity
    warning.
    """
    if change.param_name not in (_CO2_PARAMS | _VELOCITY_PARAMS):
        return None
    if not _is_increase(change):
        return None
    cfg = _as_config(room_config)
    if cfg.has_intake_filter or not cfg.has_exhaust:
        return None
    return CouplingViolation(
        ec_id="EC-015",
        param_name=change.param_name,
        hard=False,
        reason=(
            f"Proposed change to {change.param_name} ({change.delta:+g}) "
            "increases fresh-air exchange but the intake is unfiltered — "
            "pathogen / spore ingress; filtration is required for "
            "biosecurity (EC-015 Exhaust<->Pathogen ingress, fail-soft)."
        ),
    )


#: Ordered registry of every coupling-rule check, keyed by ``EC-*`` id.
#: The check signature uses ``Any`` for the change because
#: :class:`~app.schemas.llm_decision.ProposedChange` is a
#: ``TYPE_CHECKING``-only import (avoids a runtime ``NameError`` here).
COUPLING_RULES: dict[
    str,
    Callable[[Any, Any, Any], CouplingViolation | None],
] = {
    "EC-001": check_ec001,
    "EC-002": check_ec002,
    "EC-003": check_ec003,
    "EC-004": check_ec004,
    "EC-005": check_ec005,
    "EC-006": check_ec006,
    "EC-007": check_ec007,
    "EC-008": check_ec008,
    "EC-009": check_ec009,
    "EC-010": check_ec010,
    "EC-011": check_ec011,
    "EC-012": check_ec012,
    "EC-013": check_ec013,
    "EC-014": check_ec014,
    "EC-015": check_ec015,
}


def check_coupling(
    change: ProposedChange,
    snapshot: Any,
    room_config: RoomConfig | dict[str, Any] | None,
) -> list[CouplingViolation]:
    """Run every coupling-rule check against one proposed change.

    Args:
        change: The :class:`~app.schemas.llm_decision.ProposedChange` to
            screen.
        snapshot: The :class:`~app.models.sensor_snapshot.SensorSnapshot`
            (or compatible double) the proposal was reasoned about.
        room_config: The room's static equipment / coupling map — a
            :class:`RoomConfig`, a config mapping, or ``None`` (an
            all-defaults config).

    Returns:
        Every :class:`CouplingViolation` that fired, in
        :data:`COUPLING_RULES` order. Empty when the change trips no
        coupling rule.
    """
    violations: list[CouplingViolation] = []
    for ec_id, check in COUPLING_RULES.items():
        violation = check(change, snapshot, room_config)
        if violation is not None:
            log.info(
                "coupling_violation",
                ec_id=ec_id,
                param_name=change.param_name,
                hard=violation.hard,
            )
            violations.append(violation)
    return violations
