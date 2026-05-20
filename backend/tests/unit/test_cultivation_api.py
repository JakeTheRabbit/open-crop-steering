"""Unit tests for :mod:`app.api.cultivation` — genetics / batches / plants / recipes.

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
from app.api import cultivation as cultivation_api
from app.core import acl
from app.core.auth import Identity, current_identity
from app.db import get_session
from app.models.batch import Batch
from app.models.genetics import Genetics
from app.models.grow_recipe import GrowRecipe
from app.models.grow_recipe_day_override import GrowRecipeDayOverride
from app.models.plant import Plant
from app.models.user import RoleName
from fastapi import FastAPI
from httpx import ASGITransport

pytestmark = pytest.mark.unit


_FAKE_ORG = "open-crop-steering"
_FIXED_NOW = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.UTC)


class _FakeResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = list(rows)

    def scalars(self) -> list[object]:
        return list(self._rows)


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

    monkeypatch.setattr(cultivation_api, "log_audit", _stub)


@pytest.fixture(autouse=True)
def _grant_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``acl.get_user_roles`` so the fake identity is admin."""

    async def _admin(_session: Any, _user_id: str) -> set[RoleName]:
        return {RoleName.admin}

    monkeypatch.setattr(acl, "get_user_roles", _admin)


def _app(session: _FakeSession) -> FastAPI:
    app = FastAPI()
    app.include_router(cultivation_api.router)

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


def _genetics_payload() -> dict[str, Any]:
    return {
        "name": "Northern Lights",
        "prefix": "NL",
        "type": "indica",
        "status": "active",
    }


def _batch_payload(genetics_id: str = "gen-1") -> dict[str, Any]:
    return {
        "batchCode": "B-001",
        "batchType": "standard_cultivation",
        "status": "vegetative",
        "currentPhase": 1.0,
        "phaseStartDate": 1_700_000_000_000,
        "targetPlantCount": 50,
        "initialPlantCount": 50,
        "currentPlantCount": 50,
        "genetics": genetics_id,
    }


def _plant_payload(genetics_id: str = "gen-1") -> dict[str, Any]:
    return {
        "plantId": "NL-001",
        "status": "vegetative",
        "isMotherPlant": False,
        "genetics": genetics_id,
    }


def _recipe_payload() -> dict[str, Any]:
    return {
        "name": "Standard 8-week",
        "recipeType": ["indoor", "hydroponic"],
        "isActive": True,
        "phases": [
            {
                "phaseName": "veg",
                "durationDays": 21.0,
                "order": 1.0,
            }
        ],
    }


class TestGenetics:
    async def test_create_returns_camel_and_stamps_org(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.post(
                "/api/cultivation/genetics", json=_genetics_payload()
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Northern Lights"
        assert body["prefix"] == "NL"
        assert body["orgId"] == _FAKE_ORG
        assert "id" in body
        assert "createdAt" in body
        assert session.commits == 1

    async def test_list_returns_camel_items(self) -> None:
        seeded = Genetics(
            id="gen-1",
            org_id=_FAKE_ORG,
            name="Northern Lights",
            prefix="NL",
            type="indica",
            status="active",
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        session = _FakeSession(prefilled=[seeded])
        async with await _client(session) as client:
            resp = await client.get("/api/cultivation/genetics")
        assert resp.status_code == 200
        items = resp.json()["genetics"]
        assert len(items) == 1
        assert items[0]["orgId"] == _FAKE_ORG


class TestBatches:
    async def test_create_returns_camel(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.post(
                "/api/cultivation/batches", json=_batch_payload()
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["batchCode"] == "B-001"
        assert body["currentPhase"] == 1.0
        assert body["orgId"] == _FAKE_ORG

    async def test_list_camel_items(self) -> None:
        seeded = Batch(
            id="b-1",
            org_id=_FAKE_ORG,
            batch_code="B-001",
            genetics="gen-1",
            batch_type="standard_cultivation",
            status="vegetative",
            current_phase=1.0,
            phase_start_date=1_700_000_000_000,
            target_plant_count=50,
            initial_plant_count=50,
            current_plant_count=50,
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        session = _FakeSession(prefilled=[seeded])
        async with await _client(session) as client:
            resp = await client.get("/api/cultivation/batches")
        assert resp.status_code == 200
        items = resp.json()["batches"]
        assert len(items) == 1
        assert items[0]["batchCode"] == "B-001"


class TestPlants:
    async def test_create_returns_camel(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.post(
                "/api/cultivation/plants", json=_plant_payload()
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["plantId"] == "NL-001"
        assert body["isMotherPlant"] is False
        assert body["orgId"] == _FAKE_ORG

    async def test_list_camel_items(self) -> None:
        seeded = Plant(
            id="plant-1",
            org_id=_FAKE_ORG,
            plant_id="NL-001",
            genetics="gen-1",
            status="vegetative",
            is_mother_plant=False,
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        session = _FakeSession(prefilled=[seeded])
        async with await _client(session) as client:
            resp = await client.get("/api/cultivation/plants")
        assert resp.status_code == 200
        items = resp.json()["plants"]
        assert len(items) == 1
        assert items[0]["plantId"] == "NL-001"


class TestGrowRecipes:
    async def test_create_returns_camel(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.post(
                "/api/cultivation/grow-recipes", json=_recipe_payload()
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Standard 8-week"
        assert body["recipeType"] == ["indoor", "hydroponic"]
        assert body["isActive"] is True
        assert body["orgId"] == _FAKE_ORG

    async def test_list_camel_items(self) -> None:
        seeded = GrowRecipe(
            id="r-1",
            org_id=_FAKE_ORG,
            name="Standard 8-week",
            recipe_type=["indoor", "hydroponic"],
            is_active=True,
            phases=[
                {"phaseName": "veg", "durationDays": 21.0, "order": 1.0}
            ],
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        session = _FakeSession(prefilled=[seeded])
        async with await _client(session) as client:
            resp = await client.get("/api/cultivation/grow-recipes")
        assert resp.status_code == 200
        items = resp.json()["growRecipes"]
        assert len(items) == 1
        assert items[0]["recipeType"] == ["indoor", "hydroponic"]


class TestGrowRecipeEffectiveTargets:
    """GET /grow-recipes/{id}/effective-targets — resolver-backed read."""

    def _recipe_with_targets(self) -> GrowRecipe:
        return GrowRecipe(
            id="r-1",
            org_id=_FAKE_ORG,
            name="Standard 14-day",
            recipe_type=["indoor"],
            is_active=True,
            phases=[
                {
                    "phaseName": "Veg",
                    "durationDays": 7.0,
                    "order": 1.0,
                    "targets": {
                        "temp_day": {
                            "value": 25.0,
                            "tolerance": 0.5,
                            "unit": "C",
                        }
                    },
                },
                {
                    "phaseName": "Flower",
                    "durationDays": 7.0,
                    "order": 2.0,
                    "targets": {
                        "temp_day": {
                            "value": 27.0,
                            "tolerance": 0.5,
                            "unit": "C",
                        }
                    },
                },
            ],
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )

    async def test_full_grid_returns_one_cell_per_day(self) -> None:
        recipe = self._recipe_with_targets()
        session = _FakeSession(prefilled=[recipe])
        async with await _client(session) as client:
            resp = await client.get(
                "/api/cultivation/grow-recipes/r-1/effective-targets"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cycleDayCount"] == 14
        cells = body["effectiveTargets"]
        # 14 days x 1 declared param.
        assert len(cells) == 14
        assert all(c["source"] == "phase_default" for c in cells)
        # Day 1 in Veg, day 14 in Flower.
        day1 = next(c for c in cells if c["day"] == 1)
        assert day1["value"] == 25.0
        assert day1["phaseName"] == "Veg"
        day14 = next(c for c in cells if c["day"] == 14)
        assert day14["value"] == 27.0
        assert day14["phaseName"] == "Flower"

    async def test_day_query_filters_to_single_day(self) -> None:
        recipe = self._recipe_with_targets()
        ov = GrowRecipeDayOverride(
            id="ov-1",
            org_id=_FAKE_ORG,
            recipe_id="r-1",
            day=3,
            param_name="temp_day",
            value=24.0,
            tolerance=0.3,
            unit="C",
        )
        session = _FakeSession(prefilled=[recipe, ov])
        async with await _client(session) as client:
            resp = await client.get(
                "/api/cultivation/grow-recipes/r-1/effective-targets?day=3"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cycleDayCount"] == 14
        cells = body["effectiveTargets"]
        assert len(cells) == 1
        assert cells[0]["day"] == 3
        assert cells[0]["value"] == 24.0
        assert cells[0]["source"] == "day_override"

    async def test_day_out_of_cycle_404(self) -> None:
        recipe = self._recipe_with_targets()
        session = _FakeSession(prefilled=[recipe])
        async with await _client(session) as client:
            resp = await client.get(
                "/api/cultivation/grow-recipes/r-1/effective-targets?day=99"
            )
        assert resp.status_code == 404

    async def test_unknown_recipe_404(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.get(
                "/api/cultivation/grow-recipes/no-such/effective-targets"
            )
        assert resp.status_code == 404


class TestListDayOverrides:
    """GET /grow-recipes/{id}/day-overrides — surgical-read endpoint."""

    def _recipe(self) -> GrowRecipe:
        return GrowRecipe(
            id="r-1",
            org_id=_FAKE_ORG,
            name="Standard 14-day",
            recipe_type=["indoor"],
            is_active=True,
            phases=[
                {
                    "phaseName": "Veg",
                    "durationDays": 14.0,
                    "order": 1.0,
                    "targets": {
                        "temp_day": {
                            "value": 25.0,
                            "tolerance": 0.5,
                            "unit": "C",
                        }
                    },
                }
            ],
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )

    async def test_returns_overrides_with_ids_and_timestamps(self) -> None:
        recipe = self._recipe()
        ov1 = GrowRecipeDayOverride(
            id="ov-1",
            org_id=_FAKE_ORG,
            recipe_id="r-1",
            day=3,
            param_name="temp_day",
            value=24.0,
            tolerance=0.3,
            unit="C",
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        ov2 = GrowRecipeDayOverride(
            id="ov-2",
            org_id=_FAKE_ORG,
            recipe_id="r-1",
            day=5,
            param_name="temp_day",
            value=26.0,
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        session = _FakeSession(prefilled=[recipe, ov1, ov2])
        async with await _client(session) as client:
            resp = await client.get(
                "/api/cultivation/grow-recipes/r-1/day-overrides"
            )
        assert resp.status_code == 200
        body = resp.json()
        overrides = body["overrides"]
        assert len(overrides) == 2
        # Every row carries the persisted id, recipeId, and timestamps —
        # this is the whole point of the endpoint (reducer parity).
        ids = {o["id"] for o in overrides}
        assert ids == {"ov-1", "ov-2"}
        assert all(o["recipeId"] == "r-1" for o in overrides)
        assert all(o["orgId"] == _FAKE_ORG for o in overrides)
        assert all("createdAt" in o and "updatedAt" in o for o in overrides)
        # camelCase keys.
        assert all("paramName" in o for o in overrides)

    async def test_unknown_recipe_404(self) -> None:
        session = _FakeSession()
        async with await _client(session) as client:
            resp = await client.get(
                "/api/cultivation/grow-recipes/no-such/day-overrides"
            )
        assert resp.status_code == 404


class TestReplaceDayOverrides:
    """PUT /grow-recipes/{id}/day-overrides — bulk DELETE-then-INSERT."""

    def _recipe(self) -> GrowRecipe:
        return GrowRecipe(
            id="r-1",
            org_id=_FAKE_ORG,
            name="Standard 14-day",
            recipe_type=["indoor"],
            is_active=True,
            phases=[
                {
                    "phaseName": "Veg",
                    "durationDays": 14.0,
                    "order": 1.0,
                    "targets": {
                        "temp_day": {
                            "value": 25.0,
                            "tolerance": 0.5,
                            "unit": "C",
                        }
                    },
                }
            ],
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )

    async def test_replace_inserts_new_overrides(self) -> None:
        recipe = self._recipe()
        session = _FakeSession(prefilled=[recipe])
        body = {
            "overrides": [
                {"day": 3, "paramName": "temp_day", "value": 24.0},
                {"day": 5, "paramName": "temp_day", "value": 26.0},
            ]
        }
        async with await _client(session) as client:
            resp = await client.put(
                "/api/cultivation/grow-recipes/r-1/day-overrides", json=body
            )
        assert resp.status_code == 200
        result = resp.json()
        assert result["deleted"] == 0
        assert result["inserted"] == 2
        assert len(result["overrides"]) == 2
        # Each override row carries the recipeId from the URL path.
        assert all(o["recipeId"] == "r-1" for o in result["overrides"])

    async def test_replace_deletes_existing(self) -> None:
        recipe = self._recipe()
        old = GrowRecipeDayOverride(
            id="ov-old",
            org_id=_FAKE_ORG,
            recipe_id="r-1",
            day=3,
            param_name="temp_day",
            value=23.0,
        )
        session = _FakeSession(prefilled=[recipe, old])
        body = {
            "overrides": [
                {"day": 5, "paramName": "temp_day", "value": 26.0},
            ]
        }
        async with await _client(session) as client:
            resp = await client.put(
                "/api/cultivation/grow-recipes/r-1/day-overrides", json=body
            )
        assert resp.status_code == 200
        result = resp.json()
        assert result["deleted"] == 1
        assert result["inserted"] == 1

    async def test_day_out_of_cycle_422(self) -> None:
        recipe = self._recipe()
        session = _FakeSession(prefilled=[recipe])
        body = {
            "overrides": [
                {"day": 99, "paramName": "temp_day", "value": 24.0},
            ]
        }
        async with await _client(session) as client:
            resp = await client.put(
                "/api/cultivation/grow-recipes/r-1/day-overrides", json=body
            )
        assert resp.status_code == 422

    async def test_unknown_recipe_404(self) -> None:
        session = _FakeSession()
        body: dict[str, Any] = {"overrides": []}
        async with await _client(session) as client:
            resp = await client.put(
                "/api/cultivation/grow-recipes/no-such/day-overrides",
                json=body,
            )
        assert resp.status_code == 404

    async def test_empty_overrides_list_clears_existing(self) -> None:
        recipe = self._recipe()
        old1 = GrowRecipeDayOverride(
            id="ov-1",
            org_id=_FAKE_ORG,
            recipe_id="r-1",
            day=3,
            param_name="temp_day",
            value=23.0,
        )
        old2 = GrowRecipeDayOverride(
            id="ov-2",
            org_id=_FAKE_ORG,
            recipe_id="r-1",
            day=5,
            param_name="temp_day",
            value=22.0,
        )
        session = _FakeSession(prefilled=[recipe, old1, old2])
        body: dict[str, Any] = {"overrides": []}
        async with await _client(session) as client:
            resp = await client.put(
                "/api/cultivation/grow-recipes/r-1/day-overrides", json=body
            )
        assert resp.status_code == 200
        result = resp.json()
        assert result["deleted"] == 2
        assert result["inserted"] == 0
