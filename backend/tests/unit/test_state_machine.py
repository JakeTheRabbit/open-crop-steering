"""Unit tests for :func:`app.core.state_machine.next_state`.

The transition function is pure, so it is exercised exhaustively here:
every combination of {critical saturation present?} x {warning
saturation present?} x {stale sensors?} x {tolerance breach?} against
every possible ``current`` state — and the ``current`` state is asserted
to never change the outcome (the machine is memoryless).
"""

from __future__ import annotations

import pytest
from app.core.equipment import SaturationResult
from app.core.state_machine import RoomState, next_state
from app.core.tolerance import (
    DriftClass,
    DriftResult,
    ToleranceBreach,
)
from app.models.event_log import EventSeverity

pytestmark = pytest.mark.unit


def _sat(indicator_id: str, *, saturated: bool, severity: EventSeverity) -> SaturationResult:
    """Build a SaturationResult for the transition table."""
    return SaturationResult(
        indicator_id=indicator_id,
        saturated=saturated,
        severity=severity if saturated else EventSeverity.info,
        detail="synthetic",
        reason_code=indicator_id,
    )


def _breach(param: str = "temp_day") -> ToleranceBreach:
    """Build a major-drift ToleranceBreach."""
    return ToleranceBreach(
        room_id="r1",
        param_name=param,
        day_index=1,
        result=DriftResult(
            drift=DriftClass.major,
            actual=30.0,
            target=28.0,
            tolerance=0.5,
            deviation=2.0,
            exceedance=1.5,
        ),
    )


# Fired saturation results of each severity.
_CRIT_SAT = _sat("SAT-IRRIG", saturated=True, severity=EventSeverity.critical)
_WARN_SAT = _sat("SAT-AC", saturated=True, severity=EventSeverity.warning)
# A non-fired result must never influence the state.
_QUIET_SAT = _sat("SAT-DEHU", saturated=False, severity=EventSeverity.info)


class TestNextStateTransitionTable:
    """Exhaustive truth table for next_state."""

    @pytest.mark.parametrize("current", list(RoomState))
    @pytest.mark.parametrize("has_critical", [True, False])
    @pytest.mark.parametrize("has_warning", [True, False])
    @pytest.mark.parametrize("has_stale", [True, False])
    @pytest.mark.parametrize("has_breach", [True, False])
    def test_full_truth_table(
        self,
        current: RoomState,
        has_critical: bool,
        has_warning: bool,
        has_stale: bool,
        has_breach: bool,
    ) -> None:
        """Every input combination yields the documented next state."""
        sats: list[SaturationResult] = [_QUIET_SAT]
        if has_critical:
            sats.append(_CRIT_SAT)
        if has_warning:
            sats.append(_WARN_SAT)
        stale = ["r1_temp"] if has_stale else []
        breaches = [_breach()] if has_breach else []

        result = next_state(current, sats, stale, breaches)

        if has_critical:
            expected = RoomState.critical
        elif has_warning or has_stale or has_breach:
            expected = RoomState.impaired
        else:
            expected = RoomState.healthy
        assert result == expected

    @pytest.mark.parametrize("current", list(RoomState))
    def test_current_state_does_not_affect_result(
        self, current: RoomState
    ) -> None:
        """The machine is memoryless — ``current`` never changes the outcome."""
        # Same inputs, every possible current state -> identical result.
        results = {
            next_state(c, [_WARN_SAT], [], [])
            for c in RoomState
        }
        assert results == {RoomState.impaired}
        assert next_state(current, [], [], []) == RoomState.healthy


class TestNextStatePrecedence:
    def test_critical_beats_warning(self) -> None:
        result = next_state(
            RoomState.healthy, [_CRIT_SAT, _WARN_SAT], [], []
        )
        assert result == RoomState.critical

    def test_critical_beats_stale_and_breach(self) -> None:
        result = next_state(
            RoomState.healthy, [_CRIT_SAT], ["r1_rh"], [_breach()]
        )
        assert result == RoomState.critical

    def test_warning_saturation_alone_is_impaired(self) -> None:
        assert (
            next_state(RoomState.healthy, [_WARN_SAT], [], [])
            == RoomState.impaired
        )

    def test_stale_sensor_alone_is_impaired(self) -> None:
        assert (
            next_state(RoomState.healthy, [], ["r1_co2"], [])
            == RoomState.impaired
        )

    def test_tolerance_breach_alone_is_impaired(self) -> None:
        assert (
            next_state(RoomState.healthy, [], [], [_breach()])
            == RoomState.impaired
        )

    def test_no_problems_is_healthy(self) -> None:
        assert (
            next_state(RoomState.critical, [_QUIET_SAT], [], [])
            == RoomState.healthy
        )

    def test_empty_inputs_are_healthy(self) -> None:
        assert next_state(RoomState.impaired, [], [], []) == RoomState.healthy

    def test_unevaluated_predicates_do_not_trip_state(self) -> None:
        """An ``evaluated=False`` (skipped) predicate must not be saturated."""
        skipped = SaturationResult(
            indicator_id="SAT-DEW",
            saturated=False,
            severity=EventSeverity.info,
            detail="not mapped",
            reason_code="SAT-DEW",
            evaluated=False,
        )
        assert (
            next_state(RoomState.healthy, [skipped], [], [])
            == RoomState.healthy
        )
