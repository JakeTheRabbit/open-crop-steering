"""Unit tests for :mod:`app.api.integrations.ha_irrigation`.

httpx + ASGITransport against a throwaway app with the HA client,
session, and identity dependencies overridden. Same fake-session
pattern as ``test_sites_api.py``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from app.api import sensors as sensors_api
from app.api.integrations import ha_irrigation as ha_route
from app.core import acl
from app.core.auth import Identity, current_identity
from app.db import get_session
from app.ha_client import HAClientError
from app.integrations.ha_irrigation import registry as registry_module
from app.models.building import Building
from app.models.equipment import Equipment
from app.models.location import Location
from app.models.room import Room
from app.models.sensor import Sensor
from app.models.user import RoleName
from fastapi import FastAPI
from httpx import ASGITransport

pytestmark = pytest.mark.unit


_FAKE_ORG = "open-crop-steering"


# ---------------------------------------------------------------------------
# Fake session — same shape as test_ha_irrigation_registry's, plus a smart
# Select walker that handles where-clause equality filters.
# ---------------------------------------------------------------------------


class _FakeScalars:
    def __init__(self, rows: list[object]) -> None:
        self._rows = list(rows)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None


class _FakeResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = list(rows)

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(list(self._rows))


def _matches_binary_expr(row: Any, expr: Any) -> bool:
    try:
        col_name = expr.left.key
    except AttributeError:
        raise AssertionError(
            f"fake _matches_binary_expr cannot evaluate {expr!r}"
        ) from None
    expected = expr.right.value
    return getattr(row, col_name, None) == expected


class _FakeSession:
    def __init__(self, prefilled: list[object] | None = None) -> None:
        self.store: dict[tuple[type, str], object] = {}
        for row in prefilled or []:
            self.store[(type(row), row.id)] = row  # type: ignore[attr-defined]
        self.commits = 0
        self.rollbacks = 0

    def add(self, obj: object) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = str(uuid.uuid4())  # type: ignore[attr-defined]
        self.store[(type(obj), obj.id)] = obj  # type: ignore[attr-defined]

    async def flush(self) -> None:
        return None

    async def refresh(self, obj: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def get(self, model: type, key: str) -> object | None:
        return self.store.get((model, key))

    async def delete(self, obj: object) -> None:
        self.store.pop((type(obj), obj.id), None)  # type: ignore[attr-defined]

    async def execute(self, stmt: Any) -> _FakeResult:
        try:
            entity = stmt.column_descriptions[0]["entity"]
        except (AttributeError, IndexError, KeyError, TypeError):
            return _FakeResult([])
        rows = [r for (t, _), r in self.store.items() if t is entity]
        for clause in getattr(stmt, "_where_criteria", ()):
            rows = [r for r in rows if _matches_binary_expr(r, clause)]
        return _FakeResult(rows)


# ---------------------------------------------------------------------------
# Fake HA client + builder
# ---------------------------------------------------------------------------


@dataclass
class _FakeHAClient:
    """Fake matching :class:`HAClientProtocol` plus async context manager."""

    entities: list[dict[str, Any]]
    states: list[dict[str, Any]]
    raise_on_registry: Exception | None = None

    async def __aenter__(self) -> _FakeHAClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_registry(self) -> dict[str, list[dict[str, Any]]]:
        if self.raise_on_registry is not None:
            raise self.raise_on_registry
        return {
            "areas": [],
            "entities": list(self.entities),
            "devices": [],
        }

    async def get_states(self) -> list[dict[str, Any]]:
        return list(self.states)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _noop_log_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _stub(_session: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(registry_module, "log_audit", _stub)
    monkeypatch.setattr(sensors_api, "log_audit", _stub)


@pytest.fixture(autouse=True)
def _grant_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _admin(_session: Any, _user_id: str) -> set[RoleName]:
        return {RoleName.admin}

    monkeypatch.setattr(acl, "get_user_roles", _admin)


def _entity(entity_id: str) -> dict[str, Any]:
    return {"entity_id": entity_id, "platform": "crop_steering"}


def _build_app(
    session: _FakeSession, fake_ha: _FakeHAClient | None
) -> FastAPI:
    """Build a throwaway app with the relevant dependency overrides."""
    app = FastAPI()
    app.include_router(ha_route.router)

    async def _fake_identity() -> Identity:
        return Identity(
            user_id="u-1", display_name="Tester", mode="standalone"
        )

    async def _fake_session() -> _FakeSession:
        return session

    def _fake_build_ha() -> _FakeHAClient:
        assert fake_ha is not None, "no fake HA client provided"
        return fake_ha

    app.dependency_overrides[current_identity] = _fake_identity
    app.dependency_overrides[get_session] = _fake_session
    app.dependency_overrides[ha_route._build_ha_client] = _fake_build_ha
    return app


async def _client(
    session: _FakeSession, fake_ha: _FakeHAClient | None = None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=_build_app(session, fake_ha)),
        base_url="http://test",
    )


# ---------------------------------------------------------------------------
# Discover endpoint
# ---------------------------------------------------------------------------


class TestDiscoverEndpoint:
    async def test_returns_camelcase_payload(self) -> None:
        ha = _FakeHAClient(
            entities=[
                _entity("sensor.crop_steering_vwc_zone_1"),
                _entity("sensor.crop_steering_ec_zone_1"),
                _entity("switch.crop_steering_zone_1_valve"),
                _entity("switch.crop_steering_pump"),
                _entity("switch.crop_steering_main_valve"),
                _entity("number.crop_steering_steering_intent"),
                _entity("binary_sensor.crop_steering_anomaly_active"),
                _entity("select.crop_steering_irrigation_phase"),
                _entity("sensor.crop_steering_rootsense_report_latest"),
                _entity("number.crop_steering_ec_target_veg_p1"),
            ],
            states=[],
        )
        session = _FakeSession()
        async with await _client(session, ha) as client:
            resp = await client.get("/api/integrations/ha-irrigation/discover")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["suggestedRoom"]["name"] == "Crop Steering"
        assert (
            body["suggestedRoom"]["externalSystemId"]
            == "ha_irrigation_strategy"
        )
        assert body["detectedZoneCount"] == 1
        assert len(body["zones"]) == 1
        zone = body["zones"][0]
        assert zone["zoneIndex"] == 1
        assert zone["suggestedLocationLabel"] == "Zone 1"
        assert zone["candidateEntities"]["vwcSensors"] == [
            "sensor.crop_steering_vwc_zone_1"
        ]
        assert zone["candidateEntities"]["ecSensors"] == [
            "sensor.crop_steering_ec_zone_1"
        ]
        assert zone["candidateEntities"]["valves"] == [
            "switch.crop_steering_zone_1_valve"
        ]
        room = body["roomLevelCandidates"]
        assert room["pump"] == ["switch.crop_steering_pump"]
        assert room["mainlineValve"] == ["switch.crop_steering_main_valve"]
        assert room["steeringIntent"] == [
            "number.crop_steering_steering_intent"
        ]
        assert room["ecTargets"] == [
            "number.crop_steering_ec_target_veg_p1"
        ]
        assert room["anomalyBinarySensor"] == [
            "binary_sensor.crop_steering_anomaly_active"
        ]
        assert room["phaseSelect"] == [
            "select.crop_steering_irrigation_phase"
        ]
        assert room["rootsenseReportSensor"] == [
            "sensor.crop_steering_rootsense_report_latest"
        ]
        assert body["warnings"] == []

    async def test_502_on_ha_failure(self) -> None:
        ha = _FakeHAClient(
            entities=[], states=[],
            raise_on_registry=HAClientError("connection refused"),
        )
        session = _FakeSession()
        async with await _client(session, ha) as client:
            resp = await client.get("/api/integrations/ha-irrigation/discover")
        assert resp.status_code == 502
        body = resp.json()
        assert "Home Assistant unreachable" in body["detail"]

    async def test_warnings_surface_in_response(self) -> None:
        ha = _FakeHAClient(
            entities=[
                _entity("sensor.crop_steering_vwc_zone_4"),
                _entity("sensor.crop_steering_ec_zone_4"),
                # Zone 4 has no valve -> warning
            ],
            states=[],
        )
        session = _FakeSession()
        async with await _client(session, ha) as client:
            resp = await client.get("/api/integrations/ha-irrigation/discover")
        assert resp.status_code == 200
        body = resp.json()
        warning_blob = " ".join(body["warnings"])
        assert "Zone 4" in warning_blob


# ---------------------------------------------------------------------------
# Register endpoint
# ---------------------------------------------------------------------------


def _register_body() -> dict[str, Any]:
    return {
        "room": {"name": "Crop Steering"},
        "zones": [
            {
                "zoneIndex": 1,
                "locationLabel": "Zone 1",
                "vwcSensors": ["sensor.crop_steering_vwc_zone_1"],
                "ecSensors": ["sensor.crop_steering_ec_zone_1"],
                "valves": ["switch.crop_steering_zone_1_valve"],
            },
            {
                "zoneIndex": 2,
                "locationLabel": "Zone 2",
                "vwcSensors": ["sensor.crop_steering_vwc_zone_2"],
                "ecSensors": ["sensor.crop_steering_ec_zone_2"],
                "valves": ["switch.crop_steering_zone_2_valve"],
            },
        ],
        "roomLevel": {
            "pump": "switch.crop_steering_pump",
            "mainlineValve": "switch.crop_steering_main_valve",
            "steeringIntent": "number.crop_steering_steering_intent",
            "ecTargets": [
                "number.crop_steering_ec_target_veg_p1",
                "number.crop_steering_ec_target_veg_p2",
            ],
            "anomalyBinarySensor": (
                "binary_sensor.crop_steering_anomaly_active"
            ),
            "phaseSelect": "select.crop_steering_irrigation_phase",
            "rootsenseReportSensor": (
                "sensor.crop_steering_rootsense_report_latest"
            ),
        },
    }


class TestRegisterEndpoint:
    async def test_creates_full_topology_returns_camel_response(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.post(
                "/api/integrations/ha-irrigation/register",
                json=_register_body(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "roomId" in body
        assert "buildingId" in body
        assert body["locationsCreated"] == 2
        assert body["locationsUpdated"] == 0
        # 2 (VWC) + 2 (EC) + 2 (room-level anomaly + rootsense) = 6 sensors.
        assert body["sensorsCreated"] == 6
        assert body["sensorsUpdated"] == 0
        # 2 (zone valves) + pump + mainline + steering_intent
        # + phase_select + 2 (ec_targets) = 8 equipment rows.
        assert body["equipmentCreated"] == 8
        assert body["equipmentUpdated"] == 0
        # Records nested map
        recs = body["records"]
        assert recs["roomId"] == body["roomId"]
        assert recs["buildingId"] == body["buildingId"]
        assert len(recs["locations"]) == 2
        for loc in recs["locations"]:
            assert "zoneIndex" in loc
            assert "locationId" in loc
        assert len(recs["sensors"]) == 6
        for s in recs["sensors"]:
            assert "externalId" in s
            assert "sensorId" in s
            assert s["type"] in {"moisture", "ec", "other"}
            assert s["scope"] in {"room", "zone"}
        assert len(recs["equipment"]) == 8
        for e in recs["equipment"]:
            assert "externalId" in e
            assert "equipmentId" in e
            assert e["scope"] in {"room", "zone"}
        # Side effect: rows actually landed in the fake session.
        assert any(isinstance(o, Building) for (_, _), o in session.store.items())
        assert any(isinstance(o, Room) for (_, _), o in session.store.items())
        assert any(isinstance(o, Location) for (_, _), o in session.store.items())
        assert any(isinstance(o, Sensor) for (_, _), o in session.store.items())
        assert any(isinstance(o, Equipment) for (_, _), o in session.store.items())
        # And the whole thing committed once.
        assert session.commits == 1

    async def test_idempotent_second_post(self) -> None:
        session = _FakeSession()
        body = _register_body()
        async with await _client(session) as client:
            first = await client.post(
                "/api/integrations/ha-irrigation/register", json=body
            )
            second = await client.post(
                "/api/integrations/ha-irrigation/register", json=body
            )
        assert first.status_code == 200
        assert second.status_code == 200
        a = first.json()
        b = second.json()
        # Same ids on both calls.
        assert a["roomId"] == b["roomId"]
        assert a["buildingId"] == b["buildingId"]
        # No new rows on second call.
        assert b["locationsCreated"] == 0
        assert b["sensorsCreated"] == 0
        assert b["equipmentCreated"] == 0
        # Updates may legitimately appear if the upsert path "touches"
        # rows even when values are unchanged — assert specifically
        # that no NEW rows landed by counting the store.
        sensors = [r for (t, _), r in session.store.items() if t is Sensor]
        eq = [r for (t, _), r in session.store.items() if t is Equipment]
        locs = [r for (t, _), r in session.store.items() if t is Location]
        assert len(sensors) == 6
        assert len(eq) == 8
        assert len(locs) == 2

    async def test_unknown_building_returns_404(self) -> None:
        session = _FakeSession()
        body = _register_body()
        body["room"]["buildingId"] = "bld-missing"
        async with await _client(session) as client:
            resp = await client.post(
                "/api/integrations/ha-irrigation/register", json=body
            )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]

    async def test_minimal_body_only_room(self) -> None:
        """An almost-empty body — no zones, no roomLevel — still registers."""
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.post(
                "/api/integrations/ha-irrigation/register",
                json={"room": {"name": "Crop Steering"}},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["locationsCreated"] == 0
        assert body["sensorsCreated"] == 0
        assert body["equipmentCreated"] == 0
