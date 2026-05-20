"""Unit tests for :mod:`app.api.sensors` — sensors / readings / integrations.

Same throwaway-app pattern as ``test_sites_api.py`` — overrides
``get_session`` and ``current_identity`` with in-memory fakes; no
Postgres / Docker. ``log_audit`` is monkeypatched to a no-op so the
audit trigger (real-Postgres only) is not exercised here.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from app.api import sensors as sensors_api
from app.core import acl
from app.core.auth import Identity, current_identity
from app.db import get_session
from app.models.sensor import Sensor
from app.models.sensor_integration import SensorIntegration
from app.models.sensor_reading import SensorReading
from app.models.user import RoleName
from fastapi import FastAPI
from httpx import ASGITransport

pytestmark = pytest.mark.unit


_FAKE_ORG = "open-crop-steering"
_FIXED_NOW = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.UTC)


class _FakeScalars:
    """Stand-in for SQLAlchemy's :class:`ScalarResult`.

    Implements just the subset the routers exercise: iteration (for
    list endpoints) and ``.first()`` (for the lazy HA-integration
    resolver lookup).
    """

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


class _FakeSession:
    """Same shape as the sites test fake — entity-typed in-memory store."""

    def __init__(self, prefilled: list[object] | None = None) -> None:
        self.store: dict[tuple[type, str], object] = {}
        for row in prefilled or []:
            self.store[(type(row), row.id)] = row  # type: ignore[attr-defined]
        self.commits = 0

    def add(self, obj: object) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = str(uuid.uuid4())  # type: ignore[attr-defined]
        if getattr(obj, "created_at", None) is None:
            obj.created_at = _FIXED_NOW  # type: ignore[attr-defined]
        if getattr(obj, "updated_at", None) is None:
            obj.updated_at = _FIXED_NOW  # type: ignore[attr-defined]
        self.store[(type(obj), obj.id)] = obj  # type: ignore[attr-defined]

    async def flush(self) -> None:
        return None

    async def refresh(self, obj: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

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
        return _FakeResult(rows)


@pytest.fixture(autouse=True)
def _noop_log_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _stub(_session: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sensors_api, "log_audit", _stub)


@pytest.fixture(autouse=True)
def _grant_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``acl.get_user_roles`` so the fake identity is admin."""

    async def _admin(_session: Any, _user_id: str) -> set[RoleName]:
        return {RoleName.admin}

    monkeypatch.setattr(acl, "get_user_roles", _admin)


def _app(session: _FakeSession) -> FastAPI:
    app = FastAPI()
    app.include_router(sensors_api.sensors_router)
    app.include_router(sensors_api.readings_router)
    app.include_router(sensors_api.integrations_router)

    async def _fake_identity() -> Identity:
        return Identity(user_id="u-1", display_name="Tester", mode="standalone")

    async def _fake_session() -> _FakeSession:
        return session

    app.dependency_overrides[current_identity] = _fake_identity
    app.dependency_overrides[get_session] = _fake_session
    return app


async def _client(session: _FakeSession) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=_app(session)),
        base_url="http://test",
    )


def _sensor_payload() -> dict[str, Any]:
    return {
        "name": "Top Temp",
        "code": "sensor-top-temp",
        "type": "temperature",
        "dataUnit": "°C",
        "status": "active",
        "isActive": True,
    }


def _integration_payload() -> dict[str, Any]:
    return {
        "name": "Home Assistant",
        "type": "home_assistant",
        "status": "connected",
        "syncEnabled": True,
        "syncInterval": 60,
    }


def _reading_payload() -> dict[str, Any]:
    return {
        "sensorId": "sensor-1",
        "timestamp": 1_700_000_000_000,
        "value": 24.1,
        "unit": "°C",
        "hour": 1_700_000_000_000,
        "day": 1_700_000_000_000,
    }


class TestSensors:
    async def test_create_returns_camel_and_stamps_org(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.post("/api/sensors", json=_sensor_payload())
        assert resp.status_code == 200
        body = resp.json()
        assert body["dataUnit"] == "°C"
        assert body["isActive"] is True
        assert body["orgId"] == _FAKE_ORG
        assert "id" in body
        assert "createdAt" in body
        assert isinstance(body["createdAt"], int)
        assert session.commits == 1

    async def test_list_camel_items(self) -> None:
        seeded = Sensor(
            id="sensor-1",
            org_id=_FAKE_ORG,
            name="Top Temp",
            code="sensor-top-temp",
            type="temperature",
            data_unit="°C",
            status="active",
            is_active=True,
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        session = _FakeSession(prefilled=[seeded])
        async with await _client(session) as client:
            resp = await client.get("/api/sensors")
        assert resp.status_code == 200
        body = resp.json()
        items = body["sensors"]
        assert len(items) == 1
        assert items[0]["dataUnit"] == "°C"
        assert items[0]["orgId"] == _FAKE_ORG

    async def test_404_on_missing_get_one(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.get("/api/sensors/nope")
        assert resp.status_code == 404

    async def test_external_id_query_filter(self) -> None:
        """The entity picker uses ``?externalId=...`` to check for duplicates."""
        a = Sensor(
            id="sensor-a",
            org_id=_FAKE_ORG,
            name="A",
            code="a",
            type="temperature",
            data_unit="°C",
            status="active",
            is_active=True,
            external_id="sensor.f2_back_left_temp",
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        b = Sensor(
            id="sensor-b",
            org_id=_FAKE_ORG,
            name="B",
            code="b",
            type="temperature",
            data_unit="°C",
            status="active",
            is_active=True,
            external_id="sensor.f2_front_right_temp",
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        session = _FakeSession(prefilled=[a, b])
        # The fake _FakeSession does not honour SQLAlchemy WHERE clauses
        # (it returns every row of the queried entity type), so this
        # asserts only that the endpoint accepts the parameter cleanly —
        # the WHERE-clause shape itself is covered by integration tests
        # against real Postgres.
        async with await _client(session) as client:
            resp = await client.get(
                "/api/sensors", params={"externalId": "sensor.f2_back_left_temp"}
            )
        assert resp.status_code == 200
        assert "sensors" in resp.json()

    async def test_lazy_create_ha_integration_on_external_id(self) -> None:
        """Posting a sensor with ``externalId`` but no ``integrationId``
        lazy-creates the singleton HA integration row and attaches the
        sensor to it.
        """
        session = _FakeSession()
        payload = {
            **_sensor_payload(),
            "externalId": "sensor.f2_scd41_back_left_temperature",
        }
        async with await _client(session) as client:
            resp = await client.post("/api/sensors", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["integrationId"] is not None
        # The HA integration row was created as a side effect.
        integrations = [
            r
            for (t, _), r in session.store.items()
            if t is SensorIntegration
        ]
        assert len(integrations) == 1
        assert integrations[0].type == "home_assistant"
        assert body["integrationId"] == integrations[0].id


class TestSensorReadings:
    async def test_create_returns_camel(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.post(
                "/api/sensor-readings", json=_reading_payload()
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["sensorId"] == "sensor-1"
        assert body["value"] == 24.1
        assert body["orgId"] == _FAKE_ORG

    async def test_list_camel_items(self) -> None:
        seeded = SensorReading(
            id="r-1",
            org_id=_FAKE_ORG,
            sensor_id="sensor-1",
            timestamp=1_700_000_000_000,
            value=24.1,
            unit="°C",
            hour=1_700_000_000_000,
            day=1_700_000_000_000,
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        session = _FakeSession(prefilled=[seeded])
        async with await _client(session) as client:
            resp = await client.get("/api/sensor-readings")
        assert resp.status_code == 200
        body = resp.json()
        items = body["sensorReadings"]
        assert len(items) == 1
        assert items[0]["sensorId"] == "sensor-1"


class TestSensorIntegrations:
    async def test_create_returns_camel(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.post(
                "/api/sensor-integrations", json=_integration_payload()
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "home_assistant"
        assert body["syncEnabled"] is True
        assert body["syncInterval"] == 60
        assert body["orgId"] == _FAKE_ORG

    async def test_list_camel_items(self) -> None:
        seeded = SensorIntegration(
            id="int-1",
            org_id=_FAKE_ORG,
            name="Home Assistant",
            type="home_assistant",
            status="connected",
            sync_enabled=True,
            sync_interval=60,
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        session = _FakeSession(prefilled=[seeded])
        async with await _client(session) as client:
            resp = await client.get("/api/sensor-integrations")
        assert resp.status_code == 200
        body = resp.json()
        items = body["sensorIntegrations"]
        assert len(items) == 1
        assert items[0]["syncInterval"] == 60
