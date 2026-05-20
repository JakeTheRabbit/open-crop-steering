"""Unit tests for :mod:`app.api.equipment` — durable assets.

Same throwaway-app pattern as ``test_sites_api.py``. ``log_audit`` is
monkeypatched to a no-op so the audit trigger (real-Postgres only) is
not exercised here.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from app.api import equipment as equipment_api
from app.api import sensors as sensors_api
from app.core import acl
from app.core.auth import Identity, current_identity
from app.db import get_session
from app.models.equipment import Equipment
from app.models.sensor_integration import SensorIntegration
from app.models.user import RoleName
from fastapi import FastAPI
from httpx import ASGITransport

pytestmark = pytest.mark.unit


_FAKE_ORG = "open-crop-steering"
_FIXED_NOW = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.UTC)


class _FakeScalars:
    """Stand-in for SQLAlchemy's :class:`ScalarResult`."""

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

    monkeypatch.setattr(equipment_api, "log_audit", _stub)
    # The HA-integration lazy-resolver lives on ``sensors_api`` but is
    # called from ``equipment_api`` for the lazy-create flow; silence
    # its own ``log_audit`` too.
    monkeypatch.setattr(sensors_api, "log_audit", _stub)


@pytest.fixture(autouse=True)
def _grant_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``acl.get_user_roles`` so the fake identity is admin."""

    async def _admin(_session: Any, _user_id: str) -> set[RoleName]:
        return {RoleName.admin}

    monkeypatch.setattr(acl, "get_user_roles", _admin)


def _app(session: _FakeSession) -> FastAPI:
    app = FastAPI()
    app.include_router(equipment_api.router)

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


def _payload() -> dict[str, Any]:
    return {
        "name": "Mini Split A",
        "code": "ac-a",
        "type": "hvac",
        "status": "operational",
        "isActive": True,
    }


class TestEquipment:
    async def test_create_returns_camel_and_stamps_org(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.post("/api/equipment", json=_payload())
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Mini Split A"
        assert body["isActive"] is True
        assert body["orgId"] == _FAKE_ORG
        assert "id" in body
        assert "createdAt" in body
        assert isinstance(body["createdAt"], int)
        assert session.commits == 1

    async def test_list_returns_camel_items(self) -> None:
        seeded = Equipment(
            id="eq-1",
            org_id=_FAKE_ORG,
            name="Mini Split A",
            code="ac-a",
            type="hvac",
            status="operational",
            is_active=True,
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        session = _FakeSession(prefilled=[seeded])
        async with await _client(session) as client:
            resp = await client.get("/api/equipment")
        assert resp.status_code == 200
        body = resp.json()
        assert "equipment" in body
        items = body["equipment"]
        assert len(items) == 1
        assert items[0]["isActive"] is True
        assert items[0]["orgId"] == _FAKE_ORG

    async def test_delete_then_get_returns_404(self) -> None:
        seeded = Equipment(
            id="eq-1",
            org_id=_FAKE_ORG,
            name="Mini Split A",
            code="ac-a",
            type="hvac",
            status="operational",
            is_active=True,
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        session = _FakeSession(prefilled=[seeded])
        async with await _client(session) as client:
            d = await client.delete("/api/equipment/eq-1")
            assert d.status_code == 200
            assert d.json() == {"deleted": "eq-1"}
            g = await client.get("/api/equipment/eq-1")
            assert g.status_code == 404

    async def test_external_id_query_filter(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.get(
                "/api/equipment",
                params={"externalId": "climate.f2_ac_door"},
            )
        assert resp.status_code == 200
        assert "equipment" in resp.json()

    async def test_lazy_create_ha_integration_on_external_id(self) -> None:
        session = _FakeSession()
        payload = {
            **_payload(),
            "externalId": "climate.f2_ac_door_f2_ac_door",
        }
        async with await _client(session) as client:
            resp = await client.post("/api/equipment", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["integrationId"] is not None
        integrations = [
            r
            for (t, _), r in session.store.items()
            if t is SensorIntegration
        ]
        assert len(integrations) == 1
        assert integrations[0].type == "home_assistant"
        assert body["integrationId"] == integrations[0].id
