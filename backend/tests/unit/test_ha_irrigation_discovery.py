"""Unit tests for :mod:`app.integrations.ha_irrigation.discovery`.

The entity-grouping logic is exercised against a hand-built HA registry
payload (no live HA, no testcontainer). A small in-line fake HA client
satisfies the :class:`discovery.HAClientProtocol` so the orchestrator
:func:`discover` is also covered without infrastructure.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.integrations.ha_irrigation import discovery

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake HA client — satisfies HAClientProtocol for orchestrator tests
# ---------------------------------------------------------------------------


class _FakeHAClient:
    """Returns canned registry + state payloads."""

    def __init__(
        self,
        entities: list[dict[str, Any]] | None = None,
        states: list[dict[str, Any]] | None = None,
    ) -> None:
        self._entities = entities or []
        self._states = states or []
        self.registry_calls = 0
        self.state_calls = 0

    async def list_registry(self) -> dict[str, list[dict[str, Any]]]:
        self.registry_calls += 1
        return {
            "areas": [],
            "entities": list(self._entities),
            "devices": [],
        }

    async def get_states(self) -> list[dict[str, Any]]:
        self.state_calls += 1
        return list(self._states)


def _entity(entity_id: str) -> dict[str, Any]:
    """Build a minimal HA registry row."""
    return {"entity_id": entity_id, "platform": "crop_steering"}


def _state(entity_id: str, state_value: str = "0") -> dict[str, Any]:
    """Build a minimal HA state row."""
    return {"entity_id": entity_id, "state": state_value, "attributes": {}}


# ---------------------------------------------------------------------------
# Pure grouping — group_entities
# ---------------------------------------------------------------------------


class TestGroupEntities:
    """Behaviour of the pure grouping function."""

    def test_empty_input_yields_empty_zones_with_warnings(self) -> None:
        result = discovery.group_entities([])
        assert result.detected_zone_count == 0
        assert result.zones == []
        assert result.suggested_room.name == "Crop Steering"
        assert result.suggested_room.external_system_id == "ha_irrigation_strategy"
        # Empty result still produces the room-level "missing" warnings.
        warning_text = " ".join(result.warnings)
        assert "RootSense" in warning_text
        assert "steering-intent" in warning_text.lower() or "steering_intent" in warning_text.lower()
        assert "pump" in warning_text.lower()

    def test_three_zones_canonical_entities(self) -> None:
        entity_ids = [
            # Zone 1 — canonical
            "sensor.crop_steering_vwc_zone_1",
            "sensor.crop_steering_ec_zone_1",
            "switch.crop_steering_zone_1_valve",
            # Zone 2 — canonical
            "sensor.crop_steering_vwc_zone_2",
            "sensor.crop_steering_ec_zone_2",
            "switch.crop_steering_zone_2_valve",
            # Zone 3 — canonical
            "sensor.crop_steering_vwc_zone_3",
            "sensor.crop_steering_ec_zone_3",
            "switch.crop_steering_zone_3_valve",
            # Room-level
            "switch.crop_steering_pump",
            "switch.crop_steering_main_valve",
            "number.crop_steering_steering_intent",
            "binary_sensor.crop_steering_anomaly_active",
            "select.crop_steering_irrigation_phase",
            "sensor.crop_steering_rootsense_report_latest",
            # EC targets (out of order — verify they're sorted)
            "number.crop_steering_ec_target_veg_p3",
            "number.crop_steering_ec_target_veg_p0",
            "number.crop_steering_ec_target_flush",
        ]
        result = discovery.group_entities(entity_ids)
        assert result.detected_zone_count == 3
        assert [z.zone_index for z in result.zones] == [1, 2, 3]
        for zone in result.zones:
            assert zone.suggested_location_label == f"Zone {zone.zone_index}"
            assert len(zone.candidate_entities.vwc_sensors) == 1
            assert len(zone.candidate_entities.ec_sensors) == 1
            assert len(zone.candidate_entities.valves) == 1

        room = result.room_level_candidates
        assert room.pump == ["switch.crop_steering_pump"]
        assert room.mainline_valve == ["switch.crop_steering_main_valve"]
        assert room.steering_intent == [
            "number.crop_steering_steering_intent"
        ]
        assert room.anomaly_binary_sensor == [
            "binary_sensor.crop_steering_anomaly_active"
        ]
        assert room.phase_select == [
            "select.crop_steering_irrigation_phase"
        ]
        assert room.rootsense_report_sensor == [
            "sensor.crop_steering_rootsense_report_latest"
        ]
        # EC targets are sorted for stable output
        assert room.ec_targets == sorted(room.ec_targets)
        assert "number.crop_steering_ec_target_veg_p0" in room.ec_targets
        assert "number.crop_steering_ec_target_veg_p3" in room.ec_targets
        assert "number.crop_steering_ec_target_flush" in room.ec_targets
        assert result.warnings == []

    def test_raw_front_back_sensors_grouped_with_zone(self) -> None:
        entity_ids = [
            "sensor.crop_steering_vwc_zone_1",
            "sensor.vwc_zone_1_front",
            "sensor.vwc_zone_1_back",
            "sensor.crop_steering_ec_zone_1",
            "sensor.ec_zone_1_front",
            "sensor.ec_zone_1_back",
            "switch.crop_steering_zone_1_valve",
        ]
        result = discovery.group_entities(entity_ids)
        assert result.detected_zone_count == 1
        zone = result.zones[0]
        assert len(zone.candidate_entities.vwc_sensors) == 3
        assert len(zone.candidate_entities.ec_sensors) == 3
        assert set(zone.candidate_entities.vwc_sensors) == {
            "sensor.crop_steering_vwc_zone_1",
            "sensor.vwc_zone_1_front",
            "sensor.vwc_zone_1_back",
        }
        assert set(zone.candidate_entities.ec_sensors) == {
            "sensor.crop_steering_ec_zone_1",
            "sensor.ec_zone_1_front",
            "sensor.ec_zone_1_back",
        }

    def test_missing_zone_valve_produces_warning(self) -> None:
        entity_ids = [
            "sensor.crop_steering_vwc_zone_4",
            "sensor.crop_steering_ec_zone_4",
            # No zone 4 valve
            "switch.crop_steering_pump",
            "number.crop_steering_steering_intent",
            "sensor.crop_steering_rootsense_report_latest",
        ]
        result = discovery.group_entities(entity_ids)
        assert result.detected_zone_count == 1
        assert any(
            "Zone 4 has no valve entity" in w for w in result.warnings
        )

    def test_missing_rootsense_report_warning(self) -> None:
        entity_ids = [
            "sensor.crop_steering_vwc_zone_1",
            "sensor.crop_steering_ec_zone_1",
            "switch.crop_steering_zone_1_valve",
            "switch.crop_steering_pump",
            "number.crop_steering_steering_intent",
        ]
        result = discovery.group_entities(entity_ids)
        assert any("RootSense" in w for w in result.warnings)

    def test_non_crop_steering_entities_ignored(self) -> None:
        entity_ids = [
            "sensor.living_room_temp",
            "switch.bedroom_lamp",
            "binary_sensor.front_door",
            "number.thermostat_setpoint",
            "sensor.crop_steering_vwc_zone_1",
            "switch.crop_steering_zone_1_valve",
            "sensor.crop_steering_ec_zone_1",
        ]
        result = discovery.group_entities(entity_ids)
        assert result.detected_zone_count == 1
        # Only the crop_steering entities landed in candidates.
        zone = result.zones[0]
        all_zone_entities = (
            zone.candidate_entities.vwc_sensors
            + zone.candidate_entities.ec_sensors
            + zone.candidate_entities.valves
        )
        assert all(
            "crop_steering" in eid or "vwc_zone_" in eid or "ec_zone_" in eid
            for eid in all_zone_entities
        )

    def test_duplicate_entity_ids_deduped(self) -> None:
        entity_ids = [
            "sensor.crop_steering_vwc_zone_1",
            "sensor.crop_steering_vwc_zone_1",  # duplicate
            "switch.crop_steering_zone_1_valve",
            "switch.crop_steering_zone_1_valve",  # duplicate
        ]
        result = discovery.group_entities(entity_ids)
        zone = result.zones[0]
        assert len(zone.candidate_entities.vwc_sensors) == 1
        assert len(zone.candidate_entities.valves) == 1

    def test_idempotent_same_input_same_output(self) -> None:
        entity_ids = [
            "sensor.crop_steering_vwc_zone_1",
            "sensor.crop_steering_ec_zone_1",
            "switch.crop_steering_zone_1_valve",
            "switch.crop_steering_pump",
            "number.crop_steering_steering_intent",
            "number.crop_steering_ec_target_veg_p0",
            "sensor.crop_steering_rootsense_report_latest",
        ]
        first = discovery.group_entities(entity_ids)
        second = discovery.group_entities(entity_ids)
        # Compare as dicts via dataclasses.asdict-ish (we check the
        # interesting bits explicitly since dataclasses aren't trivially
        # equal across instances).
        assert first.detected_zone_count == second.detected_zone_count
        assert len(first.zones) == len(second.zones)
        for a, b in zip(first.zones, second.zones, strict=True):
            assert a.zone_index == b.zone_index
            assert (
                a.candidate_entities.vwc_sensors
                == b.candidate_entities.vwc_sensors
            )
            assert (
                a.candidate_entities.ec_sensors
                == b.candidate_entities.ec_sensors
            )
            assert a.candidate_entities.valves == b.candidate_entities.valves
        assert (
            first.room_level_candidates.ec_targets
            == second.room_level_candidates.ec_targets
        )
        assert first.warnings == second.warnings

    def test_generic_zone_suffix_lifts_zone_count(self) -> None:
        """An unmatched _zone_{N}_ entity still bumps detected zone count.

        Covers the user-renamed-install case — the canonical valve/VWC
        patterns miss, but a generic ``..._zone_4_...`` slug should still
        register zone 4 as detected (with empty candidates + a warning).
        """
        entity_ids = [
            "sensor.crop_steering_zone_4_status",  # not a canonical role
        ]
        result = discovery.group_entities(entity_ids)
        assert result.detected_zone_count == 1
        assert result.zones[0].zone_index == 4


# ---------------------------------------------------------------------------
# Orchestrator — discover()
# ---------------------------------------------------------------------------


class TestDiscoverOrchestrator:
    """The full discover() entry-point with a fake HA client."""

    async def test_calls_registry_and_states(self) -> None:
        client = _FakeHAClient(
            entities=[
                _entity("sensor.crop_steering_vwc_zone_1"),
                _entity("switch.crop_steering_zone_1_valve"),
            ],
            states=[
                _state("sensor.crop_steering_vwc_zone_1", "45.2"),
            ],
        )
        result = await discovery.discover(client)
        assert client.registry_calls == 1
        assert client.state_calls == 1
        assert result.detected_zone_count == 1

    async def test_state_only_entities_picked_up(self) -> None:
        """An entity that appears in get_states but not the registry is still grouped.

        Some HA integrations publish state without a registry row
        (template sensors, MQTT autodiscovery edge cases). The
        orchestrator should still see them.
        """
        client = _FakeHAClient(
            entities=[],
            states=[
                _state("sensor.crop_steering_vwc_zone_1"),
                _state("switch.crop_steering_zone_1_valve"),
                _state("sensor.crop_steering_ec_zone_1"),
            ],
        )
        result = await discovery.discover(client)
        assert result.detected_zone_count == 1
        assert (
            "sensor.crop_steering_vwc_zone_1"
            in result.zones[0].candidate_entities.vwc_sensors
        )

    async def test_idempotent_second_call(self) -> None:
        client = _FakeHAClient(
            entities=[
                _entity("sensor.crop_steering_vwc_zone_1"),
                _entity("sensor.crop_steering_ec_zone_1"),
                _entity("switch.crop_steering_zone_1_valve"),
                _entity("switch.crop_steering_pump"),
                _entity("number.crop_steering_steering_intent"),
                _entity("sensor.crop_steering_rootsense_report_latest"),
            ],
        )
        a = await discovery.discover(client)
        b = await discovery.discover(client)
        assert a.detected_zone_count == b.detected_zone_count
        assert a.warnings == b.warnings
        assert a.room_level_candidates.pump == b.room_level_candidates.pump

    async def test_registry_dedup_with_states(self) -> None:
        """Entity appearing in both registry and states must not duplicate."""
        client = _FakeHAClient(
            entities=[_entity("sensor.crop_steering_vwc_zone_1")],
            states=[_state("sensor.crop_steering_vwc_zone_1", "45")],
        )
        result = await discovery.discover(client)
        zone = result.zones[0]
        # Should appear once, not twice.
        assert (
            zone.candidate_entities.vwc_sensors.count(
                "sensor.crop_steering_vwc_zone_1"
            )
            == 1
        )
