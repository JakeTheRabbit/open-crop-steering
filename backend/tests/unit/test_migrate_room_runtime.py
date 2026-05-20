"""Unit tests for :mod:`tools.migrate_room_runtime_to_rooms`.

Exercises the pure :func:`tools.migrate_room_runtime_to_rooms.map_room_runtime_row`
function — the DB loader half of the script is intentionally left to
manual end-to-end verification (it touches user data; integration
testing it would defeat the "tool, not migration" framing).

These tests pin the legacy-shape → new-record-shape contract so a
refactor of the script doesn't quietly drift the mapping table the
script's docstring promises.
"""

from __future__ import annotations

import pytest
from tools.migrate_room_runtime_to_rooms import (
    EquipmentRecord,
    SensorRecord,
    map_room_runtime_row,
    map_room_runtime_rows,
)

pytestmark = pytest.mark.unit


class TestEmptyAndNoneInputs:
    """An unconfigured legacy room produces no records."""

    def test_none_equipment_map(self) -> None:
        rec = map_room_runtime_row("f1", "Flower 1", None)
        assert rec.room_id == "f1"
        assert rec.display_name == "Flower 1"
        assert rec.sensors == []
        assert rec.equipment == []

    def test_empty_equipment_map(self) -> None:
        rec = map_room_runtime_row("f1", None, {})
        assert rec.sensors == []
        assert rec.equipment == []

    def test_non_dict_equipment_map_is_ignored(self) -> None:
        # Defensive: a corrupted legacy row should not crash the loader.
        rec = map_room_runtime_row("f1", None, "not a dict")  # type: ignore[arg-type]
        assert rec.sensors == []
        assert rec.equipment == []


class TestRoomLevelSensors:
    """Every legacy sensor-list key maps to the right Convex ``type``."""

    @pytest.mark.parametrize(
        ("legacy_key", "expected_type", "expected_unit"),
        [
            ("temp_sensors", "temperature", "°C"),
            ("rh_sensors", "humidity", "%"),
            ("co2_sensors", "co2", "ppm"),
            ("leaf_temp_sensors", "temperature", "°C"),
            ("under_canopy_rh_probes", "humidity", "%"),
            ("vwc_sensors", "moisture", "%"),
            ("ec_sensors", "ec", "mS/cm"),
            ("ppfd_sensors", "light", "µmol/m²/s"),
            ("dli_sensors", "light", "mol/m²/day"),
            ("pm1_sensors", "air_quality", "µg/m³"),
            ("pm25_sensors", "air_quality", "µg/m³"),
            ("pm4_sensors", "air_quality", "µg/m³"),
            ("pm10_sensors", "air_quality", "µg/m³"),
        ],
    )
    def test_single_entity_maps_to_one_sensor(
        self,
        legacy_key: str,
        expected_type: str,
        expected_unit: str,
    ) -> None:
        rec = map_room_runtime_row(
            "f2",
            "Flower 2",
            {legacy_key: ["sensor.f2_demo_entity"]},
        )
        assert rec.equipment == []
        assert len(rec.sensors) == 1
        sensor = rec.sensors[0]
        assert sensor.external_id == "sensor.f2_demo_entity"
        assert sensor.type == expected_type
        assert sensor.data_unit == expected_unit
        # Code is a deterministic slug — re-running the migration must
        # hit the same lookup key, so the slug is part of the contract.
        assert sensor.code == "sensor-f2-demo-entity"

    def test_multiple_entities_per_role(self) -> None:
        rec = map_room_runtime_row(
            "f2",
            None,
            {
                "temp_sensors": [
                    "sensor.f2_scd41_back_left_temperature",
                    "sensor.f2_scd41_front_right_temperature",
                ]
            },
        )
        assert len(rec.sensors) == 2
        assert {s.external_id for s in rec.sensors} == {
            "sensor.f2_scd41_back_left_temperature",
            "sensor.f2_scd41_front_right_temperature",
        }
        assert all(s.type == "temperature" for s in rec.sensors)

    def test_falsy_entries_are_dropped(self) -> None:
        # Legacy JSON sometimes ships "" or null entries from a half-
        # finished pick; those would crash the picker on save and must
        # not become rows.
        rec = map_room_runtime_row(
            "f1",
            None,
            {"temp_sensors": ["", None, "sensor.real"]},  # type: ignore[list-item]
        )
        assert len(rec.sensors) == 1
        assert rec.sensors[0].external_id == "sensor.real"


class TestRoomLevelEquipment:
    """Every legacy actuator-list key maps to the right Convex ``type``."""

    @pytest.mark.parametrize(
        ("legacy_key", "expected_type"),
        [
            ("light_entities", "lighting"),
            ("ac_entities", "hvac"),
            ("dehumidifier_entities", "hvac"),
            ("reheat_entities", "hvac"),
            ("exhaust_entities", "hvac"),
            ("co2_solenoid_entities", "hvac"),
            ("irrigation_pump_entities", "irrigation"),
            ("mainline_valve_entities", "irrigation"),
        ],
    )
    def test_single_entity_maps_to_one_equipment(
        self,
        legacy_key: str,
        expected_type: str,
    ) -> None:
        rec = map_room_runtime_row(
            "f2",
            None,
            {legacy_key: ["climate.f2_demo_unit"]},
        )
        assert rec.sensors == []
        assert len(rec.equipment) == 1
        eq = rec.equipment[0]
        assert eq.external_id == "climate.f2_demo_unit"
        assert eq.type == expected_type
        assert eq.code == "climate-f2-demo-unit"

    def test_cooling_capacity_entity_becomes_monitoring_equipment(self) -> None:
        rec = map_room_runtime_row(
            "f2",
            None,
            {"cooling_capacity_entity": "climate.f2_ac_door"},
        )
        assert len(rec.equipment) == 1
        eq = rec.equipment[0]
        assert eq.external_id == "climate.f2_ac_door"
        assert eq.type == "monitoring"
        assert eq.notes == "cooling capacity source"


class TestZonesAndTanks:
    """Per-zone valves / probes and per-tank chemistry expand correctly."""

    def test_zone_valve_becomes_irrigation_equipment_with_notes(self) -> None:
        rec = map_room_runtime_row(
            "f2",
            None,
            {
                "zones": [
                    {
                        "zone_id": "row-a",
                        "valve_entities": ["switch.row_a_valve"],
                        "vwc_sensors": ["sensor.row_a_vwc"],
                        "ec_sensors": ["sensor.row_a_ec"],
                    }
                ]
            },
        )
        # one valve (equipment) + one VWC + one EC (sensors)
        assert len(rec.equipment) == 1
        assert len(rec.sensors) == 2

        valve = rec.equipment[0]
        assert valve.type == "irrigation"
        assert valve.external_id == "switch.row_a_valve"
        assert valve.notes == "zone row-a valve"

        vwc = next(s for s in rec.sensors if s.type == "moisture")
        ec = next(s for s in rec.sensors if s.type == "ec")
        assert vwc.notes == "zone row-a VWC"
        assert ec.notes == "zone row-a EC"

    def test_tank_ph_ec_and_dosers(self) -> None:
        rec = map_room_runtime_row(
            "veg",
            None,
            {
                "tanks": [
                    {
                        "tank_id": "T1",
                        "ph_sensor": "sensor.t1_ph",
                        "ec_sensor": "sensor.t1_ec",
                        "doser_entities": [
                            "switch.t1_doser_a",
                            "switch.t1_doser_b",
                        ],
                    }
                ]
            },
        )
        assert len(rec.sensors) == 2
        assert {s.type for s in rec.sensors} == {"ph", "ec"}
        assert len(rec.equipment) == 2
        assert {e.external_id for e in rec.equipment} == {
            "switch.t1_doser_a",
            "switch.t1_doser_b",
        }
        for d in rec.equipment:
            assert d.type == "irrigation"
            assert d.notes == "tank T1 doser"

    def test_zone_with_blank_id_is_skipped(self) -> None:
        rec = map_room_runtime_row(
            "f2",
            None,
            {"zones": [{"zone_id": "", "valve_entities": ["switch.x"]}]},
        )
        assert rec.sensors == []
        assert rec.equipment == []


class TestSensorRecordContents:
    """Sensor records carry the right display name + notes."""

    def test_name_includes_role_label_and_entity_id(self) -> None:
        rec = map_room_runtime_row(
            "f2",
            None,
            {"temp_sensors": ["sensor.f2_back_left_temp"]},
        )
        s = rec.sensors[0]
        assert s.name == "air temperature (sensor.f2_back_left_temp)"
        assert s.notes == "air temperature"

    def test_under_canopy_rh_keeps_role_label_in_notes(self) -> None:
        rec = map_room_runtime_row(
            "f2",
            None,
            {"under_canopy_rh_probes": ["sensor.f2_under_canopy_rh"]},
        )
        s = rec.sensors[0]
        assert s.type == "humidity"
        assert s.notes == "under-canopy RH probe"


class TestBatchMapping:
    """The ``map_room_runtime_rows`` helper expands every input row."""

    def test_three_rooms(self) -> None:
        rows = map_room_runtime_rows(
            [
                ("f1", "Flower 1", {"temp_sensors": ["sensor.f1_t"]}),
                ("f2", None, {"ac_entities": ["climate.f2_ac"]}),
                ("veg", None, None),
            ]
        )
        assert len(rows) == 3
        assert rows[0].room_id == "f1"
        assert len(rows[0].sensors) == 1
        assert rows[1].room_id == "f2"
        assert len(rows[1].equipment) == 1
        assert rows[2].room_id == "veg"
        assert rows[2].sensors == []
        assert rows[2].equipment == []


class TestRecordImmutability:
    """Records are frozen dataclasses (hashable, immutable)."""

    def test_sensor_record_is_frozen(self) -> None:
        rec = SensorRecord(
            external_id="sensor.x",
            type="temperature",
            name="x",
            code="x",
            data_unit="°C",
        )
        # Frozen dataclasses raise FrozenInstanceError (a subclass of
        # AttributeError); pinning the concrete class keeps the assert
        # from blind-catching any Exception.
        with pytest.raises(AttributeError):
            rec.type = "humidity"  # type: ignore[misc]

    def test_equipment_record_is_frozen(self) -> None:
        rec = EquipmentRecord(
            external_id="climate.x",
            type="hvac",
            name="x",
            code="x",
        )
        with pytest.raises(AttributeError):
            rec.type = "lighting"  # type: ignore[misc]
