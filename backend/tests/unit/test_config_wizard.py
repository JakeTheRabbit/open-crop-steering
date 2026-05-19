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
        "temp_sensor": "sensor.f1_temp",
        "leaf_temp_sensor": "sensor.f1_leaf_temp",
        "rh_sensor": "sensor.f1_rh",
        "co2_sensor": "sensor.f1_co2",
        "under_canopy_rh_probe": "sensor.f1_canopy_rh",
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
                "zones": [
                    {
                        "zone_id": "z1",
                        "pump_entity": "switch.f1_pump",
                        "vwc_sensor": "sensor.f1_z1_vwc",
                        "ec_sensor": "sensor.f1_z1_ec",
                        # valve_entity absent
                    }
                ],
            }
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        codes = {f["code"] for f in body["hard_refusals"]}
        assert "irrigation_zone_missing_valve_or_pump" in codes

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
                "temp_sensor": "sensor.f1_temp",
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
        del payload["under_canopy_rh_probe"]
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
