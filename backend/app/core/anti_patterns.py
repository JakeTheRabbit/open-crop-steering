"""Anti-pattern checks — ``AP-01`` ... ``AP-12``.

Implements every anti-pattern from `cultivation_knowledge.md` Section 4.
An *anti-pattern* is a proposed change that the cultivation knowledge
base flags as counter-productive: useless (it masks a real problem),
wasteful (it fights itself), or actively harmful (combined crop stress).

The Phase 9 validator (:mod:`app.core.guardrails`) runs every check
against each free-form ``proposed_changes`` entry the LLM returns. A hit
means the proposal is rejected (or, in the SFW path, would fail the
re-check) — see plan locked decision #13's decision tree.

Each check is a **pure function** with the signature
``(change, snapshot) -> AntiPatternHit | None``:

* ``change`` — one :class:`~app.schemas.llm_decision.ProposedChange`
  (the parameter, direction and signed delta the model wants).
* ``snapshot`` — the :class:`~app.models.sensor_snapshot.SensorSnapshot`
  the proposal was reasoned about; the check reads its ``payload``
  (``equipment_status`` saturation flags, ``stale_sensors``, ``sensors``
  trends, ``setpoints`` and ``lights_off_in_minutes``).

A returned :class:`AntiPatternHit` carries the ``AP-*`` id and a
human-readable reason. Where the anti-pattern is *saturation-coupled*
(e.g. AP-02: lowering temp while the AC is already saturated) the hit's
``reason_codes`` carries **both** the ``SAT-*`` indicator id and the
``AP-*`` id — the plan requires a rejection to cite both (e.g.
``[SAT-AC, AP-02]``).

The :data:`ANTI_PATTERNS` registry + :func:`check_anti_patterns` run
every check and collect the hits.
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
# Anti-pattern thresholds (from cultivation_knowledge.md Section 4).
# Centralised so the tests reference the same constants the code uses.
# Public defaults — a facility tightens them in its private overrides.
# ---------------------------------------------------------------------------

#: AP-08 — an irrigation shot scheduled within this many minutes of
#: lights-off risks a night-time RH spike.
LATE_IRRIGATION_MIN = 30

#: AP-05 — CO2 enrichment below this PPFD is not light-limited enough to
#: pay off; the snapshot carries a per-stage threshold but this is the
#: conservative public floor used when the snapshot omits one.
DEFAULT_PPFD_CO2_THRESHOLD = 400.0

#: AP-10 — air velocity above this (m/s) is treated as beyond typical
#: plant tolerance (wind stress / edge burn). Public default.
AIR_VELOCITY_TOLERANCE = 1.0

#: A trend slope at/above this magnitude counts as "climbing" / "rising"
#: for the EC-trend anti-patterns (AP-06 / AP-07). Slopes are in
#: units/second; EC drifts slowly so the floor is deliberately tiny.
EC_CLIMBING_SLOPE = 0.0


@dataclass(slots=True)
class AntiPatternHit:
    """One anti-pattern fired against a proposed change.

    Attributes:
        ap_id: The ``AP-*`` id (e.g. ``"AP-02"``).
        reason: Human-readable explanation for logs / audit / the
            operator-facing rejection detail.
        reason_codes: Machine-actionable ids for the rejection's
            ``reason_codes`` list — always contains ``ap_id``, plus the
            coupled ``SAT-*`` indicator id when the anti-pattern is
            saturation-coupled (e.g. ``["SAT-AC", "AP-02"]``).
        param_name: The parameter the offending change targeted.
    """

    ap_id: str
    reason: str
    param_name: str
    reason_codes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # The AP id must always be present in reason_codes; add it last
        # so a saturation id (cited first) keeps its [SAT, AP] ordering.
        if self.ap_id not in self.reason_codes:
            self.reason_codes.append(self.ap_id)


# ---------------------------------------------------------------------------
# Snapshot-payload accessors. The snapshot payload shape is fixed by
# app.core.snapshot.build_snapshot; these readers tolerate a partial
# payload (older snapshot, hand-built test double) by failing safe.
# ---------------------------------------------------------------------------


def _payload(snapshot: Any) -> dict[str, Any]:
    """Return a snapshot's ``payload`` dict (empty if absent)."""
    return getattr(snapshot, "payload", {}) or {}


def _equipment_status(snapshot: Any) -> dict[str, str]:
    """Return ``payload["equipment_status"]`` — equipment id -> status."""
    return _payload(snapshot).get("equipment_status", {}) or {}


def _is_saturated(snapshot: Any, equipment_id: str) -> str | None:
    """Return the ``SAT-*`` code if ``equipment_id`` is saturated, else ``None``.

    ``equipment_status`` maps each equipment id to either its ``SAT-*``
    code (saturated), ``"ok"`` (evaluated, healthy) or ``"unmonitored"``.
    A value that starts with ``SAT-`` means the predicate fired.
    """
    status = _equipment_status(snapshot).get(equipment_id)
    if isinstance(status, str) and status.startswith("SAT-"):
        return status
    return None


def _sensors(snapshot: Any) -> dict[str, dict[str, Any]]:
    """Return ``payload["sensors"]`` — entity tag -> reading dict."""
    return _payload(snapshot).get("sensors", {}) or {}


def _stale_sensors(snapshot: Any) -> list[str]:
    """Return ``payload["stale_sensors"]`` — the stale entity-tag list."""
    return list(_payload(snapshot).get("stale_sensors", []) or [])


def _ec_trend_rising(snapshot: Any) -> bool:
    """Return whether any substrate / runoff EC sensor is trending up.

    The check is a coupling input for AP-06 / AP-07. It scans every
    sensor whose tag mentions ``ec`` and reports ``True`` if any has a
    positive trend slope, or its payload carries an explicit
    ``ec_trend == "rising"`` hint.
    """
    payload = _payload(snapshot)
    if payload.get("ec_trend") == "rising":
        return True
    for tag, reading in _sensors(snapshot).items():
        if "ec" not in tag.lower():
            continue
        slope = reading.get("trend_slope")
        if isinstance(slope, (int, float)) and slope > EC_CLIMBING_SLOPE:
            return True
    return False


def _runoff_low(snapshot: Any) -> bool:
    """Return whether runoff is flagged insufficient (AP-07 coupling input).

    The snapshot carries an optional ``runoff_pct`` hint; runoff below
    the rough 10% mandatory-minimum (see EC-011) is "insufficient". A
    missing hint is treated as not-low (fail-soft — AP-07 still needs
    the rising-EC condition to fire).
    """
    runoff = _payload(snapshot).get("runoff_pct")
    if isinstance(runoff, (int, float)):
        return runoff < 10.0  # noqa: PLR2004 — EC-011 rough minimum
    return False


def _lights_off_minutes(snapshot: Any) -> float | None:
    """Return minutes until lights-off, if the snapshot carries it."""
    value = _payload(snapshot).get("lights_off_in_minutes")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _ppfd_threshold(snapshot: Any) -> float:
    """Return the stage's PPFD threshold for CO2 ROI (AP-05)."""
    value = _payload(snapshot).get("ppfd_co2_threshold")
    if isinstance(value, (int, float)):
        return float(value)
    return DEFAULT_PPFD_CO2_THRESHOLD


def _current_ppfd(snapshot: Any) -> float | None:
    """Return the room's current PPFD — setpoint first, sensor fallback."""
    setpoints = _payload(snapshot).get("setpoints", {}) or {}
    for key in ("ppfd", "ppfd_day"):
        value = setpoints.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    for tag, reading in _sensors(snapshot).items():
        if "ppfd" in tag.lower():
            value = reading.get("value")
            if isinstance(value, (int, float)):
                return float(value)
    return None


def _has_disagreeing_sensors(snapshot: Any) -> bool:
    """Return whether the snapshot flags sibling-sensor disagreement.

    The snapshot may carry a ``disagreeing_sensors`` list (sensor pairs
    whose readings diverge beyond tolerance). Used by AP-11.
    """
    return bool(_payload(snapshot).get("disagreeing_sensors"))


# ---------------------------------------------------------------------------
# Per-parameter helpers — which proposed parameters each anti-pattern
# concerns. Names follow app.core.rollout.PARAM_CLASSES.
# ---------------------------------------------------------------------------

_TEMP_PARAMS = frozenset({"temp", "temp_day", "temp_night"})
_RH_PARAMS = frozenset({"rh", "rh_day", "rh_night"})
_CO2_PARAMS = frozenset({"co2", "co2_day"})
_PPFD_PARAMS = frozenset({"ppfd", "ppfd_day"})
_DRYBACK_PARAMS = frozenset({"dryback_pct"})
_RUNOFF_PARAMS = frozenset({"drain_pct", "runoff_pct"})
_EC_PARAMS = frozenset({"ec_target", "tank_ec"})
_VELOCITY_PARAMS = frozenset({"air_velocity"})
_IRRIGATION_PARAMS = frozenset(
    {"irrigation_freq", "shot_size", "last_shot_offset"}
)


def _is_increase(change: ProposedChange) -> bool:
    """Return whether a change raises its parameter."""
    return change.direction == "increase" or change.delta > 0


def _is_decrease(change: ProposedChange) -> bool:
    """Return whether a change lowers its parameter."""
    return change.direction == "decrease" or change.delta < 0


# ---------------------------------------------------------------------------
# The twelve anti-pattern checks. Each returns an AntiPatternHit or None.
# ---------------------------------------------------------------------------


def check_ap01(
    change: ProposedChange, snapshot: Any
) -> AntiPatternHit | None:
    """``AP-01`` — increase PPFD without HVAC headroom.

    Raising PPFD raises transpiration + leaf temp + HVAC load. If the AC
    is already saturated (``SAT-AC``) the room has no cooling headroom,
    so a PPFD increase is a heat trap / leaf-burn risk.
    """
    if change.param_name not in _PPFD_PARAMS or not _is_increase(change):
        return None
    sat = _is_saturated(snapshot, "ac")
    if sat is None:
        return None
    return AntiPatternHit(
        ap_id="AP-01",
        param_name=change.param_name,
        reason=(
            f"Proposed PPFD increase ({change.delta:+g}) while the AC is "
            f"saturated ({sat}) — no cooling headroom; heat-trap / "
            "leaf-burn risk (ppfd_increase_without_hvac_headroom)."
        ),
        reason_codes=[sat],
    )


def check_ap02(
    change: ProposedChange, snapshot: Any
) -> AntiPatternHit | None:
    """``AP-02`` — lower the temp setpoint when the AC is already saturated.

    The canonical saturation+anti-pattern combination. If ``SAT-AC`` has
    fired the AC is running flat-out and losing ground; cutting the
    setpoint cannot be met and only masks the real (hardware) problem.
    """
    if change.param_name not in _TEMP_PARAMS or not _is_decrease(change):
        return None
    sat = _is_saturated(snapshot, "ac")
    if sat is None:
        return None
    return AntiPatternHit(
        ap_id="AP-02",
        param_name=change.param_name,
        reason=(
            f"Proposed temp-setpoint cut ({change.delta:+g}) while the AC "
            f"is saturated ({sat}) — useless, the AC cannot reach the "
            "current target; masks a hardware constraint "
            "(lower_temp_when_ac_saturated)."
        ),
        reason_codes=[sat],
    )


def check_ap03(
    change: ProposedChange, snapshot: Any
) -> AntiPatternHit | None:
    """``AP-03`` — lower the RH setpoint when the dehumidifier is saturated.

    If ``SAT-DEHU`` has fired the dehumidifier is maxed and RH is still
    above target; cutting the setpoint cannot be met and masks the
    transpiration-source problem.
    """
    if change.param_name not in _RH_PARAMS or not _is_decrease(change):
        return None
    sat = _is_saturated(snapshot, "dehu")
    if sat is None:
        return None
    return AntiPatternHit(
        ap_id="AP-03",
        param_name=change.param_name,
        reason=(
            f"Proposed RH-setpoint cut ({change.delta:+g}) while the "
            f"dehumidifier is saturated ({sat}) — useless; investigate "
            "the transpiration source instead "
            "(lower_rh_when_dehu_saturated)."
        ),
        reason_codes=[sat],
    )


def check_ap04(
    change: ProposedChange, snapshot: Any
) -> AntiPatternHit | None:
    """``AP-04`` — increase CO2 when exhaust is active or pressure negative.

    CO2 enrichment with an active exhaust, or in a negative-pressure
    room (``SAT-PRESS``), is wasteful — the enrichment is diluted by
    outside air as fast as it is injected.
    """
    if change.param_name not in _CO2_PARAMS or not _is_increase(change):
        return None
    sat = _is_saturated(snapshot, "pressure")
    exhaust_active = bool(_payload(snapshot).get("exhaust_active"))
    if sat is None and not exhaust_active:
        return None
    codes = [sat] if sat is not None else []
    cause = (
        f"negative room pressure ({sat})"
        if sat is not None
        else "an active exhaust"
    )
    return AntiPatternHit(
        ap_id="AP-04",
        param_name=change.param_name,
        reason=(
            f"Proposed CO2 increase ({change.delta:+g}) with {cause} — "
            "enrichment is diluted by outside air; wasteful "
            "(co2_increase_with_active_exhaust)."
        ),
        reason_codes=codes,
    )


def check_ap05(
    change: ProposedChange, snapshot: Any
) -> AntiPatternHit | None:
    """``AP-05`` — increase CO2 when PPFD is below the stage threshold.

    CO2 enrichment only pays when light is *not* the limiting factor.
    Below the stage's PPFD threshold the extra CO2 yields no extra
    photosynthesis.
    """
    if change.param_name not in _CO2_PARAMS or not _is_increase(change):
        return None
    ppfd = _current_ppfd(snapshot)
    if ppfd is None:
        return None
    threshold = _ppfd_threshold(snapshot)
    if ppfd >= threshold:
        return None
    return AntiPatternHit(
        ap_id="AP-05",
        param_name=change.param_name,
        reason=(
            f"Proposed CO2 increase ({change.delta:+g}) while PPFD "
            f"{ppfd:g} is below the stage threshold {threshold:g} — light "
            "is limiting; no photosynthesis benefit "
            "(co2_increase_below_ppfd_threshold)."
        ),
    )


def check_ap06(
    change: ProposedChange, snapshot: Any
) -> AntiPatternHit | None:
    """``AP-06`` — increase dryback when substrate EC is already climbing.

    Faster dryback concentrates substrate EC further; doing it while EC
    is already rising stacks osmotic stress on the plant.
    """
    if change.param_name not in _DRYBACK_PARAMS or not _is_increase(change):
        return None
    if not _ec_trend_rising(snapshot):
        return None
    return AntiPatternHit(
        ap_id="AP-06",
        param_name=change.param_name,
        reason=(
            f"Proposed dryback increase ({change.delta:+g}) while substrate "
            "EC is climbing — faster dryback concentrates EC further; "
            "combined osmotic stress (dryback_increase_with_climbing_ec)."
        ),
    )


def check_ap07(
    change: ProposedChange, snapshot: Any
) -> AntiPatternHit | None:
    """``AP-07`` — decrease runoff when EC is accumulating.

    Runoff is what flushes accumulated salt. Cutting it while EC is
    rising (or runoff is already below the mandatory minimum) makes
    salt accumulation worse.
    """
    if change.param_name not in _RUNOFF_PARAMS or not _is_decrease(change):
        return None
    if not (_ec_trend_rising(snapshot) or _runoff_low(snapshot)):
        return None
    return AntiPatternHit(
        ap_id="AP-07",
        param_name=change.param_name,
        reason=(
            f"Proposed runoff/drain decrease ({change.delta:+g}) while EC "
            "is accumulating — runoff flushes salt; cutting it worsens "
            "accumulation (runoff_decrease_with_accumulating_ec)."
        ),
    )


def check_ap08(
    change: ProposedChange, snapshot: Any
) -> AntiPatternHit | None:
    """``AP-08`` — schedule irrigation within X min of lights-off.

    An irrigation event close to lights-off spikes RH during the dark
    period (the dehumidifier may be off-cycle and surfaces are cooling).
    Fires when the snapshot says lights-off is within
    :data:`LATE_IRRIGATION_MIN` minutes and the proposal would add /
    bring forward an irrigation event.
    """
    if change.param_name not in _IRRIGATION_PARAMS:
        return None
    # A later last-shot offset or more frequent irrigation both push a
    # shot closer to lights-off.
    if not _is_increase(change):
        return None
    minutes = _lights_off_minutes(snapshot)
    if minutes is None or minutes > LATE_IRRIGATION_MIN:
        return None
    return AntiPatternHit(
        ap_id="AP-08",
        param_name=change.param_name,
        reason=(
            f"Proposed change to {change.param_name} ({change.delta:+g}) "
            f"with lights-off only {minutes:g} min away (< "
            f"{LATE_IRRIGATION_MIN} min) — risks a night-period RH spike "
            "(late_irrigation_near_lights_off)."
        ),
    )


def check_ap09(
    change: ProposedChange, snapshot: Any
) -> AntiPatternHit | None:
    """``AP-09`` — lower RH without checking the night-temp dew point.

    Lowering an RH setpoint without confirming the coldest surface stays
    above the dew point can drive condensation. If ``SAT-DEW`` has
    fired (active condensation), any RH change made blind is unsafe.
    """
    if change.param_name not in _RH_PARAMS:
        return None
    # AP-09 concerns RH *changes* generally; a no-op change is harmless.
    if change.delta == 0 or change.direction == "no_change":
        return None
    sat = _is_saturated(snapshot, "condensation")
    if sat is None:
        return None
    return AntiPatternHit(
        ap_id="AP-09",
        param_name=change.param_name,
        reason=(
            f"Proposed RH change ({change.delta:+g}) while active "
            f"condensation is flagged ({sat}) — the dew-point margin has "
            "not been cleared; condensation-trap risk "
            "(rh_change_without_dewpoint_check)."
        ),
        reason_codes=[sat],
    )


def check_ap10(
    change: ProposedChange, snapshot: Any
) -> AntiPatternHit | None:
    """``AP-10`` — increase air velocity beyond plant tolerance.

    Air velocity above the plant-tolerance ceiling causes wind stress /
    edge burn. Fires when the proposal raises ``air_velocity`` and the
    resulting value would exceed :data:`AIR_VELOCITY_TOLERANCE`.
    """
    if change.param_name not in _VELOCITY_PARAMS or not _is_increase(change):
        return None
    setpoints = _payload(snapshot).get("setpoints", {}) or {}
    current = setpoints.get("air_velocity")
    resulting = (
        float(current) + change.delta
        if isinstance(current, (int, float))
        else change.delta
    )
    if resulting <= AIR_VELOCITY_TOLERANCE:
        return None
    return AntiPatternHit(
        ap_id="AP-10",
        param_name=change.param_name,
        reason=(
            f"Proposed air-velocity increase ({change.delta:+g}) takes it "
            f"to ~{resulting:g} m/s, beyond the {AIR_VELOCITY_TOLERANCE:g} "
            "m/s plant-tolerance ceiling — wind-stress / edge-burn risk "
            "(air_velocity_excessive)."
        ),
    )


def check_ap11(
    change: ProposedChange, snapshot: Any
) -> AntiPatternHit | None:
    """``AP-11`` — propose a change based on a stale / disagreeing sensor.

    A decision is only as good as its inputs. If the parameter the
    change targets is backed by a sensor flagged stale, or the snapshot
    flags sibling-sensor disagreement, the proposal rests on bad data.
    """
    if change.delta == 0 or change.direction == "no_change":
        return None
    stale = _stale_sensors(snapshot)
    # Match a stale sensor tag against the changed parameter loosely —
    # the tag typically contains the measurement name.
    param_token = change.param_name.split("_")[0].lower()
    stale_hit = next(
        (tag for tag in stale if param_token and param_token in tag.lower()),
        None,
    )
    disagreeing = _has_disagreeing_sensors(snapshot)
    if stale_hit is None and not disagreeing:
        return None
    cause = (
        f"sensor '{stale_hit}' is stale"
        if stale_hit is not None
        else "sibling sensors disagree"
    )
    return AntiPatternHit(
        ap_id="AP-11",
        param_name=change.param_name,
        reason=(
            f"Proposed change to {change.param_name} ({change.delta:+g}) "
            f"but {cause} — the decision rests on bad data "
            "(proposal_on_stale_or_disagreeing_sensor)."
        ),
    )


def check_ap12(
    change: ProposedChange, snapshot: Any
) -> AntiPatternHit | None:
    """``AP-12`` — recommend higher feed EC while dryback is already high.

    High feed EC and aggressive dryback both raise root-zone osmotic
    stress; running them together combines osmotic + water stress. The
    snapshot carries a ``dryback_high`` hint (or a high ``dryback_pct``
    setpoint) for the coupling condition.
    """
    if change.param_name not in _EC_PARAMS or not _is_increase(change):
        return None
    payload = _payload(snapshot)
    dryback_high = bool(payload.get("dryback_high"))
    setpoints = payload.get("setpoints", {}) or {}
    dryback_pct = setpoints.get("dryback_pct")
    if isinstance(dryback_pct, (int, float)) and dryback_pct >= 50.0:  # noqa: PLR2004
        dryback_high = True
    if not dryback_high:
        return None
    return AntiPatternHit(
        ap_id="AP-12",
        param_name=change.param_name,
        reason=(
            f"Proposed feed-EC increase ({change.delta:+g}) while dryback "
            "is already aggressive — combined osmotic + water stress "
            "(simultaneous_high_ec_high_dryback)."
        ),
    )


#: Ordered registry of every anti-pattern check, keyed by ``AP-*`` id.
#: The check signature uses ``Any`` for the change because
#: :class:`~app.schemas.llm_decision.ProposedChange` is a
#: ``TYPE_CHECKING``-only import (avoids a runtime ``NameError`` here).
ANTI_PATTERNS: dict[
    str, Callable[[Any, Any], AntiPatternHit | None]
] = {
    "AP-01": check_ap01,
    "AP-02": check_ap02,
    "AP-03": check_ap03,
    "AP-04": check_ap04,
    "AP-05": check_ap05,
    "AP-06": check_ap06,
    "AP-07": check_ap07,
    "AP-08": check_ap08,
    "AP-09": check_ap09,
    "AP-10": check_ap10,
    "AP-11": check_ap11,
    "AP-12": check_ap12,
}


def check_anti_patterns(
    change: ProposedChange, snapshot: Any
) -> list[AntiPatternHit]:
    """Run every anti-pattern check against one proposed change.

    Args:
        change: The :class:`~app.schemas.llm_decision.ProposedChange` to
            screen.
        snapshot: The :class:`~app.models.sensor_snapshot.SensorSnapshot`
            (or compatible double) the proposal was reasoned about.

    Returns:
        Every :class:`AntiPatternHit` that fired, in :data:`ANTI_PATTERNS`
        order. Empty when the change trips no anti-pattern.
    """
    hits: list[AntiPatternHit] = []
    for ap_id, check in ANTI_PATTERNS.items():
        hit = check(change, snapshot)
        if hit is not None:
            log.info(
                "anti_pattern_hit",
                ap_id=ap_id,
                param_name=change.param_name,
                reason_codes=hit.reason_codes,
            )
            hits.append(hit)
    return hits
