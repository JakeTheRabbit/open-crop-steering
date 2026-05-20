"""Unit tests for :mod:`app.api.sites` — buildings / rooms / locations.

These run as unit tests against a throwaway FastAPI app with the
``get_session`` and ``current_identity`` dependencies overridden with
in-memory fakes — no Postgres / Docker required. The fake session
plays back deterministic shapes so the CRUD wire contract (camelCase
output, server-stamped ``orgId``, audit-log call) is exercised
end-to-end without touching real infrastructure.

The fake :class:`_FakeSession` records every call (``adds``, ``deletes``,
``flushes``, ``commits``) so audit-side-effect assertions stay simple.
``log_audit`` itself is monkeypatched to a no-op so the audit trigger
(which only runs on real Postgres) is not exercised here — that is
covered by the integration suite.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from app.api import sites
from app.core import acl
from app.core.auth import Identity, current_identity
from app.db import get_session
from app.models.building import Building
from app.models.location import Location
from app.models.room import Room
from app.models.user import RoleName
from fastapi import FastAPI
from httpx import ASGITransport

pytestmark = pytest.mark.unit


_FAKE_ORG = "open-crop-steering"
_FIXED_NOW = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.UTC)


class _FakeResult:
    """Minimal stand-in for SQLAlchemy's :class:`Result` / scalars chain."""

    def __init__(self, rows: list[object]) -> None:
        self._rows = list(rows)

    def scalars(self) -> list[object]:
        return list(self._rows)


class _FakeSession:
    """In-memory async session — keyed on ``(type, id)``.

    Implements just the subset of :class:`AsyncSession` the sites
    router uses: ``add`` / ``flush`` / ``refresh`` / ``commit`` /
    ``get`` / ``delete`` / ``execute`` / ``rollback``. ``execute`` is
    smart enough to walk the SQLAlchemy ``Select`` it gets so the
    list endpoint can return the rows that match.
    """

    def __init__(self, prefilled: list[object] | None = None) -> None:
        self.store: dict[tuple[type, str], object] = {}
        for row in prefilled or []:
            self.store[(type(row), row.id)] = row  # type: ignore[attr-defined]
        self.flushes = 0
        self.commits = 0
        self.deletes: list[object] = []

    def add(self, obj: object) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = str(uuid.uuid4())  # type: ignore[attr-defined]
        if getattr(obj, "created_at", None) is None:
            obj.created_at = _FIXED_NOW  # type: ignore[attr-defined]
        if getattr(obj, "updated_at", None) is None:
            obj.updated_at = _FIXED_NOW  # type: ignore[attr-defined]
        self.store[(type(obj), obj.id)] = obj  # type: ignore[attr-defined]

    async def flush(self) -> None:
        self.flushes += 1

    async def refresh(self, obj: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def get(self, model: type, key: str) -> object | None:
        return self.store.get((model, key))

    async def delete(self, obj: object) -> None:
        self.deletes.append(obj)
        self.store.pop((type(obj), obj.id), None)  # type: ignore[attr-defined]

    async def execute(self, stmt: Any) -> _FakeResult:
        # Walk the Select and return all rows of the queried entity type.
        try:
            entity = stmt.column_descriptions[0]["entity"]
        except (AttributeError, IndexError, KeyError, TypeError):
            return _FakeResult([])
        rows = [r for (t, _), r in self.store.items() if t is entity]
        return _FakeResult(rows)


@pytest.fixture(autouse=True)
def _noop_log_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``log_audit`` with a no-op so no audit trigger fires."""

    async def _stub(_session: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sites, "log_audit", _stub)


@pytest.fixture(autouse=True)
def _grant_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``require_role(admin)`` pass for the fake identity.

    The role lookup hits ``acl.get_user_roles`` against the test
    session; the fake session has no ``user_roles`` rows so the user
    would default to ``operator`` and every admin-gated route would
    403. Stub the lookup to return ``{admin}`` instead.
    """

    async def _admin(_session: Any, _user_id: str) -> set[RoleName]:
        return {RoleName.admin}

    monkeypatch.setattr(acl, "get_user_roles", _admin)


def _app(session: _FakeSession) -> FastAPI:
    """Build a throwaway app with overridden deps."""
    app = FastAPI()
    app.include_router(sites.router)

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


class TestBuildings:
    async def test_create_returns_camelcase_and_stamps_org(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.post(
                "/api/sites/buildings",
                json={
                    "name": "Main",
                    "address": "1 Test St",
                    "stories": ["G", "1"],
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Main"
        assert body["address"] == "1 Test St"
        assert body["stories"] == ["G", "1"]
        assert body["orgId"] == _FAKE_ORG  # server-stamped
        assert "id" in body
        assert "createdAt" in body
        assert isinstance(body["createdAt"], int)  # epoch ms
        # Side effects: the row landed in the session and was committed.
        assert session.commits == 1
        assert any(isinstance(o, Building) for (_, _), o in session.store.items())

    async def test_list_returns_camelcase_items(self) -> None:
        seeded = Building(
            id="bld-1",
            org_id=_FAKE_ORG,
            name="Main",
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        session = _FakeSession(prefilled=[seeded])
        async with await _client(session) as client:
            resp = await client.get("/api/sites/buildings")
        assert resp.status_code == 200
        body = resp.json()
        assert "buildings" in body
        assert len(body["buildings"]) == 1
        item = body["buildings"][0]
        assert item["id"] == "bld-1"
        assert item["orgId"] == _FAKE_ORG
        assert isinstance(item["createdAt"], int)


class TestRooms:
    async def test_create_with_camel_input_and_camel_output(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.post(
                "/api/sites/rooms",
                json={
                    "name": "Flower 1",
                    "buildingId": "bld-1",
                    "positionX": 1.5,
                    "positionY": 2.5,
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["buildingId"] == "bld-1"
        assert body["positionX"] == 1.5
        assert body["orgId"] == _FAKE_ORG
        assert "id" in body
        assert "createdAt" in body

    async def test_list_camel_items(self) -> None:
        seeded = Room(
            id="room-1",
            org_id=_FAKE_ORG,
            building_id="bld-1",
            name="Flower 1",
            position_x=1.5,
            position_y=2.5,
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        session = _FakeSession(prefilled=[seeded])
        async with await _client(session) as client:
            resp = await client.get("/api/sites/rooms")
        assert resp.status_code == 200
        body = resp.json()
        items = body["rooms"]
        assert len(items) == 1
        assert items[0]["buildingId"] == "bld-1"
        assert items[0]["positionX"] == 1.5

    async def test_404_on_missing_get_one(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.get("/api/sites/rooms/nope")
        assert resp.status_code == 404
        assert "nope" in resp.json()["detail"]


class TestLocations:
    async def test_create_camel_round_trip(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.post(
                "/api/sites/locations",
                json={
                    "label": "Row A",
                    "roomId": "room-1",
                    "capacity": 24,
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["label"] == "Row A"
        assert body["roomId"] == "room-1"
        assert body["capacity"] == 24
        assert body["orgId"] == _FAKE_ORG

    async def test_list_camel_items(self) -> None:
        seeded = Location(
            id="loc-1",
            org_id=_FAKE_ORG,
            room_id="room-1",
            label="Row A",
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        session = _FakeSession(prefilled=[seeded])
        async with await _client(session) as client:
            resp = await client.get("/api/sites/locations")
        assert resp.status_code == 200
        body = resp.json()
        items = body["locations"]
        assert len(items) == 1
        assert items[0]["roomId"] == "room-1"
