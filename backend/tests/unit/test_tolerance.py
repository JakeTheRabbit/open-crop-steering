"""Unit tests for :mod:`app.core.tolerance`.

Covers the pure functions :func:`in_band` and :func:`classify_drift` at
their boundary conditions — band edges, the minor/major cut at
``2 x tolerance``, and the zero-tolerance collapse. No DB / Influx.
"""

from __future__ import annotations

import pytest
from app.core.tolerance import (
    DriftClass,
    classify_drift,
    in_band,
)

pytestmark = pytest.mark.unit


class TestInBand:
    def test_exact_match_is_in_band(self) -> None:
        assert in_band(28.0, 28.0, 0.5) is True

    def test_within_band_is_in_band(self) -> None:
        assert in_band(28.3, 28.0, 0.5) is True

    def test_on_the_edge_is_in_band(self) -> None:
        # Inclusive edge: exactly tolerance away counts as in band.
        assert in_band(28.5, 28.0, 0.5) is True
        assert in_band(27.5, 28.0, 0.5) is True

    def test_just_outside_band_is_not_in_band(self) -> None:
        assert in_band(28.6, 28.0, 0.5) is False

    def test_negative_tolerance_is_treated_as_absolute(self) -> None:
        assert in_band(28.3, 28.0, -0.5) is True
        assert in_band(28.6, 28.0, -0.5) is False

    def test_zero_tolerance_requires_exact_match(self) -> None:
        assert in_band(28.0, 28.0, 0.0) is True
        assert in_band(28.01, 28.0, 0.0) is False


class TestClassifyDrift:
    def test_in_band_value(self) -> None:
        result = classify_drift(28.2, 28.0, 0.5)
        assert result.drift is DriftClass.in_band
        assert result.in_band is True
        assert result.exceedance == 0.0
        assert result.deviation == pytest.approx(0.2)

    def test_edge_of_band_is_in_band(self) -> None:
        result = classify_drift(28.5, 28.0, 0.5)
        assert result.drift is DriftClass.in_band

    def test_minor_drift_just_outside_band(self) -> None:
        # 0.7 out of band, tol 0.5 -> within 2*tol -> minor.
        result = classify_drift(28.7, 28.0, 0.5)
        assert result.drift is DriftClass.minor
        assert result.in_band is False
        assert result.exceedance == pytest.approx(0.2)

    def test_minor_drift_at_two_tolerance_edge(self) -> None:
        # Exactly 2*tol away -> still minor (inclusive upper edge).
        result = classify_drift(29.0, 28.0, 0.5)
        assert result.drift is DriftClass.minor

    def test_major_drift_beyond_two_tolerance(self) -> None:
        # Just past 2*tol -> major.
        result = classify_drift(29.01, 28.0, 0.5)
        assert result.drift is DriftClass.major
        assert result.exceedance == pytest.approx(0.51)

    def test_negative_direction_drift_classified_same(self) -> None:
        result = classify_drift(26.9, 28.0, 0.5)
        assert result.drift is DriftClass.major
        assert result.deviation == pytest.approx(-1.1)

    def test_zero_tolerance_exact_match_in_band(self) -> None:
        result = classify_drift(28.0, 28.0, 0.0)
        assert result.drift is DriftClass.in_band

    def test_zero_tolerance_any_miss_is_major(self) -> None:
        # No minor zone exists when tolerance is 0.
        result = classify_drift(28.01, 28.0, 0.0)
        assert result.drift is DriftClass.major

    def test_negative_tolerance_is_normalised(self) -> None:
        result = classify_drift(28.3, 28.0, -0.5)
        assert result.drift is DriftClass.in_band
        assert result.tolerance == 0.5
