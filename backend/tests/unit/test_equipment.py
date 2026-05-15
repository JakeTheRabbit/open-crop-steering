"""Unit tests for :mod:`app.core.equipment` — the 7 saturation predicates.

Each predicate is exercised against :class:`FakeInflux`, a synthetic
double whose four query helpers return per-entity scripted data. Tests
assert each indicator fires (``saturated=True``) at the thresholds
documented in ``cultivation_knowledge.md`` Section 3 and stays quiet just
inside them — and that ``SAT-IRRIG`` / ``SAT-DEW`` carry CRITICAL
severity while the other five carry WARNING.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from app.core.equipment import (
    CRITICAL_INDICATORS,
    FiredShot,
    RoomContext,
    evaluate_all,
    evaluate_sat_ac,
    evaluate_sat_co2,
    evaluate_sat_dehu,
    evaluate_sat_dew,
    evaluate_sat_fan,
    evaluate_sat_irrig,
    evaluate_sat_press,
)
from app.influx_client import ThresholdOp
from app.models.event_log import EventSeverity

pytestmark = pytest.mark.unit


class FakeInflux:
    """Scripted stand-in for :class:`app.influx_client.InfluxClient`.

    Each query helper looks its answer up per ``entity``:

    * ``current`` — ``{entity: value}`` for :meth:`current_value`.
    * ``slope`` — ``{entity: slope}`` for :meth:`trend_slope`.
    * ``threshold`` — ``{entity: bool}`` for :meth:`at_threshold_for`
      (a flat answer; the predicates only ever ask one threshold
      question per entity, so per-entity granularity is enough).
    * ``delta`` — ``{entity: delta}`` for :meth:`delta_post_event`.

    A missing key returns ``None`` (or ``False`` for thresholds),
    matching the real client's "no data" behaviour.
    """

    def __init__(
        self,
        *,
        current: dict[str, float] | None = None,
        slope: dict[str, float] | None = None,
        threshold: dict[str, bool] | None = None,
        delta: dict[str, float] | None = None,
    ) -> None:
        self._current = current or {}
        self._slope = slope or {}
        self._threshold = threshold or {}
        self._delta = delta or {}

    def current_value(self, entity: str) -> float | None:
        return self._current.get(entity)

    def trend_slope(
        self, entity: str, window: timedelta | str
    ) -> float | None:
        return self._slope.get(entity)

    def at_threshold_for(
        self,
        entity: str,
        op: ThresholdOp,
        threshold: float,
        duration: timedelta | str,
    ) -> bool:
        return self._threshold.get(entity, False)

    def delta_post_event(
        self, entity: str, event_ts: str, window: timedelta | str
    ) -> float | None:
        return self._delta.get(entity)


# ---------------------------------------------------------------------------
# SAT-AC
# ---------------------------------------------------------------------------


class TestSatAc:
    def _ctx(self, **over: Any) -> RoomContext:
        base = {
            "room_id": "r1",
            "temp_actual_entity": "r1_temp",
            "temp_setpoint": 28.0,
            "ac_fan_entity": "r1_ac_fan",
        }
        base.update(over)
        return RoomContext(**base)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_fires_when_fan_high_temp_over_and_climbing(self) -> None:
        influx = FakeInflux(
            threshold={"r1_ac_fan": True},
            current={"r1_temp": 29.5},
            slope={"r1_temp": 0.01},
        )
        result = await evaluate_sat_ac(influx, self._ctx())
        assert result.saturated is True
        assert result.indicator_id == "SAT-AC"
        assert result.reason_code == "SAT-AC"
        assert result.severity is EventSeverity.warning

    @pytest.mark.asyncio
    async def test_quiet_when_fan_not_high(self) -> None:
        influx = FakeInflux(
            threshold={"r1_ac_fan": False},
            current={"r1_temp": 29.5},
            slope={"r1_temp": 0.01},
        )
        result = await evaluate_sat_ac(influx, self._ctx())
        assert result.saturated is False

    @pytest.mark.asyncio
    async def test_quiet_when_temp_within_setpoint(self) -> None:
        influx = FakeInflux(
            threshold={"r1_ac_fan": True},
            current={"r1_temp": 27.9},
            slope={"r1_temp": 0.01},
        )
        result = await evaluate_sat_ac(influx, self._ctx())
        assert result.saturated is False

    @pytest.mark.asyncio
    async def test_quiet_when_temp_not_climbing(self) -> None:
        influx = FakeInflux(
            threshold={"r1_ac_fan": True},
            current={"r1_temp": 29.5},
            slope={"r1_temp": -0.02},
        )
        result = await evaluate_sat_ac(influx, self._ctx())
        assert result.saturated is False

    @pytest.mark.asyncio
    async def test_skipped_when_ac_not_mapped(self) -> None:
        result = await evaluate_sat_ac(
            FakeInflux(), self._ctx(ac_fan_entity=None)
        )
        assert result.saturated is False
        assert result.evaluated is False


# ---------------------------------------------------------------------------
# SAT-DEHU
# ---------------------------------------------------------------------------


class TestSatDehu:
    def _ctx(self, **over: Any) -> RoomContext:
        base = {
            "room_id": "r1",
            "rh_actual_entity": "r1_rh",
            "rh_setpoint": 60.0,
            "dehu_switch_entity": "r1_dehu",
        }
        base.update(over)
        return RoomContext(**base)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_fires_when_dehu_on_rh_overshoot_and_flat(self) -> None:
        # RH 64 > setpoint(60)+3 -> overshoot; slope flat.
        influx = FakeInflux(
            threshold={"r1_dehu": True},
            current={"r1_rh": 64.0},
            slope={"r1_rh": 0.0},
        )
        result = await evaluate_sat_dehu(influx, self._ctx())
        assert result.saturated is True
        assert result.severity is EventSeverity.warning

    @pytest.mark.asyncio
    async def test_quiet_when_rh_within_overshoot_band(self) -> None:
        # RH 62 < setpoint+3 (63) -> not an overshoot.
        influx = FakeInflux(
            threshold={"r1_dehu": True},
            current={"r1_rh": 62.0},
            slope={"r1_rh": 0.1},
        )
        result = await evaluate_sat_dehu(influx, self._ctx())
        assert result.saturated is False

    @pytest.mark.asyncio
    async def test_quiet_when_rh_recovering(self) -> None:
        # Negative RH slope -> dehu is winning, not saturated.
        influx = FakeInflux(
            threshold={"r1_dehu": True},
            current={"r1_rh": 64.0},
            slope={"r1_rh": -0.05},
        )
        result = await evaluate_sat_dehu(influx, self._ctx())
        assert result.saturated is False

    @pytest.mark.asyncio
    async def test_quiet_when_dehu_not_on_long_enough(self) -> None:
        influx = FakeInflux(
            threshold={"r1_dehu": False},
            current={"r1_rh": 64.0},
            slope={"r1_rh": 0.0},
        )
        result = await evaluate_sat_dehu(influx, self._ctx())
        assert result.saturated is False


# ---------------------------------------------------------------------------
# SAT-CO2
# ---------------------------------------------------------------------------


class TestSatCo2:
    def _ctx(self, **over: Any) -> RoomContext:
        base = {
            "room_id": "r1",
            "co2_actual_entity": "r1_co2",
            "co2_setpoint": 1200.0,
            "co2_solenoid_entity": "r1_co2_sol",
        }
        base.update(over)
        return RoomContext(**base)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_fires_when_co2_deficit_and_solenoid_open(self) -> None:
        # CO2 held below 0.9*1200=1080; solenoid continuously open -> duty 1.0.
        influx = FakeInflux(
            threshold={"r1_co2": True, "r1_co2_sol": True},
        )
        result = await evaluate_sat_co2(influx, self._ctx())
        assert result.saturated is True
        assert result.severity is EventSeverity.warning

    @pytest.mark.asyncio
    async def test_quiet_when_co2_not_in_deficit(self) -> None:
        influx = FakeInflux(
            threshold={"r1_co2": False, "r1_co2_sol": True},
        )
        result = await evaluate_sat_co2(influx, self._ctx())
        assert result.saturated is False

    @pytest.mark.asyncio
    async def test_quiet_when_solenoid_duty_low(self) -> None:
        # CO2 in deficit, but solenoid not continuously open and last
        # reading is 0 (shut) -> duty 0.0, below the 50% gate.
        influx = FakeInflux(
            threshold={"r1_co2": True, "r1_co2_sol": False},
            current={"r1_co2_sol": 0.0},
        )
        result = await evaluate_sat_co2(influx, self._ctx())
        assert result.saturated is False

    @pytest.mark.asyncio
    async def test_skipped_without_setpoint(self) -> None:
        result = await evaluate_sat_co2(
            FakeInflux(), self._ctx(co2_setpoint=None)
        )
        assert result.evaluated is False


# ---------------------------------------------------------------------------
# SAT-FAN
# ---------------------------------------------------------------------------


class TestSatFan:
    def _ctx(self, **over: Any) -> RoomContext:
        base: dict[str, Any] = {
            "room_id": "r1",
            "fan_intensity_entities": ["r1_fan_a", "r1_fan_b"],
            "stratification_entities": [("r1_temp_top", "r1_temp_bot")],
        }
        base.update(over)
        return RoomContext(**base)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_fires_when_fan_maxed_and_stratified(self) -> None:
        # fan_b at 10/10; top/bottom differ by 1.5degC > 0.5 epsilon.
        influx = FakeInflux(
            threshold={"r1_fan_b": True},
            current={"r1_temp_top": 26.0, "r1_temp_bot": 24.5},
        )
        result = await evaluate_sat_fan(influx, self._ctx())
        assert result.saturated is True
        assert result.severity is EventSeverity.warning

    @pytest.mark.asyncio
    async def test_quiet_when_no_fan_maxed(self) -> None:
        influx = FakeInflux(
            threshold={},
            current={"r1_temp_top": 26.0, "r1_temp_bot": 24.5},
        )
        result = await evaluate_sat_fan(influx, self._ctx())
        assert result.saturated is False

    @pytest.mark.asyncio
    async def test_quiet_when_stratification_negligible(self) -> None:
        # Fan maxed but top/bottom within 0.5degC -> no measurable
        # stratification, fans are coping.
        influx = FakeInflux(
            threshold={"r1_fan_a": True},
            current={"r1_temp_top": 25.2, "r1_temp_bot": 25.0},
        )
        result = await evaluate_sat_fan(influx, self._ctx())
        assert result.saturated is False


# ---------------------------------------------------------------------------
# SAT-IRRIG  -- escalates to critical severity
# ---------------------------------------------------------------------------


class TestSatIrrig:
    def _shot(self, **over: Any) -> FiredShot:
        base = {
            "zone_id": "z1",
            "vwc_entity": "z1_vwc",
            "fired_at": "2026-05-16T10:00:00Z",
            "volume_ml": 200.0,
            "substrate_volume_ml": 4000.0,  # expected rise = 200/4000*100 = 5%VWC
        }
        base.update(over)
        return FiredShot(**base)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_expected_delta_math(self) -> None:
        assert self._shot().expected_vwc_delta == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_fires_critical_when_vwc_barely_moves(self) -> None:
        # Expected ~5%VWC; measured 1.0% is 20% of expected (< 30%).
        influx = FakeInflux(delta={"z1_vwc": 1.0})
        ctx = RoomContext(room_id="r1", fired_shots=[self._shot()])
        result = await evaluate_sat_irrig(influx, ctx)
        assert result.saturated is True
        assert result.indicator_id == "SAT-IRRIG"
        assert result.severity is EventSeverity.critical

    @pytest.mark.asyncio
    async def test_fires_critical_when_no_vwc_data_after_shot(self) -> None:
        influx = FakeInflux(delta={})  # delta_post_event -> None
        ctx = RoomContext(room_id="r1", fired_shots=[self._shot()])
        result = await evaluate_sat_irrig(influx, ctx)
        assert result.saturated is True
        assert result.severity is EventSeverity.critical

    @pytest.mark.asyncio
    async def test_quiet_when_vwc_rises_as_expected(self) -> None:
        # Measured 4.8%VWC against ~5% expected -> healthy response.
        influx = FakeInflux(delta={"z1_vwc": 4.8})
        ctx = RoomContext(room_id="r1", fired_shots=[self._shot()])
        result = await evaluate_sat_irrig(influx, ctx)
        assert result.saturated is False

    @pytest.mark.asyncio
    async def test_quiet_at_exactly_30_percent_threshold(self) -> None:
        # 30% of 5% = 1.5%; measured exactly 1.5% is NOT below the
        # threshold, so it does not fire.
        influx = FakeInflux(delta={"z1_vwc": 1.5})
        ctx = RoomContext(room_id="r1", fired_shots=[self._shot()])
        result = await evaluate_sat_irrig(influx, ctx)
        assert result.saturated is False

    @pytest.mark.asyncio
    async def test_skipped_when_no_shots_fired(self) -> None:
        result = await evaluate_sat_irrig(
            FakeInflux(), RoomContext(room_id="r1")
        )
        assert result.evaluated is False


# ---------------------------------------------------------------------------
# SAT-PRESS
# ---------------------------------------------------------------------------


class TestSatPress:
    def _ctx(self, **over: Any) -> RoomContext:
        base: dict[str, Any] = {
            "room_id": "r1",
            "pressure_diff_entity": "r1_dp",
            "co2_enrichment_active": True,
        }
        base.update(over)
        return RoomContext(**base)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_fires_when_negative_pressure_and_enrichment_on(self) -> None:
        influx = FakeInflux(
            threshold={"r1_dp": True},
            current={"r1_dp": -8.0},
        )
        result = await evaluate_sat_press(influx, self._ctx())
        assert result.saturated is True
        assert result.severity is EventSeverity.warning

    @pytest.mark.asyncio
    async def test_quiet_when_enrichment_not_active(self) -> None:
        influx = FakeInflux(threshold={"r1_dp": True}, current={"r1_dp": -8.0})
        result = await evaluate_sat_press(
            influx, self._ctx(co2_enrichment_active=False)
        )
        assert result.saturated is False

    @pytest.mark.asyncio
    async def test_quiet_when_pressure_not_negative_enough(self) -> None:
        # Pressure not held <= -5 Pa.
        influx = FakeInflux(threshold={"r1_dp": False}, current={"r1_dp": -2.0})
        result = await evaluate_sat_press(influx, self._ctx())
        assert result.saturated is False


# ---------------------------------------------------------------------------
# SAT-DEW  -- escalates to critical severity
# ---------------------------------------------------------------------------


class TestSatDew:
    def _ctx(self, **over: Any) -> RoomContext:
        base: dict[str, Any] = {
            "room_id": "r1",
            "coldest_surface_entity": "r1_surface",
            "dew_point_entity": "r1_dew",
        }
        base.update(over)
        return RoomContext(**base)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_fires_critical_when_surface_at_dew_point_sustained(
        self,
    ) -> None:
        # Surface 17.0 <= dew point 18.0; sustained threshold true.
        influx = FakeInflux(
            current={"r1_surface": 17.0, "r1_dew": 18.0},
            threshold={"r1_surface": True},
        )
        result = await evaluate_sat_dew(influx, self._ctx())
        assert result.saturated is True
        assert result.indicator_id == "SAT-DEW"
        assert result.severity is EventSeverity.critical

    @pytest.mark.asyncio
    async def test_quiet_when_surface_above_dew_point(self) -> None:
        influx = FakeInflux(
            current={"r1_surface": 20.0, "r1_dew": 18.0},
            threshold={"r1_surface": True},
        )
        result = await evaluate_sat_dew(influx, self._ctx())
        assert result.saturated is False

    @pytest.mark.asyncio
    async def test_quiet_when_not_sustained(self) -> None:
        # Below dew point right now, but the sustained threshold is false
        # -> a one-sample blip, not a fired indicator.
        influx = FakeInflux(
            current={"r1_surface": 17.0, "r1_dew": 18.0},
            threshold={"r1_surface": False},
        )
        result = await evaluate_sat_dew(influx, self._ctx())
        assert result.saturated is False

    @pytest.mark.asyncio
    async def test_quiet_when_no_readings(self) -> None:
        result = await evaluate_sat_dew(FakeInflux(), self._ctx())
        assert result.saturated is False


# ---------------------------------------------------------------------------
# evaluate_all + severity mapping
# ---------------------------------------------------------------------------


class TestEvaluateAll:
    @pytest.mark.asyncio
    async def test_returns_all_seven_indicators(self) -> None:
        results = await evaluate_all(FakeInflux(), RoomContext(room_id="r1"))
        assert len(results) == 7
        ids = {r.indicator_id for r in results}
        assert ids == {
            "SAT-AC",
            "SAT-DEHU",
            "SAT-CO2",
            "SAT-FAN",
            "SAT-IRRIG",
            "SAT-PRESS",
            "SAT-DEW",
        }

    @pytest.mark.asyncio
    async def test_unmapped_room_evaluates_nothing(self) -> None:
        results = await evaluate_all(FakeInflux(), RoomContext(room_id="r1"))
        # Nothing mapped -> every predicate skipped, none saturated.
        assert all(not r.evaluated for r in results)
        assert all(not r.saturated for r in results)

    def test_critical_indicator_set(self) -> None:
        # The two crop-impact indicators are the only criticals.
        assert {"SAT-IRRIG", "SAT-DEW"} == CRITICAL_INDICATORS

    @pytest.mark.asyncio
    async def test_critical_indicators_carry_critical_severity(self) -> None:
        """SAT-IRRIG and SAT-DEW escalate to CRITICAL when they fire."""
        shot = FiredShot(
            zone_id="z1",
            vwc_entity="z1_vwc",
            fired_at="2026-05-16T10:00:00Z",
            volume_ml=200.0,
            substrate_volume_ml=4000.0,
        )
        ctx = RoomContext(
            room_id="r1",
            coldest_surface_entity="r1_surface",
            dew_point_entity="r1_dew",
            fired_shots=[shot],
        )
        influx = FakeInflux(
            delta={"z1_vwc": 0.2},  # SAT-IRRIG fires
            current={"r1_surface": 16.0, "r1_dew": 18.0},  # SAT-DEW fires
            threshold={"r1_surface": True},
        )
        results = {r.indicator_id: r for r in await evaluate_all(influx, ctx)}
        assert results["SAT-IRRIG"].saturated is True
        assert results["SAT-IRRIG"].severity is EventSeverity.critical
        assert results["SAT-DEW"].saturated is True
        assert results["SAT-DEW"].severity is EventSeverity.critical

    @pytest.mark.asyncio
    async def test_warning_indicators_carry_warning_severity(self) -> None:
        """The five non-crop-impact indicators fire at WARNING severity."""
        ctx = RoomContext(
            room_id="r1",
            temp_actual_entity="r1_temp",
            temp_setpoint=28.0,
            ac_fan_entity="r1_ac_fan",
        )
        influx = FakeInflux(
            threshold={"r1_ac_fan": True},
            current={"r1_temp": 30.0},
            slope={"r1_temp": 0.02},
        )
        results = {r.indicator_id: r for r in await evaluate_all(influx, ctx)}
        assert results["SAT-AC"].saturated is True
        assert results["SAT-AC"].severity is EventSeverity.warning
