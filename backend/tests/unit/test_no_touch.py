"""Unit tests for :mod:`app.core.no_touch`.

Covers :class:`NoTouchWindow` clock + weekday matching (including the
wrap-past-midnight case), config-mapping parsing, validation of bad
clock strings, and :func:`in_no_touch_window` over a window list.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.core.no_touch import NoTouchWindow, in_no_touch_window

pytestmark = pytest.mark.unit


def _at(hour: int, minute: int = 0, *, weekday: int = 0) -> dt.datetime:
    """Build a datetime at a clock time on a chosen weekday.

    2026-05-18 is a Monday (weekday 0); adding ``weekday`` days lands on
    the requested weekday.
    """
    base = dt.datetime(2026, 5, 18, hour, minute, tzinfo=dt.UTC)
    return base + dt.timedelta(days=weekday)


class TestSameDayWindow:
    def test_inside_window_matches(self) -> None:
        window = NoTouchWindow(start="09:00", end="17:00")
        assert window.contains(_at(12, 0)) is True

    def test_at_start_edge_matches(self) -> None:
        window = NoTouchWindow(start="09:00", end="17:00")
        assert window.contains(_at(9, 0)) is True

    def test_at_end_edge_is_exclusive(self) -> None:
        window = NoTouchWindow(start="09:00", end="17:00")
        assert window.contains(_at(17, 0)) is False

    def test_before_window_does_not_match(self) -> None:
        window = NoTouchWindow(start="09:00", end="17:00")
        assert window.contains(_at(8, 59)) is False

    def test_after_window_does_not_match(self) -> None:
        window = NoTouchWindow(start="09:00", end="17:00")
        assert window.contains(_at(17, 1)) is False


class TestWrapPastMidnightWindow:
    def test_late_evening_matches(self) -> None:
        window = NoTouchWindow(start="22:00", end="02:00")
        assert window.contains(_at(23, 30)) is True

    def test_early_morning_matches(self) -> None:
        window = NoTouchWindow(start="22:00", end="02:00")
        assert window.contains(_at(1, 0)) is True

    def test_midday_does_not_match(self) -> None:
        window = NoTouchWindow(start="22:00", end="02:00")
        assert window.contains(_at(12, 0)) is False

    def test_end_edge_exclusive_when_wrapping(self) -> None:
        window = NoTouchWindow(start="22:00", end="02:00")
        assert window.contains(_at(2, 0)) is False


class TestWeekdayRestriction:
    def test_matches_only_on_listed_weekday(self) -> None:
        window = NoTouchWindow(start="09:00", end="17:00", weekdays=frozenset({0}))
        assert window.contains(_at(12, 0, weekday=0)) is True
        assert window.contains(_at(12, 0, weekday=1)) is False

    def test_empty_weekdays_matches_every_day(self) -> None:
        window = NoTouchWindow(start="09:00", end="17:00")
        for weekday in range(7):
            assert window.contains(_at(12, 0, weekday=weekday)) is True


class TestDegenerateWindow:
    def test_zero_length_window_never_matches(self) -> None:
        window = NoTouchWindow(start="09:00", end="09:00")
        assert window.contains(_at(9, 0)) is False


class TestValidation:
    def test_bad_clock_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid HH:MM"):
            NoTouchWindow(start="9am", end="17:00")

    def test_out_of_range_hour_rejected(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            NoTouchWindow(start="25:00", end="17:00")

    def test_out_of_range_weekday_rejected(self) -> None:
        with pytest.raises(ValueError, match="weekday out of range"):
            NoTouchWindow(start="09:00", end="17:00", weekdays=frozenset({9}))


class TestFromMapping:
    def test_parses_full_mapping(self) -> None:
        window = NoTouchWindow.from_mapping(
            {
                "start": "22:00",
                "end": "06:00",
                "weekdays": [5, 6],
                "label": "weekend night",
            }
        )
        assert window.start == "22:00"
        assert window.weekdays == frozenset({5, 6})
        assert window.label == "weekend night"

    def test_parses_minimal_mapping(self) -> None:
        window = NoTouchWindow.from_mapping({"start": "09:00", "end": "17:00"})
        assert window.weekdays == frozenset()
        assert window.label == ""

    def test_string_weekdays_rejected(self) -> None:
        with pytest.raises(TypeError, match="weekdays must be a list"):
            NoTouchWindow.from_mapping(
                {"start": "09:00", "end": "17:00", "weekdays": "monday"}
            )


class TestInNoTouchWindow:
    def test_empty_window_list_is_false(self) -> None:
        assert in_no_touch_window(_at(12, 0), []) is False

    def test_matches_when_any_window_contains_now(self) -> None:
        windows = [
            NoTouchWindow(start="00:00", end="06:00"),
            NoTouchWindow(start="09:00", end="17:00"),
        ]
        assert in_no_touch_window(_at(12, 0), windows) is True

    def test_false_when_no_window_contains_now(self) -> None:
        windows = [
            NoTouchWindow(start="00:00", end="06:00"),
            NoTouchWindow(start="09:00", end="17:00"),
        ]
        assert in_no_touch_window(_at(20, 0), windows) is False

    def test_accepts_raw_config_mappings(self) -> None:
        windows = [{"start": "09:00", "end": "17:00"}]
        assert in_no_touch_window(_at(12, 0), windows) is True
        assert in_no_touch_window(_at(20, 0), windows) is False
