"""Unit tests for the 12-week cannabis preset.

Pure-data tests — no database, no Docker. Verify the preset expands to
the right shape (84 days x all required params) and that every value
sits inside an agronomically sane band (cultivation_knowledge.md).
"""

from __future__ import annotations

import pytest
from app.presets import build_cannabis_12week, get_preset
from app.presets.cannabis_12week import CYCLE_DAYS, REQUIRED_PARAMS

pytestmark = pytest.mark.unit


# Sane absolute ranges per parameter (min, max). Generous bands — these
# guard against gross errors, not tight horticultural optimality.
_SANE_RANGES: dict[str, tuple[float, float]] = {
    "temp_day": (18.0, 32.0),
    "temp_night": (15.0, 28.0),
    "rh_day": (40.0, 75.0),
    "rh_night": (40.0, 75.0),
    "co2_day": (400.0, 1500.0),
    "vpd_day": (0.4, 1.8),
    "ppfd": (200.0, 1000.0),
    "photoperiod_hours": (10.0, 20.0),
    "vwc_target": (30.0, 75.0),
    "ec_target": (0.8, 3.5),
    "dryback_pct": (3.0, 35.0),
}


@pytest.fixture(scope="module")
def preset() -> list[dict[str, object]]:
    """Build the preset once for the whole module."""
    return build_cannabis_12week()


def test_preset_has_84_days(preset: list[dict[str, object]]) -> None:
    """The recipe covers exactly 84 distinct 1-based day indices."""
    days = {int(row["day_index"]) for row in preset}  # type: ignore[call-overload]
    assert days == set(range(1, CYCLE_DAYS + 1))


def test_every_day_has_every_required_param(
    preset: list[dict[str, object]],
) -> None:
    """Each of the 84 days carries exactly the required parameter set."""
    by_day: dict[int, set[str]] = {}
    for row in preset:
        day = int(row["day_index"])  # type: ignore[call-overload]
        by_day.setdefault(day, set()).add(str(row["param_name"]))

    for day in range(1, CYCLE_DAYS + 1):
        assert by_day[day] == set(REQUIRED_PARAMS), f"day {day} param mismatch"


def test_total_row_count(preset: list[dict[str, object]]) -> None:
    """Row count is 84 days x N required params, no duplicates."""
    assert len(preset) == CYCLE_DAYS * len(REQUIRED_PARAMS)
    keys = {
        (int(r["day_index"]), str(r["param_name"]))  # type: ignore[call-overload]
        for r in preset
    }
    assert len(keys) == len(preset)


def test_values_in_sane_ranges(preset: list[dict[str, object]]) -> None:
    """Every value sits inside its agronomically sane band."""
    for row in preset:
        name = str(row["param_name"])
        value = float(row["value"])  # type: ignore[arg-type]
        lo, hi = _SANE_RANGES[name]
        assert lo <= value <= hi, (
            f"day {row['day_index']} {name}={value} outside [{lo}, {hi}]"
        )


def test_every_row_has_tolerance_and_unit(
    preset: list[dict[str, object]],
) -> None:
    """Stage-default tolerance + unit are present on every row."""
    for row in preset:
        assert "tolerance" in row
        assert row.get("unit"), f"row missing unit: {row}"


def test_photoperiod_flips_to_flower_on_day_43(
    preset: list[dict[str, object]],
) -> None:
    """18 h veg photoperiod through day 42; 12 h flower from day 43."""
    photo = {
        int(r["day_index"]): float(r["value"])  # type: ignore[arg-type]
        for r in preset
        if r["param_name"] == "photoperiod_hours"
    }
    assert photo[1] == 18.0
    assert photo[42] == 18.0
    assert photo[43] == 12.0
    assert photo[CYCLE_DAYS] == 12.0


def test_progression_is_monotonic_where_expected(
    preset: list[dict[str, object]],
) -> None:
    """RH trends down and dryback trends up across the cycle.

    Not strict per-day monotonic (interpolation rounding can tie), but
    the stage endpoints must move in the agronomic direction: humidity
    falls from early veg to late flower, dryback deepens.
    """
    rh_day = {
        int(r["day_index"]): float(r["value"])  # type: ignore[arg-type]
        for r in preset
        if r["param_name"] == "rh_day"
    }
    dryback = {
        int(r["day_index"]): float(r["value"])  # type: ignore[arg-type]
        for r in preset
        if r["param_name"] == "dryback_pct"
    }
    assert rh_day[1] > rh_day[CYCLE_DAYS]
    assert dryback[1] < dryback[CYCLE_DAYS]


def test_co2_off_in_early_veg_and_final_week(
    preset: list[dict[str, object]],
) -> None:
    """CO2 enrichment is off (ambient) in early veg and the final week."""
    co2 = {
        int(r["day_index"]): float(r["value"])  # type: ignore[arg-type]
        for r in preset
        if r["param_name"] == "co2_day"
    }
    # Early veg (week 1) — no enrichment yet.
    assert co2[1] <= 450.0
    # Late veg / early-mid flower — enrichment on.
    assert co2[35] > 800.0
    # Final week — enrichment withdrawn.
    assert co2[CYCLE_DAYS] <= 450.0


def test_get_preset_returns_same_builder() -> None:
    """The registry resolves the cannabis preset to its builder."""
    builder = get_preset("cannabis_12week")
    assert builder is build_cannabis_12week
    assert len(builder()) == CYCLE_DAYS * len(REQUIRED_PARAMS)


def test_get_preset_unknown_raises() -> None:
    """An unknown preset name raises KeyError."""
    with pytest.raises(KeyError, match="unknown preset"):
        get_preset("does_not_exist")
