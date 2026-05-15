"""Integration tests for :mod:`app.core.auth` — identity resolution.

Covers the standalone-mode JWT round-trip, HA-Ingress header parsing
for add-on mode, the ``current_identity`` mode dispatch, and the
Telegram chat-id → HA-user resolution.

Marked ``integration`` (per the Phase 5 brief): the JWT helpers need
the ``HMAC_KEY_1`` env var that the ``session`` fixture chain wires,
and :func:`resolve_telegram_user` needs real Postgres.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.config import get_settings
from app.core.auth import (
    HEADER_USER_DISPLAY_NAME,
    HEADER_USER_ID,
    HEADER_USER_NAME,
    Identity,
    create_access_token,
    current_identity,
    decode_token,
    resolve_telegram_user,
)
from app.models.telegram_user_map import TelegramUserMap
from app.models.user import User
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


def _make_request(headers: dict[str, str]) -> Request:
    """Build a bare ASGI ``Request`` carrying the given headers."""
    raw = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in headers.items()
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw,
        "query_string": b"",
    }
    return Request(scope)


# --- JWT round-trip ----------------------------------------------------


async def test_jwt_round_trip(session: AsyncSession) -> None:
    """A minted token decodes back to the same subject."""
    token = create_access_token("jwt-user-1")
    claims = decode_token(token)
    assert claims["sub"] == "jwt-user-1"
    assert "exp" in claims
    assert "iat" in claims


async def test_jwt_carries_extra_claims(session: AsyncSession) -> None:
    """Extra claims survive the encode/decode round-trip."""
    token = create_access_token(
        "jwt-user-2", extra_claims={"display_name": "Jane Cultivator"}
    )
    claims = decode_token(token)
    assert claims["display_name"] == "Jane Cultivator"


async def test_jwt_expired_token_rejected(session: AsyncSession) -> None:
    """An already-expired token raises HTTP 401."""
    token = create_access_token(
        "jwt-user-3", expires_in=dt.timedelta(seconds=-1)
    )
    with pytest.raises(HTTPException) as exc:
        decode_token(token)
    assert exc.value.status_code == 401


async def test_jwt_tampered_token_rejected(session: AsyncSession) -> None:
    """A token with a mangled signature raises HTTP 401."""
    token = create_access_token("jwt-user-4")
    tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    with pytest.raises(HTTPException) as exc:
        decode_token(tampered)
    assert exc.value.status_code == 401


# --- Ingress header parsing -------------------------------------------


async def test_ingress_headers_full(session: AsyncSession) -> None:
    """current_identity reads HA Ingress headers in add-on mode."""
    settings = get_settings()
    original = settings.mode
    settings.mode = "addon"
    try:
        request = _make_request(
            {
                HEADER_USER_ID: "ha-user-42",
                HEADER_USER_NAME: "operator1",
                HEADER_USER_DISPLAY_NAME: "Operator One",
            }
        )
        identity = await current_identity(request)
        assert identity.user_id == "ha-user-42"
        assert identity.display_name == "Operator One"
        assert identity.mode == "addon"
    finally:
        settings.mode = original


async def test_ingress_display_name_falls_back_to_username(
    session: AsyncSession,
) -> None:
    """Display name falls back to X-Remote-User-Name then the id."""
    settings = get_settings()
    original = settings.mode
    settings.mode = "addon"
    try:
        identity = await current_identity(
            _make_request(
                {HEADER_USER_ID: "ha-user-7", HEADER_USER_NAME: "just-a-name"}
            )
        )
        assert identity.display_name == "just-a-name"

        identity_bare = await current_identity(
            _make_request({HEADER_USER_ID: "ha-user-9"})
        )
        assert identity_bare.display_name == "ha-user-9"
    finally:
        settings.mode = original


async def test_ingress_missing_user_id_raises_401(
    session: AsyncSession,
) -> None:
    """A request with no X-Remote-User-Id raises HTTP 401 in add-on mode."""
    settings = get_settings()
    original = settings.mode
    settings.mode = "addon"
    try:
        with pytest.raises(HTTPException) as exc:
            await current_identity(_make_request({}))
        assert exc.value.status_code == 401
    finally:
        settings.mode = original


async def test_current_identity_standalone_uses_jwt(
    session: AsyncSession,
) -> None:
    """In standalone mode current_identity validates a bearer JWT."""
    settings = get_settings()
    original = settings.mode
    settings.mode = "standalone"
    try:
        token = create_access_token("standalone-user")
        identity = await current_identity(
            _make_request({"Authorization": f"Bearer {token}"})
        )
        assert identity.user_id == "standalone-user"
        assert identity.mode == "standalone"
    finally:
        settings.mode = original


async def test_current_identity_standalone_missing_bearer_raises(
    session: AsyncSession,
) -> None:
    """Standalone mode rejects a request with no bearer token."""
    settings = get_settings()
    original = settings.mode
    settings.mode = "standalone"
    try:
        with pytest.raises(HTTPException) as exc:
            await current_identity(_make_request({}))
        assert exc.value.status_code == 401
    finally:
        settings.mode = original


# --- Telegram identity -------------------------------------------------


async def test_resolve_telegram_user_mapped(session: AsyncSession) -> None:
    """A mapped chat_id resolves to its HA user."""
    session.add(User(id="tg-user-1", display_name="Telegram User"))
    await session.flush()
    session.add(
        TelegramUserMap(
            chat_id="555111", user_id="tg-user-1", created_by="tg-user-1"
        )
    )
    await session.flush()

    resolved = await resolve_telegram_user(session, "555111")
    assert resolved is not None
    assert resolved.id == "tg-user-1"
    # Integer chat_id is coerced to string for the lookup.
    resolved_int = await resolve_telegram_user(session, 555111)
    assert resolved_int is not None
    assert resolved_int.id == "tg-user-1"


async def test_resolve_telegram_user_unmapped_returns_none(
    session: AsyncSession,
) -> None:
    """An unmapped chat_id resolves to None (caller must reject + audit)."""
    assert await resolve_telegram_user(session, "no-such-chat") is None


def test_identity_dataclass_defaults() -> None:
    """A fresh Identity has an empty role set until RBAC stamps it."""
    identity = Identity(user_id="x", display_name="X", mode="standalone")
    assert identity.roles == set()
