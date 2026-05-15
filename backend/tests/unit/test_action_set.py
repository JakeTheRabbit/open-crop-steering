"""Unit tests for :mod:`app.core.action_set`.

Exercises the deterministic ``allowed_action_set`` generator: three
options per steered setpoint (up / down / no-change), step size from the
tolerance band or a per-parameter default, stable content-derived
``action_id`` values, and risk-class tagging.
"""

from __future__ import annotations

import pytest
from app.core.action_set import (
    DIRECTION_DOWN,
    DIRECTION_HOLD,
    DIRECTION_UP,
    Action,
    ParamCandidate,
    action_set_to_payload,
    build_action_set,
)
from app.core.rollout import ParamClass

pytestmark = pytest.mark.unit


class _FakeSnapshot:
    """Minimal snapshot stand-in — just an ``id`` and a ``payload`` dict."""

    def __init__(self, payload: dict, snapshot_id: int = 1) -> None:
        self.id = snapshot_id
        self.payload = payload


class TestBuildActionSetFromCandidates:
    def test_three_actions_per_candidate(self) -> None:
        actions = build_action_set(
            _FakeSnapshot({}),
            candidates=[
                ParamCandidate("temp_day", current_value=28.0, tolerance=0.5)
            ],
        )
        assert len(actions) == 3
        directions = {a.direction for a in actions}
        assert directions == {DIRECTION_UP, DIRECTION_DOWN, DIRECTION_HOLD}

    def test_step_size_uses_tolerance_band(self) -> None:
        actions = build_action_set(
            _FakeSnapshot({}),
            candidates=[
                ParamCandidate("temp_day", current_value=28.0, tolerance=0.5)
            ],
        )
        up = next(a for a in actions if a.direction == DIRECTION_UP)
        down = next(a for a in actions if a.direction == DIRECTION_DOWN)
        assert up.delta == 0.5
        assert down.delta == -0.5
        assert up.resulting_value == 28.5
        assert down.resulting_value == 27.5

    def test_step_size_falls_back_to_default_when_no_tolerance(self) -> None:
        actions = build_action_set(
            _FakeSnapshot({}),
            candidates=[ParamCandidate("co2_day", current_value=1000.0)],
        )
        up = next(a for a in actions if a.direction == DIRECTION_UP)
        # Default step for co2_day is 25.0 (see _DEFAULT_STEP).
        assert up.delta == 25.0

    def test_hold_action_has_zero_delta(self) -> None:
        actions = build_action_set(
            _FakeSnapshot({}),
            candidates=[ParamCandidate("rh_day", current_value=60.0, tolerance=3.0)],
        )
        hold = next(a for a in actions if a.direction == DIRECTION_HOLD)
        assert hold.delta == 0.0
        assert hold.resulting_value == 60.0

    def test_action_ids_are_stable_across_calls(self) -> None:
        candidate = ParamCandidate("temp_day", current_value=28.0, tolerance=0.5)
        first = build_action_set(_FakeSnapshot({}), candidates=[candidate])
        second = build_action_set(_FakeSnapshot({}), candidates=[candidate])
        assert [a.action_id for a in first] == [a.action_id for a in second]

    def test_action_ids_are_distinct_per_direction(self) -> None:
        actions = build_action_set(
            _FakeSnapshot({}),
            candidates=[ParamCandidate("temp_day", current_value=28.0, tolerance=0.5)],
        )
        ids = [a.action_id for a in actions]
        assert len(ids) == len(set(ids))

    def test_param_class_is_tagged(self) -> None:
        actions = build_action_set(
            _FakeSnapshot({}),
            candidates=[ParamCandidate("temp_day", current_value=28.0)],
        )
        assert all(a.param_class is ParamClass.b for a in actions)

    def test_unknown_param_is_class_a(self) -> None:
        actions = build_action_set(
            _FakeSnapshot({}),
            candidates=[ParamCandidate("mystery_param", current_value=1.0)],
        )
        assert all(a.param_class is ParamClass.a for a in actions)

    def test_empty_candidates_yields_empty_set(self) -> None:
        assert build_action_set(_FakeSnapshot({}), candidates=[]) == []


class TestBuildActionSetFromSnapshotPayload:
    def test_derives_candidates_from_setpoints(self) -> None:
        snapshot = _FakeSnapshot(
            {
                "setpoints": {"temp": 28.0, "rh": 60.0, "co2": 1000.0},
                "tolerances": {"temp": 0.5},
            }
        )
        actions = build_action_set(snapshot)
        params = {a.param_name for a in actions}
        assert params == {"temp", "rh", "co2"}
        # 3 setpoints x 3 directions.
        assert len(actions) == 9

    def test_skips_setpoint_with_none_value(self) -> None:
        snapshot = _FakeSnapshot(
            {"setpoints": {"temp": 28.0, "rh": None, "co2": None}}
        )
        actions = build_action_set(snapshot)
        assert {a.param_name for a in actions} == {"temp"}

    def test_no_setpoints_yields_empty_set(self) -> None:
        assert build_action_set(_FakeSnapshot({"setpoints": {}})) == []
        assert build_action_set(_FakeSnapshot({})) == []


class TestActionSetToPayload:
    def test_payload_is_json_able(self) -> None:
        actions = build_action_set(
            _FakeSnapshot({}),
            candidates=[ParamCandidate("temp_day", current_value=28.0, tolerance=0.5)],
        )
        payload = action_set_to_payload(actions)
        assert len(payload) == 3
        sample = payload[0]
        assert set(sample) == {
            "action_id",
            "param_name",
            "param_class",
            "direction",
            "delta",
            "current_value",
            "resulting_value",
            "description",
        }

    def test_action_to_dict_round_trips_fields(self) -> None:
        action = Action(
            action_id="act-x",
            param_name="temp_day",
            param_class=ParamClass.b,
            direction=DIRECTION_UP,
            delta=0.5,
            current_value=28.0,
            resulting_value=28.5,
            description="raise",
        )
        d = action.to_dict()
        assert d["action_id"] == "act-x"
        assert d["param_class"] == "B"
        assert d["resulting_value"] == 28.5
