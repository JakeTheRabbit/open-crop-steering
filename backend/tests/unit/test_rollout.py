"""Unit tests for :mod:`app.core.rollout`.

Covers the per-class effective-mode mapping (plan locked decision #11):
Class A / E always report-only; Class B reaches bounded-auto-adjust at
rollout stage 2, C at 4, D at 6, with a supervised-approval band exactly
one stage earlier. Also covers stage resolution and param classification.
"""

from __future__ import annotations

import pytest
from app.core.rollout import (
    REPORT_ONLY_STAGE,
    ParamClass,
    class_for_param,
    current_stage,
    effective_mode,
    stage_by_name,
)
from app.models.runtime_adjustment import AdjustmentMode

pytestmark = pytest.mark.unit


class _FakeRuntime:
    """Minimal ``RoomRuntime`` stand-in — just a ``rollout_stage``."""

    def __init__(self, rollout_stage: str) -> None:
        self.rollout_stage = rollout_stage


class TestClassForParam:
    def test_environment_params_are_class_b(self) -> None:
        for param in ("temp_day", "rh_day", "co2_day", "vpd_day", "ppfd"):
            assert class_for_param(param) is ParamClass.b

    def test_irrigation_params_are_class_c(self) -> None:
        for param in ("vwc_target", "ec_target", "dryback_pct"):
            assert class_for_param(param) is ParamClass.c

    def test_chemistry_params_are_class_d(self) -> None:
        for param in ("tank_ph", "tank_ec", "leaf_temp"):
            assert class_for_param(param) is ParamClass.d

    def test_unknown_param_defaults_to_class_a(self) -> None:
        assert class_for_param("totally_unknown") is ParamClass.a


class TestEffectiveModeClassA:
    def test_class_a_always_report_only(self) -> None:
        for stage_index in range(7):
            assert (
                effective_mode(stage_index, ParamClass.a)
                is AdjustmentMode.report_only
            )


class TestEffectiveModeClassE:
    def test_class_e_always_report_only(self) -> None:
        for stage_index in range(7):
            assert (
                effective_mode(stage_index, ParamClass.e)
                is AdjustmentMode.report_only
            )


class TestEffectiveModeClassB:
    def test_report_only_before_stage_1(self) -> None:
        assert effective_mode(0, ParamClass.b) is AdjustmentMode.report_only

    def test_supervised_approval_at_stage_1(self) -> None:
        # One stage before auto-adjust (stage 2) -> supervised approval.
        assert (
            effective_mode(1, ParamClass.b)
            is AdjustmentMode.supervised_approval
        )

    def test_bounded_auto_adjust_from_stage_2(self) -> None:
        for stage_index in (2, 3, 4, 5, 6):
            assert (
                effective_mode(stage_index, ParamClass.b)
                is AdjustmentMode.bounded_auto_adjust
            )


class TestEffectiveModeClassC:
    def test_report_only_before_stage_3(self) -> None:
        for stage_index in (0, 1, 2):
            assert (
                effective_mode(stage_index, ParamClass.c)
                is AdjustmentMode.report_only
            )

    def test_supervised_approval_at_stage_3(self) -> None:
        assert (
            effective_mode(3, ParamClass.c)
            is AdjustmentMode.supervised_approval
        )

    def test_bounded_auto_adjust_from_stage_4(self) -> None:
        for stage_index in (4, 5, 6):
            assert (
                effective_mode(stage_index, ParamClass.c)
                is AdjustmentMode.bounded_auto_adjust
            )


class TestEffectiveModeClassD:
    def test_report_only_before_stage_5(self) -> None:
        for stage_index in (0, 1, 2, 3, 4):
            assert (
                effective_mode(stage_index, ParamClass.d)
                is AdjustmentMode.report_only
            )

    def test_supervised_approval_at_stage_5(self) -> None:
        assert (
            effective_mode(5, ParamClass.d)
            is AdjustmentMode.supervised_approval
        )

    def test_bounded_auto_adjust_at_stage_6(self) -> None:
        assert (
            effective_mode(6, ParamClass.d)
            is AdjustmentMode.bounded_auto_adjust
        )


class TestEffectiveModeArgumentForms:
    def test_accepts_stage_name(self) -> None:
        assert (
            effective_mode("report_only", ParamClass.b)
            is AdjustmentMode.report_only
        )
        assert (
            effective_mode("stage_2", ParamClass.b)
            is AdjustmentMode.bounded_auto_adjust
        )

    def test_accepts_param_class_value_string(self) -> None:
        assert effective_mode(2, "B") is AdjustmentMode.bounded_auto_adjust
        assert effective_mode(6, "A") is AdjustmentMode.report_only

    def test_v01_default_stage_is_report_only_everywhere(self) -> None:
        # v0.1 ships every room at REPORT_ONLY_STAGE; no class can
        # auto-adjust there.
        for pclass in ParamClass:
            assert (
                effective_mode(REPORT_ONLY_STAGE, pclass)
                is AdjustmentMode.report_only
            )


class TestStageResolution:
    def test_stage_by_name_resolves_known_stage(self) -> None:
        assert stage_by_name("stage_2").index == 2

    def test_stage_by_name_unknown_falls_back_to_report_only(self) -> None:
        assert stage_by_name("does_not_exist") is REPORT_ONLY_STAGE

    def test_current_stage_reads_runtime(self) -> None:
        assert current_stage(_FakeRuntime("stage_4")).index == 4

    def test_current_stage_unknown_falls_back_to_report_only(self) -> None:
        assert current_stage(_FakeRuntime("garbage")) is REPORT_ONLY_STAGE

    def test_effective_mode_unknown_stage_index_is_report_only(self) -> None:
        # An out-of-range index must never grant more than report-only.
        assert effective_mode(99, ParamClass.b) is AdjustmentMode.report_only
