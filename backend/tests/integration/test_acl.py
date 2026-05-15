"""Integration tests for :mod:`app.core.acl` — the RBAC core.

Exercises the role hierarchy helpers, :func:`get_user_roles` against
real Postgres, and the :func:`require_role` dependency's allow/deny
behaviour via a tiny FastAPI app + ``httpx`` so the full dependency
wiring (identity + session + role lookup) is covered.
"""

from __future__ import annotations

from typing import Annotated

import httpx
import pytest
from app.core.acl import (
    DEFAULT_ROLE,
    ROLE_ORDER,
    effective_role,
    get_user_roles,
    has_min_role,
    max_role,
    require_role,
    role_rank,
)
from app.core.auth import Identity, current_identity
from app.db import get_session
from app.models.user import RoleName, User, UserRole
from fastapi import Depends, FastAPI
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


# --- pure hierarchy helpers (no DB) ------------------------------------


def test_role_order_is_strictly_ascending() -> None:
    """ROLE_ORDER ranks operator < cultivator < qap < admin."""
    ranks = [role_rank(r) for r in ROLE_ORDER]
    assert ranks == sorted(ranks)
    assert role_rank(RoleName.operator) < role_rank(RoleName.cultivator)
    assert role_rank(RoleName.cultivator) < role_rank(RoleName.qap)
    assert role_rank(RoleName.qap) < role_rank(RoleName.admin)


def test_max_role_picks_highest() -> None:
    """max_role returns the most-privileged role in the set."""
    assert max_role({RoleName.operator, RoleName.qap}) == RoleName.qap
    assert max_role({RoleName.cultivator}) == RoleName.cultivator
    assert max_role({RoleName.admin, RoleName.operator}) == RoleName.admin


def test_max_role_empty_set_defaults_to_operator() -> None:
    """An empty role set resolves to the read-only default role."""
    assert max_role(set()) == DEFAULT_ROLE == RoleName.operator


def test_has_min_role_hierarchy() -> None:
    """A higher role satisfies every lower requirement, not vice versa."""
    admin = {RoleName.admin}
    assert has_min_role(admin, RoleName.operator)
    assert has_min_role(admin, RoleName.qap)
    assert has_min_role(admin, RoleName.admin)

    operator = {RoleName.operator}
    assert has_min_role(operator, RoleName.operator)
    assert not has_min_role(operator, RoleName.cultivator)
    assert not has_min_role(operator, RoleName.admin)


def test_has_min_role_unmapped_user_is_operator() -> None:
    """An empty set satisfies operator-gated routes only."""
    assert has_min_role(set(), RoleName.operator)
    assert not has_min_role(set(), RoleName.cultivator)


# --- get_user_roles against Postgres -----------------------------------


async def _seed_user_with_roles(
    session: AsyncSession, uid: str, roles: list[RoleName]
) -> None:
    """Insert a user and grant the given roles, then flush."""
    session.add(User(id=uid, display_name=f"User {uid}"))
    await session.flush()
    for role in roles:
        session.add(UserRole(user_id=uid, role_name=role))
    await session.flush()


async def test_get_user_roles_returns_granted_roles(
    session: AsyncSession,
) -> None:
    """get_user_roles returns exactly the granted role set."""
    await _seed_user_with_roles(
        session, "acl-multi", [RoleName.cultivator, RoleName.qap]
    )
    roles = await get_user_roles(session, "acl-multi")
    assert roles == {RoleName.cultivator, RoleName.qap}


async def test_get_user_roles_unmapped_user_empty(
    session: AsyncSession,
) -> None:
    """An unmapped user id yields an empty set (treated as operator)."""
    assert await get_user_roles(session, "acl-nobody-here") == set()


async def test_effective_role_resolves_highest(
    session: AsyncSession,
) -> None:
    """effective_role collapses the role set to its highest member."""
    await _seed_user_with_roles(
        session, "acl-eff", [RoleName.operator, RoleName.admin]
    )
    assert await effective_role(session, "acl-eff") == RoleName.admin
    # Unmapped → default operator.
    assert await effective_role(session, "acl-eff-missing") == RoleName.operator


# --- require_role dependency via a tiny FastAPI app --------------------


def _build_app(fake_user_id: str, session: AsyncSession) -> FastAPI:
    """Build a minimal app whose identity + session are overridden.

    ``current_identity`` is replaced with a fixed standalone identity
    and ``get_session`` with the test's transactional session, so
    :func:`require_role` runs end-to-end without HA headers or a second
    DB connection.
    """
    app = FastAPI()

    @app.get("/needs-cultivator")
    async def _needs_cultivator(
        identity: Annotated[Identity, Depends(require_role(RoleName.cultivator))],
    ) -> dict[str, str]:
        return {"actor": identity.user_id}

    @app.get("/needs-qap")
    async def _needs_qap(
        identity: Annotated[Identity, Depends(require_role(RoleName.qap))],
    ) -> dict[str, str]:
        return {"actor": identity.user_id}

    async def _fake_identity() -> Identity:
        return Identity(
            user_id=fake_user_id, display_name="Fake", mode="standalone"
        )

    async def _fake_session() -> AsyncSession:
        return session

    app.dependency_overrides[current_identity] = _fake_identity
    app.dependency_overrides[get_session] = _fake_session
    return app


async def _client(app: FastAPI) -> httpx.AsyncClient:
    """Return an httpx client bound to the ASGI app."""
    return httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    )


async def test_require_role_allows_sufficient_role(
    session: AsyncSession,
) -> None:
    """A cultivator passes a cultivator-gated route."""
    await _seed_user_with_roles(session, "acl-cult", [RoleName.cultivator])
    app = _build_app("acl-cult", session)
    async with await _client(app) as client:
        resp = await client.get("/needs-cultivator")
    assert resp.status_code == 200
    assert resp.json() == {"actor": "acl-cult"}


async def test_require_role_allows_higher_role(
    session: AsyncSession,
) -> None:
    """An admin passes a qap-gated route (hierarchy)."""
    await _seed_user_with_roles(session, "acl-admin", [RoleName.admin])
    app = _build_app("acl-admin", session)
    async with await _client(app) as client:
        resp = await client.get("/needs-qap")
    assert resp.status_code == 200


async def test_require_role_denies_insufficient_role(
    session: AsyncSession,
) -> None:
    """An operator is rejected with 403 from a cultivator-gated route."""
    await _seed_user_with_roles(session, "acl-op", [RoleName.operator])
    app = _build_app("acl-op", session)
    async with await _client(app) as client:
        resp = await client.get("/needs-cultivator")
    assert resp.status_code == 403
    assert "cultivator" in resp.json()["detail"]


async def test_require_role_unmapped_user_denied_above_operator(
    session: AsyncSession,
) -> None:
    """An unmapped identity (no roles) cannot reach a qap route."""
    app = _build_app("acl-ghost", session)
    async with await _client(app) as client:
        resp = await client.get("/needs-qap")
    assert resp.status_code == 403
