"""Integration tests for :mod:`app.api.audit` — the audit export API.

Mounts the real audit router on a throwaway FastAPI app, overrides
identity + session, and exercises the events list, chain verification,
CSV export, the PDF 501, and the qap-role gate. Runs against real
Postgres via the shared ``session`` fixture.
"""

from __future__ import annotations

import httpx
import pytest
from app.api import audit as audit_api
from app.core.audit import log_audit
from app.core.auth import Identity, current_identity
from app.db import get_session
from app.models.audit_event import AuditEventType
from app.models.user import User
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


def _app_with_identity(role_mode: str, session: AsyncSession) -> FastAPI:
    """Build an app serving the audit router with a fixed identity.

    ``role_mode`` is just the identity's ``user_id`` here; the actual
    role gate is decided by the ``user_roles`` rows the test seeds for
    that id.
    """
    app = FastAPI()
    app.include_router(audit_api.router)

    async def _fake_identity() -> Identity:
        return Identity(
            user_id=role_mode, display_name="Tester", mode="standalone"
        )

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


async def _seed(
    session: AsyncSession,
    uid: str,
    *,
    role: str | None,
    events: int = 0,
) -> None:
    """Seed a user, an optional role, and some audit events."""
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
    for i in range(events):
        await log_audit(
            session,
            event_type=AuditEventType.info_event,
            actor_id=uid,
            summary=f"{uid}-evt-{i}",
        )
    await session.flush()


async def test_list_events_requires_qap_role(
    session: AsyncSession,
) -> None:
    """An operator (below qap) gets 403 from the events list."""
    await _seed(session, "audapi-op", role="operator")
    app = _app_with_identity("audapi-op", session)
    async with _client(app) as client:
        resp = await client.get("/api/audit/events")
    assert resp.status_code == 403


async def test_list_events_allows_qap(session: AsyncSession) -> None:
    """A qap user can list audit events; payload is well-formed."""
    await _seed(session, "audapi-qap", role="qap", events=3)
    app = _app_with_identity("audapi-qap", session)
    async with _client(app) as client:
        resp = await client.get("/api/audit/events?limit=50")
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 50
    assert body["total"] >= 3
    assert isinstance(body["events"], list)
    # Chain fields are hex strings, not raw bytes.
    sample = body["events"][0]
    assert isinstance(sample["hmac"], str)
    assert isinstance(sample["prev_event_hash"], str)


async def test_list_events_admin_allowed(session: AsyncSession) -> None:
    """An admin (above qap) also passes the qap gate."""
    await _seed(session, "audapi-admin", role="admin", events=1)
    app = _app_with_identity("audapi-admin", session)
    async with _client(app) as client:
        resp = await client.get("/api/audit/events")
    assert resp.status_code == 200


async def test_verify_endpoint_reports_ok(session: AsyncSession) -> None:
    """The verify endpoint returns ok=True for an intact chain."""
    await _seed(session, "audapi-verify", role="qap", events=3)
    app = _app_with_identity("audapi-verify", session)
    async with _client(app) as client:
        resp = await client.get("/api/audit/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["first_bad_id"] is None
    assert body["rows_checked"] >= 3


async def test_verify_endpoint_requires_qap(session: AsyncSession) -> None:
    """A cultivator (below qap) is denied the verify endpoint."""
    await _seed(session, "audapi-cult", role="cultivator")
    app = _app_with_identity("audapi-cult", session)
    async with _client(app) as client:
        resp = await client.get("/api/audit/verify")
    assert resp.status_code == 403


async def test_csv_export_returns_csv(session: AsyncSession) -> None:
    """CSV export streams a text/csv body with a header row."""
    await _seed(session, "audapi-csv", role="qap", events=2)
    app = _app_with_identity("audapi-csv", session)
    async with _client(app) as client:
        resp = await client.get("/api/audit/export?format=csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    lines = resp.text.strip().splitlines()
    assert lines[0].startswith("id,occurred_at,event_type")
    assert len(lines) >= 3  # header + >=2 events


async def test_csv_export_writes_audit_export_event(
    session: AsyncSession,
) -> None:
    """Exporting writes an audit_export row recording the request."""
    await _seed(session, "audapi-exprow", role="qap", events=1)
    app = _app_with_identity("audapi-exprow", session)
    async with _client(app) as client:
        resp = await client.get("/api/audit/export?format=csv")
    assert resp.status_code == 200

    row = await session.execute(
        text(
            "SELECT count(*) FROM audit_event "
            "WHERE event_type = 'audit_export' AND actor_id = :a"
        ),
        {"a": "audapi-exprow"},
    )
    assert row.scalar_one() >= 1


async def test_pdf_export_returns_501(session: AsyncSession) -> None:
    """PDF export is intentionally not implemented (501, P13)."""
    await _seed(session, "audapi-pdf", role="qap")
    app = _app_with_identity("audapi-pdf", session)
    async with _client(app) as client:
        resp = await client.get("/api/audit/export?format=pdf")
    assert resp.status_code == 501
    assert "P13" in resp.json()["detail"]


async def test_export_requires_qap(session: AsyncSession) -> None:
    """An operator cannot export the audit log."""
    await _seed(session, "audapi-exp-op", role="operator")
    app = _app_with_identity("audapi-exp-op", session)
    async with _client(app) as client:
        resp = await client.get("/api/audit/export?format=csv")
    assert resp.status_code == 403
