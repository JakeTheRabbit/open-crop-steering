"""Integration tests for :mod:`app.api.approvals` — the SFW approvals API.

Mounts the real approvals router on a throwaway FastAPI app, overrides
identity + session, and exercises the list / detail views and the
approve / reject role gates. Runs against real Postgres.

Covered:

* ``GET /api/approvals`` lists open pending rows; ``GET .../{id}``
  returns the proposal + snapshot.
* A ``cultivator`` identity can approve (the proposal applies).
* An ``operator`` identity gets 403 on approve / reject (below the
  cultivator decision floor) but may still *view*.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
from app.api import approvals as approvals_api
from app.core.auth import Identity, current_identity
from app.core.sfw import create_pending
from app.db import get_session
from app.models.pending_approval import PendingApproval, PendingStatus
from app.models.recipe_revision import (
    RecipeRevision,
    RecipeRevisionParam,
    RecipeStatus,
)
from app.models.runtime_adjustment import RuntimeAdjustment
from app.models.sensor_snapshot import SensorSnapshot
from app.models.user import User
from app.schemas.llm_decision import LLM_DECISION_SCHEMA_VERSION
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------- app glue


def _app_with_identity(uid: str, session: AsyncSession) -> FastAPI:
    """Build an app serving the approvals router with a fixed identity.

    The role gate is decided by the ``user_roles`` rows the test seeds
    for ``uid`` — not by the identity object itself.
    """
    app = FastAPI()
    app.include_router(approvals_api.router)

    async def _fake_identity() -> Identity:
        return Identity(user_id=uid, display_name="Tester", mode="standalone")

    async def _fake_session() -> AsyncSession:
        return session

    app.dependency_overrides[current_identity] = _fake_identity
    app.dependency_overrides[get_session] = _fake_session
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    """Return an httpx client bound to the ASGI app."""
    return httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    )


@asynccontextmanager
async def _verify_session(
    engine: AsyncEngine,
) -> AsyncIterator[AsyncSession]:
    """Open a fresh session for post-request verification.

    The endpoint shares the conftest ``session`` and commits inside the
    request; re-querying through that same session after the ASGI client
    closes is brittle. A fresh session reads the committed state cleanly.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as verify:
        yield verify


# --------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(async_engine: AsyncEngine) -> AsyncIterator[None]:
    """Truncate committed tables these tests touch, before each test."""
    from app.core.effective import refresh_effective_targets  # noqa: PLC0415

    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with maker() as cleanup:
        for table in (
            "command_queue",
            "command_batch",
            "runtime_adjustment",
            "event_log",
            "sensor_snapshot",
            "pending_approval",
        ):
            await cleanup.execute(text(f"DELETE FROM {table}"))  # noqa: S608
        await cleanup.execute(
            text(
                "UPDATE recipe_revision SET status = 'superseded' "
                "WHERE status = 'approved'"
            )
        )
        await cleanup.execute(text("DELETE FROM recipe_revision_param"))
        await cleanup.execute(text("DELETE FROM recipe_revision"))
        await cleanup.execute(text("DELETE FROM users WHERE id LIKE 'apr-%'"))
        await cleanup.commit()
        await refresh_effective_targets(cleanup)
        await cleanup.commit()
    yield


def _proposal(snapshot_id: int) -> dict:
    """A valid ocs.llm_decision.v1 proposal with one concrete change."""
    return {
        "schema_version": LLM_DECISION_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "assessment": "Room drifting warm.",
        "recommended_action_id": None,
        "proposed_changes": [
            {
                "param_name": "temp_day",
                "direction": "decrease",
                "delta": -0.3,
                "unit": "C",
                "rationale": "Trim toward band centre.",
            }
        ],
        "confidence": 0.8,
        "reason_codes": ["EC-001"],
        "human_summary": "Lower temp_day by 0.3 C.",
        "requires_human": True,
        "day_index": 1,
    }


async def _seed_user(
    session: AsyncSession, uid: str, *, role: str | None
) -> None:
    """Seed a user and (optionally) one role row."""
    session.add(User(id=uid, display_name=f"User {uid}"))
    await session.flush()
    if role is not None:
        await session.execute(
            text(
                "INSERT INTO user_roles (user_id, role_name) "
                "VALUES (:u, :r)"
            ),
            {"u": uid, "r": role},
        )
        await session.flush()


async def _seed_pending(
    session: AsyncSession, *, uid: str, room_id: str
) -> PendingApproval:
    """Seed + commit a recipe + snapshot + an open pending approval."""
    from app.core.effective import refresh_effective_targets  # noqa: PLC0415

    rev = RecipeRevision(
        room_id=room_id,
        version=1,
        name=f"Recipe {room_id}",
        status=RecipeStatus.approved,
        created_by=uid,
        approved_by=uid,
    )
    session.add(rev)
    await session.flush()
    session.add(
        RecipeRevisionParam(
            recipe_revision_id=rev.id,
            room_id=room_id,
            day_index=1,
            param_name="temp_day",
            value=26.0,
            tolerance=0.5,
        )
    )
    await session.commit()
    await refresh_effective_targets(session)
    await session.commit()

    snapshot = SensorSnapshot(
        room_id=room_id,
        payload={"room_id": room_id, "schema_version": "ocs.snapshot.v1"},
        recipe_revision_id=rev.id,
        cycle_day=1,
        rollout_stage="stage_3",
    )
    session.add(snapshot)
    await session.commit()

    pending = await create_pending(
        session,
        room_id=room_id,
        proposal=_proposal(snapshot.id),
        snapshot_id=snapshot.id,
        llm_call_id=None,
        summary="Lower temp_day by 0.3 C",
    )
    await session.commit()
    return pending


# --------------------------------------------------------------------- list


async def test_list_approvals_returns_open_rows(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """An operator can list open pending approvals."""
    await _seed_user(session, "apr-op", role="operator")
    pending = await _seed_pending(session, uid="apr-op", room_id="apr-r1")

    app = _app_with_identity("apr-op", session)
    async with _client(app) as client:
        resp = await client.get("/api/approvals")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    ids = [a["id"] for a in body["approvals"]]
    assert pending.id in ids
    row = next(a for a in body["approvals"] if a["id"] == pending.id)
    assert row["status"] == "open"
    assert row["room_id"] == "apr-r1"


async def test_get_approval_detail_includes_proposal_and_snapshot(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """The detail view carries the proposal payload + the snapshot."""
    await _seed_user(session, "apr-det", role="operator")
    pending = await _seed_pending(session, uid="apr-det", room_id="apr-r2")

    app = _app_with_identity("apr-det", session)
    async with _client(app) as client:
        resp = await client.get(f"/api/approvals/{pending.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == pending.id
    assert body["proposal"]["proposed_changes"][0]["param_name"] == "temp_day"
    assert body["snapshot"] is not None
    assert body["snapshot"]["recipe_revision_id"] is not None


async def test_get_unknown_approval_is_404(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A detail request for a missing approval is a 404."""
    await _seed_user(session, "apr-404", role="operator")
    app = _app_with_identity("apr-404", session)
    async with _client(app) as client:
        resp = await client.get("/api/approvals/99999")
    assert resp.status_code == 404


# --------------------------------------------------------------------- approve


async def test_cultivator_can_approve(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A cultivator identity can approve; the proposal applies."""
    await _seed_user(session, "apr-cult", role="cultivator")
    pending = await _seed_pending(session, uid="apr-cult", room_id="apr-r3")

    app = _app_with_identity("apr-cult", session)
    async with _client(app) as client:
        resp = await client.post(f"/api/approvals/{pending.id}/approve")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["decision_channel"] == "ui"

    async with _verify_session(async_engine) as verify:
        refreshed = await verify.get(PendingApproval, pending.id)
        assert refreshed is not None
        assert refreshed.status is PendingStatus.approved
        assert refreshed.decided_by == "apr-cult"
        # The approve applied the overlay.
        overlays = await verify.scalar(
            select(func.count())
            .select_from(RuntimeAdjustment)
            .where(RuntimeAdjustment.room_id == "apr-r3")
        )
        assert int(overlays or 0) == 1


async def test_qap_can_approve(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A qap identity (above cultivator) also passes the approve gate."""
    await _seed_user(session, "apr-qap", role="qap")
    pending = await _seed_pending(session, uid="apr-qap", room_id="apr-r4")

    app = _app_with_identity("apr-qap", session)
    async with _client(app) as client:
        resp = await client.post(f"/api/approvals/{pending.id}/approve")
    assert resp.status_code == 200


async def test_operator_approve_is_403(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """An operator identity is denied approve (below cultivator)."""
    await _seed_user(session, "apr-op2", role="operator")
    pending = await _seed_pending(session, uid="apr-op2", room_id="apr-r5")

    app = _app_with_identity("apr-op2", session)
    async with _client(app) as client:
        resp = await client.post(f"/api/approvals/{pending.id}/approve")
    assert resp.status_code == 403

    # The pending row is untouched and nothing applied.
    async with _verify_session(async_engine) as verify:
        refreshed = await verify.get(PendingApproval, pending.id)
        assert refreshed is not None
        assert refreshed.status is PendingStatus.open
        overlays = await verify.scalar(
            select(func.count())
            .select_from(RuntimeAdjustment)
            .where(RuntimeAdjustment.room_id == "apr-r5")
        )
        assert int(overlays or 0) == 0


# --------------------------------------------------------------------- reject


async def test_cultivator_can_reject(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A cultivator identity can reject a pending approval."""
    await _seed_user(session, "apr-rej", role="cultivator")
    pending = await _seed_pending(session, uid="apr-rej", room_id="apr-r6")

    app = _app_with_identity("apr-rej", session)
    async with _client(app) as client:
        resp = await client.post(
            f"/api/approvals/{pending.id}/reject",
            json={"notes": "not warranted"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


async def test_operator_reject_is_403(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """An operator identity is denied reject (below cultivator)."""
    await _seed_user(session, "apr-op3", role="operator")
    pending = await _seed_pending(session, uid="apr-op3", room_id="apr-r7")

    app = _app_with_identity("apr-op3", session)
    async with _client(app) as client:
        resp = await client.post(f"/api/approvals/{pending.id}/reject")
    assert resp.status_code == 403


async def test_approve_already_decided_is_409(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """Approving an already-decided pending row is a 409 conflict."""
    await _seed_user(session, "apr-409", role="cultivator")
    pending = await _seed_pending(session, uid="apr-409", room_id="apr-r8")

    app = _app_with_identity("apr-409", session)
    async with _client(app) as client:
        first = await client.post(f"/api/approvals/{pending.id}/approve")
        assert first.status_code == 200
        second = await client.post(f"/api/approvals/{pending.id}/approve")
    assert second.status_code == 409
