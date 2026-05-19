"""Unit tests for :mod:`app.api.config_wizard` — the room-config wizard.

The validate endpoint is pure (no database, no side effects), so these
run as unit tests against a throwaway FastAPI app with no ``session``
fixture — no Postgres / Docker required.

Coverage: a config missing a required coupling produces a hard refusal
(``ok`` is ``False``); a fail-soft case produces a warning, not a
refusal (``ok`` stays ``True``); a fully-equipped config passes clean.
"""

from __future__ import annotations

import httpx
import pytest
from app.api import config_wizard
from fastapi import FastAPI
from httpx import ASGITransport

pytestmark = pytest.mark.unit


def _app() -> FastAPI:
    """Build a throwaway app serving only the config-wizard router."""
    app = FastAPI()
    app.include_router(config_wizard.router)
    return app


async def _validate(payload: dict) -> httpx.Response:
    """POST a config payload to the validate endpoint."""
    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.post("/api/config-wizard/validate", json=payload)


def _fully_equipped_env_room() -> dict:
    """A room with environmental + PPFD control and every required entity."""
    return {
        "room_id": "F1",
        "env_control_enabled": True,
        "ppfd_control_enabled": True,
        "light_entities": ["light.f1_lights"],
        "temp_sensors": ["sensor.f1_temp"],
        "leaf_temp_sensors": ["sensor.f1_leaf_temp"],
        "rh_sensors": ["sensor.f1_rh"],
        "co2_sensors": ["sensor.f1_co2"],
        "under_canopy_rh_probes": ["sensor.f1_canopy_rh"],
        "cooling_capacity_entity": "climate.f1_ac",
    }


class TestHardRefusals:
    """A missing required coupling blocks the save."""

    async def test_ppfd_without_lights_is_hard_refusal(self) -> None:
        resp = await _validate(
            {
                "room_id": "F1",
                "ppfd_control_enabled": True,
                # light_entities deliberately absent
            }
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        codes = {f["code"] for f in body["hard_refusals"]}
        assert "ppfd_without_lights" in codes
        assert all(f["hard"] is True for f in body["hard_refusals"])

    async def test_irrigation_zone_missing_valve_is_hard_refusal(self) -> None:
        resp = await _validate(
            {
                "room_id": "F1",
                "irrigation_control_enabled": True,
                "irrigation_pump_entities": ["switch.f1_pump"],
                "zones": [
                    {
                        "zone_id": "z1",
                        "vwc_sensors": ["sensor.f1_z1_vwc"],
                        "ec_sensors": ["sensor.f1_z1_ec"],
                        # valve_entities absent
                    }
                ],
            }
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        codes = {f["code"] for f in body["hard_refusals"]}
        assert "irrigation_zone_missing_valve" in codes

    async def test_tank_control_without_doser_is_hard_refusal(self) -> None:
        resp = await _validate(
            {
                "room_id": "F1",
                "tank_control_enabled": True,
                "tanks": [
                    {
                        "tank_id": "t1",
                        "ph_sensor": "sensor.t1_ph",
                        "ec_sensor": "sensor.t1_ec",
                        "doser_entities": [],
                    }
                ],
            }
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        codes = {f["code"] for f in body["hard_refusals"]}
        assert "tank_without_doser_pumps" in codes

    async def test_env_control_missing_sensors_is_hard_refusal(self) -> None:
        resp = await _validate(
            {
                "room_id": "F1",
                "env_control_enabled": True,
                "temp_sensors": ["sensor.f1_temp"],
                # leaf_temp / rh / co2 sensors absent
            }
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        codes = {f["code"] for f in body["hard_refusals"]}
        assert "env_control_missing_sensors" in codes


class TestFailSoftWarnings:
    """A fail-soft coupling warns but does not block the save."""

    async def test_dehu_ac_no_reheat_is_warning_not_refusal(self) -> None:
        """EC-004 — dehu + AC, no reheat: a warning, ``ok`` stays True."""
        payload = _fully_equipped_env_room()
        payload["dehumidifier_entities"] = ["humidifier.f1_dehu"]
        payload["ac_entities"] = ["climate.f1_ac"]
        # reheat_entities deliberately absent
        resp = await _validate(payload)
        assert resp.status_code == 200
        body = resp.json()
        # No hard refusal — the config is still saveable.
        assert body["ok"] is True
        assert body["hard_refusals"] == []
        warning_codes = {f["code"] for f in body["warnings"]}
        assert "EC-004" in warning_codes
        assert all(f["hard"] is False for f in body["warnings"])
        # And the constraint is recorded for the AI supervisor.
        assert any("reheat" in note for note in body["coupling_notes"])

    async def test_exhaust_with_co2_is_warning(self) -> None:
        """EC-005 — exhaust + CO2 enrichment: a fail-soft warning."""
        payload = _fully_equipped_env_room()
        payload["co2_control_enabled"] = True
        payload["co2_solenoid_entities"] = ["switch.f1_co2"]
        payload["exhaust_entities"] = ["fan.f1_exhaust"]
        resp = await _validate(payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        warning_codes = {f["code"] for f in body["warnings"]}
        assert "EC-005" in warning_codes

    async def test_missing_under_canopy_probe_is_warning(self) -> None:
        """EC-009 — no under-canopy RH probe: a fail-soft warning."""
        payload = _fully_equipped_env_room()
        del payload["under_canopy_rh_probes"]
        resp = await _validate(payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        warning_codes = {f["code"] for f in body["warnings"]}
        assert "EC-009" in warning_codes


class TestCleanConfig:
    """A fully-equipped config passes with no findings."""

    async def test_fully_equipped_room_is_clean(self) -> None:
        payload = _fully_equipped_env_room()
        # Give it a cooling-headroom source so EC-001 does not warn,
        # and an under-canopy probe so EC-009 does not warn.
        resp = await _validate(payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["hard_refusals"] == []
        assert body["warnings"] == []

    async def test_minimal_room_no_control_enabled_is_clean(self) -> None:
        """A room with no control surface enabled has nothing to validate."""
        resp = await _validate({"room_id": "F9"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["hard_refusals"] == []

    async def test_unknown_field_is_rejected(self) -> None:
        """``extra='forbid'`` — an unknown key is a 422, not silently kept."""
        resp = await _validate(
            {"room_id": "F1", "totally_unknown_field": True}
        )
        assert resp.status_code == 422


class TestMultiSensorRoles:
    """Sensor roles accept several entities; legacy scalar keys still load."""

    async def test_multiple_sensors_per_role_accepted(self) -> None:
        """A room can map several entities to one sensor role."""
        payload = _fully_equipped_env_room()
        payload["temp_sensors"] = [
            "sensor.f1_temp_top",
            "sensor.f1_temp_mid",
            "sensor.f1_temp_canopy",
        ]
        payload["co2_sensors"] = ["sensor.f1_co2_a", "sensor.f1_co2_b"]
        payload["vwc_sensors"] = ["sensor.f1_z1_vwc", "sensor.f1_z2_vwc"]
        payload["pm25_sensors"] = ["sensor.f1_pm25"]
        resp = await _validate(payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["hard_refusals"] == []

    async def test_legacy_scalar_sensor_keys_accepted(self) -> None:
        """A config saved with the pre-multi-sensor scalar keys loads.

        ``RoomEquipmentMap`` folds the old ``*_sensor`` strings forward
        into the plural list keys, so an ``equipment_map`` persisted
        under the old schema still validates against ``extra='forbid'``
        and the four required env sensors are seen as present.
        """
        resp = await _validate(
            {
                "room_id": "F1",
                "env_control_enabled": True,
                "temp_sensor": "sensor.f1_temp",
                "leaf_temp_sensor": "sensor.f1_leaf_temp",
                "rh_sensor": "sensor.f1_rh",
                "co2_sensor": "sensor.f1_co2",
            }
        )
        assert resp.status_code == 200
        codes = {f["code"] for f in resp.json()["hard_refusals"]}
        assert "env_control_missing_sensors" not in codes


class TestIrrigationSupply:
    """Irrigation: a shared room pump + mainline valves, per-zone valves."""

    @staticmethod
    def _irrigated_room() -> dict:
        """A room with irrigation control fully and validly equipped."""
        return {
            "room_id": "F1",
            "irrigation_control_enabled": True,
            "irrigation_pump_entities": ["switch.f1_irrigation_pump"],
            "mainline_valve_entities": [
                "switch.f1_mainline_valve",
                "switch.f1_manifold_valve",
            ],
            "zones": [
                {
                    "zone_id": "zone1",
                    "valve_entities": ["switch.f1_zone1_valve"],
                    "vwc_sensors": ["sensor.f1_z1_vwc"],
                    "ec_sensors": ["sensor.f1_z1_ec"],
                }
            ],
        }

    async def test_fully_equipped_irrigation_is_clean(self) -> None:
        """Pump + mainline valves + a complete zone → no hard refusals."""
        resp = await _validate(self._irrigated_room())
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["hard_refusals"] == []

    async def test_irrigation_without_pump_is_hard_refusal(self) -> None:
        """Irrigation needs a shared room-level pump."""
        payload = self._irrigated_room()
        del payload["irrigation_pump_entities"]
        resp = await _validate(payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        codes = {f["code"] for f in body["hard_refusals"]}
        assert "irrigation_without_pump" in codes

    async def test_mainline_valve_absent_is_not_a_refusal(self) -> None:
        """A room may feed straight off the pump — mainline is optional."""
        payload = self._irrigated_room()
        del payload["mainline_valve_entities"]
        resp = await _validate(payload)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    async def test_legacy_zone_keys_accepted(self) -> None:
        """A zone saved with the pre-multi scalar keys still validates.

        ``ZoneConfig`` folds the old ``valve_entity`` / ``vwc_sensor`` /
        ``ec_sensor`` scalars into the plural lists and drops the
        now-room-level ``pump_entity`` — so an old zone config neither
        trips ``extra='forbid'`` nor reports a missing valve / sensor.
        """
        resp = await _validate(
            {
                "room_id": "F1",
                "irrigation_control_enabled": True,
                "irrigation_pump_entities": ["switch.f1_pump"],
                "zones": [
                    {
                        "zone_id": "zone1",
                        "valve_entity": "switch.f1_zone1_valve",
                        "pump_entity": "switch.f1_pump",
                        "vwc_sensor": "sensor.f1_z1_vwc",
                        "ec_sensor": "sensor.f1_z1_ec",
                    }
                ],
            }
        )
        assert resp.status_code == 200
        codes = {f["code"] for f in resp.json()["hard_refusals"]}
        assert "irrigation_zone_missing_valve" not in codes
        assert "irrigation_zone_missing_vwc_or_ec_sensor" not in codes
