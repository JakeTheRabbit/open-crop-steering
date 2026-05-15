"""Equipment-saturation predicates (``SAT-AC`` ... ``SAT-DEW``).

Implements every saturation indicator from
`cultivation_knowledge.md` Section 3. Each predicate turns a pattern of
sensor evidence into a structured :class:`SaturationResult`; when one
fires, the equipment is "saturated" — running flat-out and *still* not
holding its target — and later phases must stop proposing a more
aggressive setpoint for that equipment's domain.

Indicator -> outcome map (knowledge base Section 3):

======== ============ ============================ =========
ID       Equipment    Pattern (sensor evidence)    Severity
======== ============ ============================ =========
SAT-AC   AC           fan high >=15m, temp > set,  warning
                      temp slope > 0
SAT-DEHU dehumidifier dehu on >=20m, RH > set+3%,  warning
                      RH slope flat/positive
SAT-CO2  CO2 inject   CO2 < 0.9x set >=5m,         warning
                      solenoid duty > 50%
SAT-FAN  circulation  any fan at 10/10 >=15m,      warning
                      stratification measurable
SAT-IRRIG irrigation  VWC delta after shot < 30%   critical
                      of expected
SAT-PRESS pressure    room dP < -5 Pa, CO2         warning
                      enrichment active
SAT-DEW  condensation coldest surface < dew point  critical
                      >=5m
======== ============ ============================ =========

``SAT-IRRIG`` and ``SAT-DEW`` escalate to *critical* because they imply
direct crop / process harm (an unresponsive emitter must block further
auto-shots; active condensation must block any RH-up or temp-down
proposal). The other five are *warning*: the equipment is maxed but the
room is not being damaged, so the right move is to relax the target, not
to alarm.

Every result embeds its ``SAT-*`` id in ``reason_code`` so Phase 7+
supervisors can cite it directly (e.g. a rejection carrying
``[SAT-AC, AP-02]``).

InfluxDB coupling
-----------------
Predicates depend only on the injected client's four query helpers
(:meth:`~app.influx_client.InfluxClient.current_value`,
:meth:`~app.influx_client.InfluxClient.trend_slope`,
:meth:`~app.influx_client.InfluxClient.at_threshold_for`,
:meth:`~app.influx_client.InfluxClient.delta_post_event`). The client is
passed in, never constructed here, so tests inject a synthetic double.
Influx ``entity_id`` tags are bare (no ``sensor.`` domain prefix); the
:class:`RoomContext` carries the tag values per room.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

import structlog

from app.influx_client import ThresholdOp
from app.models.event_log import EventSeverity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Saturation-pattern thresholds (from cultivation_knowledge.md Section 3).
# Centralised so the tests can reference the same constants the code uses.
# ---------------------------------------------------------------------------

#: SAT-AC — climate fan must read "high" for at least this long.
AC_FAN_HIGH_MIN = 15
#: SAT-AC — fan-mode is encoded numerically; >= this counts as "high".
AC_FAN_HIGH_LEVEL = 3.0

#: SAT-DEHU — dehumidifier must be running for at least this long.
DEHU_ON_MIN = 20
#: SAT-DEHU — RH must exceed setpoint by at least this many %RH.
DEHU_RH_OVERSHOOT = 3.0

#: SAT-CO2 — CO2 must sit below this fraction of setpoint.
CO2_DEFICIT_FRACTION = 0.9
#: SAT-CO2 — ...for at least this long.
CO2_DEFICIT_MIN = 5
#: SAT-CO2 — solenoid duty cycle over the window must exceed this fraction.
CO2_SOLENOID_DUTY = 0.5

#: SAT-FAN — an AC-Infinity fan must sit at this intensity (10/10).
FAN_MAX_INTENSITY = 10.0
#: SAT-FAN — ...for at least this long.
FAN_MAX_MIN = 15
#: SAT-FAN — temp/RH stratification above this (in sensor units) still counts.
FAN_STRATIFICATION_EPSILON = 0.5

#: SAT-IRRIG — measured VWC rise below this fraction of the expected rise
#: means the shot did not land.
IRRIG_RESPONSE_FRACTION = 0.3
#: SAT-IRRIG — window after the shot over which the VWC delta is measured.
IRRIG_RESPONSE_MIN = 10

#: SAT-PRESS — room differential pressure at/below this (Pa) is "negative".
PRESS_NEGATIVE_PA = -5.0
#: SAT-PRESS — ...sampled over this trailing window.
PRESS_WINDOW_MIN = 5

#: SAT-DEW — coldest surface within this margin (degC) of dew point counts
#: as condensing. 0.0 == surface must actually reach the dew point.
DEW_MARGIN_C = 0.0
#: SAT-DEW — ...sustained for at least this long.
DEW_SUSTAINED_MIN = 5


class _InfluxLike(Protocol):
    """Structural type for the ``InfluxClient`` surface used by predicates."""

    def current_value(self, entity: str) -> float | None:  # pragma: no cover
        ...

    def trend_slope(
        self, entity: str, window: timedelta | str
    ) -> float | None:  # pragma: no cover
        ...

    def at_threshold_for(
        self,
        entity: str,
        op: ThresholdOp,
        threshold: float,
        duration: timedelta | str,
    ) -> bool:  # pragma: no cover
        ...

    def delta_post_event(
        self, entity: str, event_ts: str, window: timedelta | str
    ) -> float | None:  # pragma: no cover
        ...


@dataclass(slots=True)
class FiredShot:
    """A just-fired irrigation shot, for the ``SAT-IRRIG`` response check.

    Attributes:
        zone_id: Zone the shot targeted.
        vwc_entity: InfluxDB ``entity_id`` tag of that zone's VWC sensor.
        fired_at: RFC3339 timestamp the shot fired (window start).
        volume_ml: Shot volume in millilitres.
        substrate_volume_ml: Substrate volume the shot was applied to, in
            millilitres. The expected VWC rise is
            ``volume_ml / substrate_volume_ml * 100`` (%VWC).
    """

    zone_id: str
    vwc_entity: str
    fired_at: str
    volume_ml: float
    substrate_volume_ml: float

    @property
    def expected_vwc_delta(self) -> float:
        """Expected %VWC rise for this shot (knowledge base SAT-IRRIG)."""
        if self.substrate_volume_ml <= 0:
            return 0.0
        return self.volume_ml / self.substrate_volume_ml * 100.0


@dataclass(slots=True)
class RoomContext:
    """Per-room sensor entities + setpoints a predicate set needs.

    Entity names are InfluxDB ``entity_id`` tag values (bare, no domain
    prefix). A field left ``None`` / empty means that equipment is not
    mapped for the room, and the corresponding predicate is skipped (it
    returns a non-saturated "not evaluated" result rather than firing).

    Attributes:
        room_id: Room identifier.
        temp_actual_entity: Air-temperature sensor tag.
        temp_setpoint: Effective air-temp target (degC).
        ac_fan_entity: Climate fan-mode sensor tag (numeric encoding).
        rh_actual_entity: Relative-humidity sensor tag.
        rh_setpoint: Effective RH target (%RH).
        dehu_switch_entity: Dehumidifier on/off sensor tag (1/0).
        co2_actual_entity: CO2 sensor tag.
        co2_setpoint: Effective CO2 target (ppm).
        co2_solenoid_entity: CO2 solenoid on/off sensor tag (1/0).
        fan_intensity_entities: AC-Infinity fan intensity sensor tags.
        stratification_entities: Sensor-pair tags whose spread measures
            temp/RH stratification; each tuple is ``(top, bottom)``.
        pressure_diff_entity: Room differential-pressure sensor tag (Pa).
        co2_enrichment_active: Whether CO2 enrichment is currently on.
        coldest_surface_entity: Coldest-surface temperature sensor tag.
        dew_point_entity: Room dew-point sensor tag.
        fired_shots: Irrigation shots fired this evaluation cycle.
    """

    room_id: str
    temp_actual_entity: str | None = None
    temp_setpoint: float | None = None
    ac_fan_entity: str | None = None

    rh_actual_entity: str | None = None
    rh_setpoint: float | None = None
    dehu_switch_entity: str | None = None

    co2_actual_entity: str | None = None
    co2_setpoint: float | None = None
    co2_solenoid_entity: str | None = None

    fan_intensity_entities: list[str] = field(default_factory=list)
    stratification_entities: list[tuple[str, str]] = field(
        default_factory=list
    )

    pressure_diff_entity: str | None = None
    co2_enrichment_active: bool = False

    coldest_surface_entity: str | None = None
    dew_point_entity: str | None = None

    fired_shots: list[FiredShot] = field(default_factory=list)


@dataclass(slots=True)
class SaturationResult:
    """Outcome of evaluating one saturation indicator.

    Attributes:
        indicator_id: The ``SAT-*`` id (e.g. ``"SAT-AC"``).
        saturated: ``True`` iff the saturation pattern fired.
        severity: :class:`~app.models.event_log.EventSeverity` to attach
            when saturated — ``critical`` for ``SAT-IRRIG`` / ``SAT-DEW``,
            ``warning`` otherwise. Always ``info`` when not saturated.
        detail: Human-readable explanation for logs / dashboards.
        reason_code: The indicator id, echoed so callers can splice it
            straight into an event/audit ``reason_codes`` list.
        evaluated: ``False`` when the room lacks the entities this
            indicator needs (predicate skipped, never saturated).
    """

    indicator_id: str
    saturated: bool
    severity: EventSeverity
    detail: str
    reason_code: str
    evaluated: bool = True


#: Indicators that escalate straight to CRITICAL when they fire.
CRITICAL_INDICATORS: frozenset[str] = frozenset({"SAT-IRRIG", "SAT-DEW"})


def _severity_for(indicator_id: str) -> EventSeverity:
    """Return the saturated-state severity for an indicator id."""
    return (
        EventSeverity.critical
        if indicator_id in CRITICAL_INDICATORS
        else EventSeverity.warning
    )


def _not_saturated(indicator_id: str, detail: str) -> SaturationResult:
    """Build a non-saturated (but evaluated) result."""
    return SaturationResult(
        indicator_id=indicator_id,
        saturated=False,
        severity=EventSeverity.info,
        detail=detail,
        reason_code=indicator_id,
    )


def _skipped(indicator_id: str, detail: str) -> SaturationResult:
    """Build a not-evaluated result (equipment not mapped for the room)."""
    return SaturationResult(
        indicator_id=indicator_id,
        saturated=False,
        severity=EventSeverity.info,
        detail=detail,
        reason_code=indicator_id,
        evaluated=False,
    )


def _saturated(indicator_id: str, detail: str) -> SaturationResult:
    """Build a saturated result with the indicator's mapped severity."""
    return SaturationResult(
        indicator_id=indicator_id,
        saturated=True,
        severity=_severity_for(indicator_id),
        detail=detail,
        reason_code=indicator_id,
    )


# ---------------------------------------------------------------------------
# The seven predicates.
# ---------------------------------------------------------------------------


async def evaluate_sat_ac(  # noqa: PLR0911 — guard chain, one return per check
    influx: _InfluxLike, ctx: RoomContext
) -> SaturationResult:
    """``SAT-AC`` — air-conditioner saturated.

    Fires when the climate fan has read "high" for >= :data:`AC_FAN_HIGH_MIN`
    minutes **and** measured air temperature is above setpoint **and** the
    temperature slope is positive (still climbing). All three together
    mean the AC is doing everything it can and losing ground.

    Args:
        influx: Sensor reader.
        ctx: Room context (needs ``ac_fan_entity``, ``temp_actual_entity``,
            ``temp_setpoint``).

    Returns:
        A :class:`SaturationResult` for ``SAT-AC``.
    """
    indicator = "SAT-AC"
    if not (ctx.ac_fan_entity and ctx.temp_actual_entity):
        return _skipped(indicator, "AC / temp sensors not mapped")
    if ctx.temp_setpoint is None:
        return _skipped(indicator, "no temp setpoint")

    fan_high = influx.at_threshold_for(
        ctx.ac_fan_entity,
        "gte",
        AC_FAN_HIGH_LEVEL,
        timedelta(minutes=AC_FAN_HIGH_MIN),
    )
    if not fan_high:
        return _not_saturated(
            indicator, f"AC fan not continuously high for {AC_FAN_HIGH_MIN}m"
        )

    temp = influx.current_value(ctx.temp_actual_entity)
    if temp is None:
        return _not_saturated(indicator, "no current temperature reading")
    if temp <= ctx.temp_setpoint:
        return _not_saturated(
            indicator, f"temp {temp} within/below setpoint {ctx.temp_setpoint}"
        )

    slope = influx.trend_slope(
        ctx.temp_actual_entity, timedelta(minutes=AC_FAN_HIGH_MIN)
    )
    if slope is None or slope <= 0:
        return _not_saturated(
            indicator, f"temp not climbing (slope={slope})"
        )

    return _saturated(
        indicator,
        (
            f"AC fan high >={AC_FAN_HIGH_MIN}m, temp {temp} > "
            f"setpoint {ctx.temp_setpoint}, slope {slope:+.4f}/s"
        ),
    )


async def evaluate_sat_dehu(  # noqa: PLR0911 — guard chain, one return per check
    influx: _InfluxLike, ctx: RoomContext
) -> SaturationResult:
    """``SAT-DEHU`` — dehumidifier saturated.

    Fires when the dehumidifier has been on for >= :data:`DEHU_ON_MIN`
    minutes **and** measured RH is above ``setpoint + 3 %RH`` **and** the
    RH slope is flat or positive (not recovering).

    Args:
        influx: Sensor reader.
        ctx: Room context (needs ``dehu_switch_entity``,
            ``rh_actual_entity``, ``rh_setpoint``).

    Returns:
        A :class:`SaturationResult` for ``SAT-DEHU``.
    """
    indicator = "SAT-DEHU"
    if not (ctx.dehu_switch_entity and ctx.rh_actual_entity):
        return _skipped(indicator, "dehu / RH sensors not mapped")
    if ctx.rh_setpoint is None:
        return _skipped(indicator, "no RH setpoint")

    dehu_on = influx.at_threshold_for(
        ctx.dehu_switch_entity,
        "gte",
        1.0,
        timedelta(minutes=DEHU_ON_MIN),
    )
    if not dehu_on:
        return _not_saturated(
            indicator, f"dehu not continuously on for {DEHU_ON_MIN}m"
        )

    rh = influx.current_value(ctx.rh_actual_entity)
    if rh is None:
        return _not_saturated(indicator, "no current RH reading")
    overshoot_target = ctx.rh_setpoint + DEHU_RH_OVERSHOOT
    if rh <= overshoot_target:
        return _not_saturated(
            indicator,
            f"RH {rh} within setpoint+{DEHU_RH_OVERSHOOT} ({overshoot_target})",
        )

    slope = influx.trend_slope(
        ctx.rh_actual_entity, timedelta(minutes=DEHU_ON_MIN)
    )
    if slope is None or slope < 0:
        return _not_saturated(
            indicator, f"RH recovering (slope={slope})"
        )

    return _saturated(
        indicator,
        (
            f"dehu on >={DEHU_ON_MIN}m, RH {rh} > setpoint+"
            f"{DEHU_RH_OVERSHOOT} ({overshoot_target}), slope {slope:+.4f}/s"
        ),
    )


async def evaluate_sat_co2(
    influx: _InfluxLike, ctx: RoomContext
) -> SaturationResult:
    """``SAT-CO2`` — CO2 injection at its limit.

    Fires when CO2 has held below ``0.9 x setpoint`` for >=
    :data:`CO2_DEFICIT_MIN` minutes **and** the solenoid duty cycle over
    that window exceeds 50%. Means the supply is maxed or the room is too
    leaky to hold enrichment.

    Args:
        influx: Sensor reader.
        ctx: Room context (needs ``co2_actual_entity``, ``co2_setpoint``,
            ``co2_solenoid_entity``).

    Returns:
        A :class:`SaturationResult` for ``SAT-CO2``.
    """
    indicator = "SAT-CO2"
    if not (ctx.co2_actual_entity and ctx.co2_solenoid_entity):
        return _skipped(indicator, "CO2 / solenoid sensors not mapped")
    if ctx.co2_setpoint is None:
        return _skipped(indicator, "no CO2 setpoint")

    deficit_threshold = CO2_DEFICIT_FRACTION * ctx.co2_setpoint
    window = timedelta(minutes=CO2_DEFICIT_MIN)
    below = influx.at_threshold_for(
        ctx.co2_actual_entity, "lt", deficit_threshold, window
    )
    if not below:
        return _not_saturated(
            indicator,
            f"CO2 not continuously below {deficit_threshold} for "
            f"{CO2_DEFICIT_MIN}m",
        )

    # Solenoid sensor is 1/0; estimate its duty cycle over the window.
    duty_cycle = _solenoid_duty(influx, ctx.co2_solenoid_entity, window)
    if duty_cycle is None or duty_cycle <= CO2_SOLENOID_DUTY:
        return _not_saturated(
            indicator,
            f"solenoid duty {duty_cycle} not above {CO2_SOLENOID_DUTY}",
        )

    return _saturated(
        indicator,
        (
            f"CO2 < {deficit_threshold:.0f} (0.9x setpoint) for "
            f">={CO2_DEFICIT_MIN}m, solenoid duty {duty_cycle:.2f}"
        ),
    )


def _solenoid_duty(
    influx: _InfluxLike, entity: str, window: timedelta
) -> float | None:
    """Estimate a 1/0 solenoid's duty cycle over ``window``.

    The solenoid sensor reports ``1`` while open and ``0`` while shut.
    The duty cycle is the fraction of the window it was open; we count
    "open" readings against all readings using
    :meth:`~app.influx_client.InfluxClient.at_threshold_for`'s building
    block is not exposed, so this samples the trailing window: a fully
    open solenoid reads ``1`` throughout (``at_threshold_for >= 1`` is
    true), a fully shut one never (``at_threshold_for`` false). For the
    in-between case we fall back to the current value as a coarse proxy.
    """
    fully_open = influx.at_threshold_for(entity, "gte", 1.0, window)
    if fully_open:
        return 1.0
    current = influx.current_value(entity)
    if current is None:
        return None
    # Coarse: the solenoid is not continuously open, so treat the latest
    # state as the representative duty (0 shut / 1 open). Better duty
    # math arrives with a dedicated Influx helper (WIRING NEEDED).
    return float(current)


async def evaluate_sat_fan(
    influx: _InfluxLike, ctx: RoomContext
) -> SaturationResult:
    """``SAT-FAN`` — circulation fans maxed.

    Fires when any AC-Infinity fan intensity has sat at 10/10 for >=
    :data:`FAN_MAX_MIN` minutes **and** temp/RH stratification is still
    measurable (a sensor pair still differs by more than
    :data:`FAN_STRATIFICATION_EPSILON`).

    Args:
        influx: Sensor reader.
        ctx: Room context (needs ``fan_intensity_entities`` and at least
            one ``stratification_entities`` pair).

    Returns:
        A :class:`SaturationResult` for ``SAT-FAN``.
    """
    indicator = "SAT-FAN"
    if not ctx.fan_intensity_entities or not ctx.stratification_entities:
        return _skipped(indicator, "fans / stratification probes not mapped")

    window = timedelta(minutes=FAN_MAX_MIN)
    maxed = next(
        (
            fan
            for fan in ctx.fan_intensity_entities
            if influx.at_threshold_for(fan, "gte", FAN_MAX_INTENSITY, window)
        ),
        None,
    )
    if maxed is None:
        return _not_saturated(
            indicator, f"no fan continuously at 10/10 for {FAN_MAX_MIN}m"
        )

    spread = _max_stratification(influx, ctx.stratification_entities)
    if spread is None:
        return _not_saturated(indicator, "stratification not measurable")
    if spread <= FAN_STRATIFICATION_EPSILON:
        return _not_saturated(
            indicator,
            f"stratification {spread:.2f} within {FAN_STRATIFICATION_EPSILON}",
        )

    return _saturated(
        indicator,
        (
            f"fan {maxed} at 10/10 >={FAN_MAX_MIN}m, stratification "
            f"{spread:.2f} still measurable"
        ),
    )


def _max_stratification(
    influx: _InfluxLike, pairs: list[tuple[str, str]]
) -> float | None:
    """Return the largest absolute spread across stratification sensor pairs."""
    spreads: list[float] = []
    for top, bottom in pairs:
        top_v = influx.current_value(top)
        bottom_v = influx.current_value(bottom)
        if top_v is None or bottom_v is None:
            continue
        spreads.append(abs(top_v - bottom_v))
    return max(spreads) if spreads else None


async def evaluate_sat_irrig(
    influx: _InfluxLike, ctx: RoomContext
) -> SaturationResult:
    """``SAT-IRRIG`` — irrigation unresponsive (CRITICAL).

    For each shot fired this cycle, the measured VWC rise in the
    :data:`IRRIG_RESPONSE_MIN`-minute window after the shot is compared to
    the expected rise (``volume / substrate_volume x 100``). If the
    measured rise is below :data:`IRRIG_RESPONSE_FRACTION` (30%) of
    expected, the emitter / valve / pump is not delivering and the result
    escalates to CRITICAL — Phase 9 must block further auto-shots.

    Args:
        influx: Sensor reader.
        ctx: Room context (needs ``fired_shots``).

    Returns:
        A :class:`SaturationResult` for ``SAT-IRRIG``. CRITICAL severity
        when saturated.
    """
    indicator = "SAT-IRRIG"
    if not ctx.fired_shots:
        return _skipped(indicator, "no shots fired this cycle")

    for shot in ctx.fired_shots:
        expected = shot.expected_vwc_delta
        if expected <= 0:
            continue
        measured = influx.delta_post_event(
            shot.vwc_entity,
            shot.fired_at,
            timedelta(minutes=IRRIG_RESPONSE_MIN),
        )
        if measured is None:
            return _saturated(
                indicator,
                (
                    f"zone {shot.zone_id}: no VWC data after shot at "
                    f"{shot.fired_at} — emitter/valve/pump suspect"
                ),
            )
        if measured < IRRIG_RESPONSE_FRACTION * expected:
            return _saturated(
                indicator,
                (
                    f"zone {shot.zone_id}: VWC rose {measured:.2f}%, "
                    f"expected ~{expected:.2f}% "
                    f"({measured / expected * 100:.0f}% of expected) — "
                    "irrigation unresponsive"
                ),
            )

    return _not_saturated(indicator, "all fired shots produced expected VWC rise")


async def evaluate_sat_press(
    influx: _InfluxLike, ctx: RoomContext
) -> SaturationResult:
    """``SAT-PRESS`` — negative pressure fighting CO2 enrichment.

    Fires when the room differential pressure has held at/below
    :data:`PRESS_NEGATIVE_PA` (-5 Pa) **and** CO2 enrichment is active.
    Negative pressure pulls in outside air that dilutes the enrichment.

    Args:
        influx: Sensor reader.
        ctx: Room context (needs ``pressure_diff_entity`` and
            ``co2_enrichment_active``).

    Returns:
        A :class:`SaturationResult` for ``SAT-PRESS``.
    """
    indicator = "SAT-PRESS"
    if not ctx.pressure_diff_entity:
        return _skipped(indicator, "pressure-differential sensor not mapped")
    if not ctx.co2_enrichment_active:
        return _not_saturated(indicator, "CO2 enrichment not active")

    negative = influx.at_threshold_for(
        ctx.pressure_diff_entity,
        "lte",
        PRESS_NEGATIVE_PA,
        timedelta(minutes=PRESS_WINDOW_MIN),
    )
    if not negative:
        return _not_saturated(
            indicator,
            f"pressure not continuously <= {PRESS_NEGATIVE_PA} Pa",
        )

    current = influx.current_value(ctx.pressure_diff_entity)
    return _saturated(
        indicator,
        (
            f"room pressure {current} Pa (<= {PRESS_NEGATIVE_PA}) while CO2 "
            "enrichment active — outside air diluting enrichment"
        ),
    )


async def evaluate_sat_dew(
    influx: _InfluxLike, ctx: RoomContext
) -> SaturationResult:
    """``SAT-DEW`` — active condensation risk (CRITICAL).

    Fires when the coldest tracked surface temperature has sat at/below
    the room dew point (within :data:`DEW_MARGIN_C`) for >=
    :data:`DEW_SUSTAINED_MIN` minutes. Water is condensing on that
    surface; the result escalates to CRITICAL — Phase 9 must block any
    RH-raising or temp-lowering proposal.

    Implementation: a synthetic ``surface_minus_dew = coldest_surface -
    dew_point`` series is not available, so this reads the *current*
    margin and confirms it via a sustained threshold on the coldest
    surface against the current dew point (a conservative proxy — dew
    point moves slowly relative to the 5-minute window).

    Args:
        influx: Sensor reader.
        ctx: Room context (needs ``coldest_surface_entity`` and
            ``dew_point_entity``).

    Returns:
        A :class:`SaturationResult` for ``SAT-DEW``. CRITICAL severity
        when saturated.
    """
    indicator = "SAT-DEW"
    if not (ctx.coldest_surface_entity and ctx.dew_point_entity):
        return _skipped(indicator, "surface / dew-point sensors not mapped")

    surface = influx.current_value(ctx.coldest_surface_entity)
    dew_point = influx.current_value(ctx.dew_point_entity)
    if surface is None or dew_point is None:
        return _not_saturated(indicator, "no current surface / dew-point reading")

    margin = surface - dew_point
    if margin > DEW_MARGIN_C:
        return _not_saturated(
            indicator,
            f"coldest surface {surface} is {margin:+.2f}degC above dew point",
        )

    # Confirm it is not a one-sample blip: the surface must have held
    # at/below (dew_point + margin) for the sustained window.
    sustained = influx.at_threshold_for(
        ctx.coldest_surface_entity,
        "lte",
        dew_point + DEW_MARGIN_C,
        timedelta(minutes=DEW_SUSTAINED_MIN),
    )
    if not sustained:
        return _not_saturated(
            indicator,
            f"surface at/below dew point but not sustained {DEW_SUSTAINED_MIN}m",
        )

    return _saturated(
        indicator,
        (
            f"coldest surface {surface}degC at/below dew point {dew_point}degC "
            f"for >={DEW_SUSTAINED_MIN}m — active condensation"
        ),
    )


#: Ordered registry of every predicate, keyed by indicator id.
_PREDICATES: dict[
    str, Callable[[_InfluxLike, RoomContext], Awaitable[SaturationResult]]
] = {
    "SAT-AC": evaluate_sat_ac,
    "SAT-DEHU": evaluate_sat_dehu,
    "SAT-CO2": evaluate_sat_co2,
    "SAT-FAN": evaluate_sat_fan,
    "SAT-IRRIG": evaluate_sat_irrig,
    "SAT-PRESS": evaluate_sat_press,
    "SAT-DEW": evaluate_sat_dew,
}


async def evaluate_all(
    influx: _InfluxLike, room_context: RoomContext
) -> list[SaturationResult]:
    """Run every saturation predicate for a room.

    Predicates whose equipment is not mapped return an *unevaluated*
    result (``evaluated=False``) rather than being omitted, so the caller
    sees the full 7-indicator picture.

    Args:
        influx: Sensor reader (:class:`~app.influx_client.InfluxClient`
            or compatible double).
        room_context: The room's sensor entities + setpoints.

    Returns:
        A list of seven :class:`SaturationResult` objects, one per
        indicator, in :data:`_PREDICATES` order.
    """
    results: list[SaturationResult] = []
    for indicator_id, predicate in _PREDICATES.items():
        result = await predicate(influx, room_context)
        if result.saturated:
            log.warning(
                "equipment_saturated",
                room_id=room_context.room_id,
                indicator=indicator_id,
                severity=result.severity.value,
                detail=result.detail,
            )
        results.append(result)
    return results
