"""Unit tests for :mod:`app.core.anti_patterns` — AP-01 ... AP-12.

One test per anti-pattern: a proposed change crafted to trip it, against
a snapshot crafted to supply the trigger condition, asserting the check
returns a hit with the right ``ap_id``. Saturation-coupled anti-patterns
additionally assert the coupled ``SAT-*`` id is in ``reason_codes`` —
including the plan's canonical case (AP-02 / ``SAT-AC``).

Also covers the registry + :func:`check_anti_patterns` aggregation and
the "clean change trips nothing" negative case.
"""

from __future__ import annotations

import pytest
from app.core.anti_patterns import (
    ANTI_PATTERNS,
    AntiPatternHit,
    check_anti_patterns,
)
from app.schemas.llm_decision import ProposedChange

pytestmark = pytest.mark.unit


class _FakeSnapshot:
    """Minimal snapshot stand-in — just a ``payload`` dict."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload


def _change(
    param_name: str, delta: float, direction: str | None = None
) -> ProposedChange:
    """Build a :class:`ProposedChange` (direction inferred from delta)."""
    if direction is None:
        direction = (
            "increase" if delta > 0 else "decrease" if delta < 0 else "no_change"
        )
    return ProposedChange(
        param_name=param_name, direction=direction, delta=delta
    )


def _hit_for(ap_id: str, change: ProposedChange, snapshot: _FakeSnapshot):  # type: ignore[no-untyped-def]
    """Run a single named anti-pattern check, return its hit (or None)."""
    return ANTI_PATTERNS[ap_id](change, snapshot)


# --------------------------------------------------------------------- AP-01


def test_ap01_ppfd_increase_without_hvac_headroom() -> None:
    """AP-01: PPFD up while the AC is saturated -> hit citing SAT-AC."""
    snapshot = _FakeSnapshot({"equipment_status": {"ac": "SAT-AC"}})
    hit = _hit_for("AP-01", _change("ppfd", 50.0), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-01"
    assert "SAT-AC" in hit.reason_codes
    assert "AP-01" in hit.reason_codes


# --------------------------------------------------------------------- AP-02


def test_ap02_lower_temp_when_ac_saturated_canonical() -> None:
    """AP-02 canonical case: temp cut + SAT-AC -> reason_codes [SAT-AC, AP-02]."""
    snapshot = _FakeSnapshot({"equipment_status": {"ac": "SAT-AC"}})
    hit = _hit_for("AP-02", _change("temp_day", -0.5), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-02"
    # The plan requires BOTH ids — the SAT id first, the AP id last.
    assert hit.reason_codes == ["SAT-AC", "AP-02"]


def test_ap02_does_not_fire_when_ac_healthy() -> None:
    """AP-02 stays quiet when the AC is not saturated."""
    snapshot = _FakeSnapshot({"equipment_status": {"ac": "ok"}})
    assert _hit_for("AP-02", _change("temp_day", -0.5), snapshot) is None


def test_ap02_does_not_fire_on_a_temp_increase() -> None:
    """AP-02 concerns only a temp *cut* — an increase is fine."""
    snapshot = _FakeSnapshot({"equipment_status": {"ac": "SAT-AC"}})
    assert _hit_for("AP-02", _change("temp_day", +0.5), snapshot) is None


# --------------------------------------------------------------------- AP-03


def test_ap03_lower_rh_when_dehu_saturated() -> None:
    """AP-03: RH cut while the dehumidifier is saturated -> hit citing SAT-DEHU."""
    snapshot = _FakeSnapshot({"equipment_status": {"dehu": "SAT-DEHU"}})
    hit = _hit_for("AP-03", _change("rh_day", -2.0), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-03"
    assert hit.reason_codes == ["SAT-DEHU", "AP-03"]


# --------------------------------------------------------------------- AP-04


def test_ap04_co2_increase_with_negative_pressure() -> None:
    """AP-04: CO2 up while pressure is negative -> hit citing SAT-PRESS."""
    snapshot = _FakeSnapshot({"equipment_status": {"pressure": "SAT-PRESS"}})
    hit = _hit_for("AP-04", _change("co2_day", 50.0), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-04"
    assert "SAT-PRESS" in hit.reason_codes


def test_ap04_co2_increase_with_active_exhaust() -> None:
    """AP-04 also fires on an active exhaust (no SAT id then)."""
    snapshot = _FakeSnapshot({"exhaust_active": True})
    hit = _hit_for("AP-04", _change("co2_day", 50.0), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-04"


# --------------------------------------------------------------------- AP-05


def test_ap05_co2_increase_below_ppfd_threshold() -> None:
    """AP-05: CO2 up while PPFD is below the stage threshold -> hit."""
    snapshot = _FakeSnapshot(
        {
            "setpoints": {"co2": 800.0, "ppfd": 300.0},
            "ppfd_co2_threshold": 500.0,
        }
    )
    hit = _hit_for("AP-05", _change("co2", 50.0), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-05"


def test_ap05_quiet_when_ppfd_above_threshold() -> None:
    """AP-05 stays quiet when PPFD is sufficient (light not limiting)."""
    snapshot = _FakeSnapshot(
        {
            "setpoints": {"co2": 800.0, "ppfd": 700.0},
            "ppfd_co2_threshold": 500.0,
        }
    )
    assert _hit_for("AP-05", _change("co2", 50.0), snapshot) is None


# --------------------------------------------------------------------- AP-06


def test_ap06_dryback_increase_with_climbing_ec() -> None:
    """AP-06: dryback up while EC is climbing -> hit."""
    snapshot = _FakeSnapshot({"ec_trend": "rising"})
    hit = _hit_for("AP-06", _change("dryback_pct", 5.0), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-06"


def test_ap06_detects_climbing_ec_from_sensor_slope() -> None:
    """AP-06 also reads a rising EC trend from a sensor slope."""
    snapshot = _FakeSnapshot(
        {"sensors": {"f1_substrate_ec": {"trend_slope": 0.002}}}
    )
    hit = _hit_for("AP-06", _change("dryback_pct", 5.0), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-06"


# --------------------------------------------------------------------- AP-07


def test_ap07_runoff_decrease_with_accumulating_ec() -> None:
    """AP-07: drain% cut while EC is accumulating -> hit."""
    snapshot = _FakeSnapshot({"ec_trend": "rising"})
    hit = _hit_for("AP-07", _change("drain_pct", -3.0), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-07"


# --------------------------------------------------------------------- AP-08


def test_ap08_late_irrigation_near_lights_off() -> None:
    """AP-08: irrigation pushed close to lights-off -> hit."""
    snapshot = _FakeSnapshot({"lights_off_in_minutes": 15})
    hit = _hit_for("AP-08", _change("irrigation_freq", 1.0), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-08"


def test_ap08_quiet_when_lights_off_is_far() -> None:
    """AP-08 stays quiet when lights-off is comfortably away."""
    snapshot = _FakeSnapshot({"lights_off_in_minutes": 240})
    assert _hit_for("AP-08", _change("irrigation_freq", 1.0), snapshot) is None


# --------------------------------------------------------------------- AP-09


def test_ap09_rh_change_without_dewpoint_check() -> None:
    """AP-09: RH change while condensation is active -> hit citing SAT-DEW."""
    snapshot = _FakeSnapshot(
        {"equipment_status": {"condensation": "SAT-DEW"}}
    )
    hit = _hit_for("AP-09", _change("rh_night", -3.0), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-09"
    assert "SAT-DEW" in hit.reason_codes


# --------------------------------------------------------------------- AP-10


def test_ap10_air_velocity_excessive() -> None:
    """AP-10: air-velocity increase past the tolerance ceiling -> hit."""
    snapshot = _FakeSnapshot({"setpoints": {"air_velocity": 0.9}})
    hit = _hit_for("AP-10", _change("air_velocity", 0.5), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-10"


def test_ap10_quiet_when_within_tolerance() -> None:
    """AP-10 stays quiet when the resulting velocity is within tolerance."""
    snapshot = _FakeSnapshot({"setpoints": {"air_velocity": 0.3}})
    assert _hit_for("AP-10", _change("air_velocity", 0.2), snapshot) is None


# --------------------------------------------------------------------- AP-11


def test_ap11_proposal_on_stale_sensor() -> None:
    """AP-11: a change backed by a stale sensor -> hit."""
    snapshot = _FakeSnapshot({"stale_sensors": ["f1_temp"]})
    hit = _hit_for("AP-11", _change("temp_day", -0.3), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-11"


def test_ap11_proposal_on_disagreeing_sensors() -> None:
    """AP-11 also fires when sibling sensors disagree."""
    snapshot = _FakeSnapshot(
        {"disagreeing_sensors": [["f1_rh_a", "f1_rh_b"]]}
    )
    hit = _hit_for("AP-11", _change("rh_day", 1.0), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-11"


# --------------------------------------------------------------------- AP-12


def test_ap12_simultaneous_high_ec_high_dryback() -> None:
    """AP-12: feed-EC increase while dryback is already aggressive -> hit."""
    snapshot = _FakeSnapshot({"dryback_high": True})
    hit = _hit_for("AP-12", _change("ec_target", 0.3), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-12"


def test_ap12_detects_high_dryback_from_setpoint() -> None:
    """AP-12 also reads an aggressive dryback from the setpoint value."""
    snapshot = _FakeSnapshot({"setpoints": {"dryback_pct": 55.0}})
    hit = _hit_for("AP-12", _change("tank_ec", 0.3), snapshot)
    assert hit is not None
    assert hit.ap_id == "AP-12"


# --------------------------------------------------------------- aggregation


class TestCheckAntiPatterns:
    def test_registry_has_all_twelve(self) -> None:
        assert sorted(ANTI_PATTERNS) == [f"AP-{i:02d}" for i in range(1, 13)]

    def test_check_collects_the_canonical_hit(self) -> None:
        """check_anti_patterns returns the AP-02 hit for the canonical case."""
        snapshot = _FakeSnapshot({"equipment_status": {"ac": "SAT-AC"}})
        hits = check_anti_patterns(_change("temp_day", -0.5), snapshot)
        assert [h.ap_id for h in hits] == ["AP-02"]
        assert hits[0].reason_codes == ["SAT-AC", "AP-02"]

    def test_clean_change_trips_nothing(self) -> None:
        """A modest temp cut in a healthy room trips no anti-pattern."""
        snapshot = _FakeSnapshot(
            {"equipment_status": {"ac": "ok", "dehu": "ok"}}
        )
        assert check_anti_patterns(_change("temp_day", -0.2), snapshot) == []

    def test_hit_always_carries_its_ap_id(self) -> None:
        """An AntiPatternHit built without its ap_id in codes still has it."""
        hit = AntiPatternHit(
            ap_id="AP-05", reason="x", param_name="co2", reason_codes=[]
        )
        assert "AP-05" in hit.reason_codes
