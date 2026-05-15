"""Unit tests for :mod:`app.core.coupling_rules` — EC-001 ... EC-015.

One test per coupling rule: a proposed change crafted to trip it,
against a snapshot + room config crafted to supply the trigger
condition, asserting the check returns a violation with the right
``ec_id``. The knowledge base's hard-refusal vs fail-soft distinction is
checked on a sample of each kind.

Also covers the registry + :func:`check_coupling` aggregation and the
"clean change trips nothing" negative case.
"""

from __future__ import annotations

import pytest
from app.core.coupling_rules import (
    COUPLING_RULES,
    CouplingViolation,
    RoomConfig,
    check_coupling,
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


def _violation_for(  # type: ignore[no-untyped-def]
    ec_id: str,
    change: ProposedChange,
    snapshot: _FakeSnapshot,
    room_config: RoomConfig | None,
):
    """Run a single named coupling-rule check, return its violation."""
    return COUPLING_RULES[ec_id](change, snapshot, room_config)


# --------------------------------------------------------------------- EC-001


def test_ec001_ppfd_increase_without_hvac_headroom_source() -> None:
    """EC-001: a >=10% PPFD increase with no HVAC-headroom source -> hard."""
    snapshot = _FakeSnapshot({"setpoints": {"ppfd": 600.0}})
    cfg = RoomConfig(room_id="r1", hvac_headroom_source=None)
    violation = _violation_for("EC-001", _change("ppfd", 100.0), snapshot, cfg)
    assert violation is not None
    assert violation.ec_id == "EC-001"
    assert violation.hard is True


def test_ec001_quiet_when_headroom_source_declared() -> None:
    """EC-001 stays quiet when an HVAC-headroom source is declared."""
    snapshot = _FakeSnapshot({"setpoints": {"ppfd": 600.0}})
    cfg = RoomConfig(room_id="r1", hvac_headroom_source="climate.r1")
    assert (
        _violation_for("EC-001", _change("ppfd", 100.0), snapshot, cfg) is None
    )


# --------------------------------------------------------------------- EC-002


def test_ec002_ppfd_increase_without_dehu_headroom_source() -> None:
    """EC-002: a >=10% PPFD increase with no dehum-headroom source -> hard."""
    snapshot = _FakeSnapshot({"setpoints": {"ppfd": 600.0}})
    cfg = RoomConfig(room_id="r1", dehu_headroom_source=None)
    violation = _violation_for("EC-002", _change("ppfd", 100.0), snapshot, cfg)
    assert violation is not None
    assert violation.ec_id == "EC-002"


# --------------------------------------------------------------------- EC-003


def test_ec003_ppfd_increase_when_leaf_temp_at_air_temp() -> None:
    """EC-003: PPFD up while leaf temp >= air temp -> bleach-risk violation."""
    snapshot = _FakeSnapshot(
        {
            "setpoints": {"ppfd": 600.0, "temp": 26.0},
            "sensors": {"f1_leaf_temp": {"value": 27.0}},
        }
    )
    violation = _violation_for(
        "EC-003", _change("ppfd", 100.0), snapshot, None
    )
    assert violation is not None
    assert violation.ec_id == "EC-003"


# --------------------------------------------------------------------- EC-004


def test_ec004_rh_cut_in_dehu_ac_room_without_reheat() -> None:
    """EC-004: RH cut in a dehu+AC room with no reheat -> fail-soft."""
    snapshot = _FakeSnapshot(
        {"equipment_status": {"dehu": "ok", "ac": "ok"}}
    )
    cfg = RoomConfig(room_id="r1", has_reheat=False)
    violation = _violation_for("EC-004", _change("rh_day", -2.0), snapshot, cfg)
    assert violation is not None
    assert violation.ec_id == "EC-004"
    assert violation.hard is False


def test_ec004_quiet_when_reheat_present() -> None:
    """EC-004 stays quiet when a reheat coil is mapped."""
    snapshot = _FakeSnapshot(
        {"equipment_status": {"dehu": "ok", "ac": "ok"}}
    )
    cfg = RoomConfig(room_id="r1", has_reheat=True)
    assert (
        _violation_for("EC-004", _change("rh_day", -2.0), snapshot, cfg)
        is None
    )


# --------------------------------------------------------------------- EC-005


def test_ec005_co2_increase_with_active_exhaust() -> None:
    """EC-005: CO2 up with a mapped, active exhaust -> hard."""
    snapshot = _FakeSnapshot({"exhaust_active": True})
    cfg = RoomConfig(room_id="r1", has_exhaust=True)
    violation = _violation_for("EC-005", _change("co2_day", 50.0), snapshot, cfg)
    assert violation is not None
    assert violation.ec_id == "EC-005"
    assert violation.hard is True


# --------------------------------------------------------------------- EC-006


def test_ec006_co2_increase_below_ppfd_threshold() -> None:
    """EC-006: CO2 up while PPFD is below threshold -> fail-soft (ROI)."""
    snapshot = _FakeSnapshot(
        {"setpoints": {"co2": 800.0, "ppfd": 300.0}, "ppfd_co2_threshold": 500.0}
    )
    violation = _violation_for("EC-006", _change("co2", 50.0), snapshot, None)
    assert violation is not None
    assert violation.ec_id == "EC-006"
    assert violation.hard is False


# --------------------------------------------------------------------- EC-007


def test_ec007_co2_increase_in_negative_pressure_room() -> None:
    """EC-007: CO2 up in a negative-pressure room -> fail-soft."""
    snapshot = _FakeSnapshot(
        {"equipment_status": {"pressure": "SAT-PRESS"}}
    )
    violation = _violation_for("EC-007", _change("co2_day", 50.0), snapshot, None)
    assert violation is not None
    assert violation.ec_id == "EC-007"
    assert violation.hard is False


# --------------------------------------------------------------------- EC-008


def test_ec008_night_temp_cut_with_active_condensation() -> None:
    """EC-008: night-temp cut while condensation is active -> hard."""
    snapshot = _FakeSnapshot(
        {"equipment_status": {"condensation": "SAT-DEW"}}
    )
    violation = _violation_for(
        "EC-008", _change("temp_night", -1.0), snapshot, None
    )
    assert violation is not None
    assert violation.ec_id == "EC-008"
    assert violation.hard is True


# --------------------------------------------------------------------- EC-009


def test_ec009_rh_cut_without_under_canopy_airflow() -> None:
    """EC-009: RH cut in a room with no under-canopy airflow -> fail-soft."""
    snapshot = _FakeSnapshot({})
    cfg = RoomConfig(room_id="r1", has_under_canopy_airflow=False)
    violation = _violation_for("EC-009", _change("rh_day", -2.0), snapshot, cfg)
    assert violation is not None
    assert violation.ec_id == "EC-009"
    assert violation.hard is False


# --------------------------------------------------------------------- EC-010


def test_ec010_dryback_increase_without_runoff_ec_data() -> None:
    """EC-010: dryback up with no runoff EC data -> fail-soft."""
    snapshot = _FakeSnapshot({"sensors": {}})
    violation = _violation_for(
        "EC-010", _change("dryback_pct", 5.0), snapshot, None
    )
    assert violation is not None
    assert violation.ec_id == "EC-010"
    assert violation.hard is False


# --------------------------------------------------------------------- EC-011


def test_ec011_runoff_cut_below_mandatory_minimum() -> None:
    """EC-011: a drain% cut taking runoff below ~10% -> hard."""
    snapshot = _FakeSnapshot({"setpoints": {"drain_pct": 15.0}})
    violation = _violation_for(
        "EC-011", _change("drain_pct", -8.0), snapshot, None
    )
    assert violation is not None
    assert violation.ec_id == "EC-011"
    assert violation.hard is True


def test_ec011_quiet_when_runoff_stays_above_minimum() -> None:
    """EC-011 stays quiet when the cut leaves runoff above the minimum."""
    snapshot = _FakeSnapshot({"setpoints": {"drain_pct": 25.0}})
    assert (
        _violation_for("EC-011", _change("drain_pct", -5.0), snapshot, None)
        is None
    )


# --------------------------------------------------------------------- EC-012


def test_ec012_feed_ec_increase_with_high_vpd() -> None:
    """EC-012: feed-EC up while VPD is already high -> hard."""
    snapshot = _FakeSnapshot({"setpoints": {"vpd_day": 1.6}})
    violation = _violation_for(
        "EC-012", _change("ec_target", 0.3), snapshot, None
    )
    assert violation is not None
    assert violation.ec_id == "EC-012"
    assert violation.hard is True


# --------------------------------------------------------------------- EC-013


def test_ec013_late_irrigation_shot_near_lights_off() -> None:
    """EC-013: an irrigation change pushing a shot near lights-off -> hard."""
    snapshot = _FakeSnapshot({"lights_off_in_minutes": 20})
    violation = _violation_for(
        "EC-013", _change("irrigation_freq", 1.0), snapshot, None
    )
    assert violation is not None
    assert violation.ec_id == "EC-013"
    assert violation.hard is True


# --------------------------------------------------------------------- EC-014


def test_ec014_climate_change_without_validated_sensor_placement() -> None:
    """EC-014: a temp/RH change in a room with unvalidated placement -> soft."""
    snapshot = _FakeSnapshot({})
    cfg = RoomConfig(room_id="r1", sensor_placement_validated=False)
    violation = _violation_for("EC-014", _change("temp_day", -0.3), snapshot, cfg)
    assert violation is not None
    assert violation.ec_id == "EC-014"
    assert violation.hard is False


# --------------------------------------------------------------------- EC-015


def test_ec015_fresh_air_increase_with_unfiltered_intake() -> None:
    """EC-015: more fresh-air exchange with an unfiltered intake -> fail-soft."""
    snapshot = _FakeSnapshot({})
    cfg = RoomConfig(room_id="r1", has_exhaust=True, has_intake_filter=False)
    violation = _violation_for("EC-015", _change("co2_day", 50.0), snapshot, cfg)
    assert violation is not None
    assert violation.ec_id == "EC-015"
    assert violation.hard is False


# --------------------------------------------------------------- aggregation


class TestCheckCoupling:
    def test_registry_has_all_fifteen(self) -> None:
        assert sorted(COUPLING_RULES) == [
            f"EC-{i:03d}" for i in range(1, 16)
        ]

    def test_check_collects_a_violation(self) -> None:
        """check_coupling returns the EC-011 violation for a runoff cut."""
        snapshot = _FakeSnapshot({"setpoints": {"drain_pct": 12.0}})
        violations = check_coupling(
            _change("drain_pct", -8.0), snapshot, None
        )
        assert any(v.ec_id == "EC-011" for v in violations)

    def test_clean_change_trips_nothing(self) -> None:
        """A modest temp cut in a fully-configured room trips no coupling."""
        snapshot = _FakeSnapshot(
            {"equipment_status": {"ac": "ok", "dehu": "ok"}}
        )
        cfg = RoomConfig(
            room_id="r1",
            has_reheat=True,
            sensor_placement_validated=True,
        )
        assert check_coupling(_change("temp_day", -0.2), snapshot, cfg) == []

    def test_from_mapping_builds_config(self) -> None:
        """RoomConfig.from_mapping parses a config dict, defaults the rest."""
        cfg = RoomConfig.from_mapping(
            {"room_id": "r9", "has_reheat": True}
        )
        assert cfg.room_id == "r9"
        assert cfg.has_reheat is True
        assert cfg.has_exhaust is False

    def test_violation_always_carries_its_ec_id(self) -> None:
        """A CouplingViolation built without its ec_id in codes still has it."""
        v = CouplingViolation(
            ec_id="EC-005", reason="x", param_name="co2", reason_codes=[]
        )
        assert "EC-005" in v.reason_codes
