"""Role-based access control.

Four roles, strictly nested (plan v3 #7, #13, locked decision #6):

* ``operator``   — read-only + acknowledge alerts.
* ``cultivator`` — operator + create recipe revisions + SFW (supervised)
  approval.
* ``qap``        — cultivator + advance rollout + acknowledge formal
  deviations + export the audit log.
* ``admin``      — qap + manage users / role assignments + the HA entity
  map + guardrail bounds.

Roles are *nested*: a higher role implicitly satisfies every capability
of the roles below it, so authorization is a single "is the user's
highest role >= the required role" comparison. A user with no row in
``user_roles`` defaults to ``operator`` (read-only) — see plan locked
decision #7.

This module is transport-agnostic at its core (:func:`get_user_roles`,
:func:`max_role`, :func:`has_min_role`) and also exposes
:func:`require_role`, a FastAPI dependency factory used to guard routes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import structlog
from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Identity, current_identity
from app.db import get_session
from app.models.user import RoleName, UserRole

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

log = structlog.get_logger(__name__)


# Strict role ordering, lowest privilege first. The index of a role in
# this tuple is its privilege rank; a higher rank satisfies every lower
# requirement. Keep this the single source of truth for the hierarchy.
ROLE_ORDER: tuple[RoleName, ...] = (
    RoleName.operator,
    RoleName.cultivator,
    RoleName.qap,
    RoleName.admin,
)

#: Privilege rank per role (``operator`` == 0, ``admin`` == 3).
ROLE_RANK: dict[RoleName, int] = {role: i for i, role in enumerate(ROLE_ORDER)}

#: Role assigned to any identity with no explicit ``user_roles`` rows.
DEFAULT_ROLE: RoleName = RoleName.operator


def role_rank(role: RoleName) -> int:
    """Return the privilege rank of *role* (``operator`` is lowest)."""
    return ROLE_RANK[role]


def max_role(roles: set[RoleName]) -> RoleName:
    """Return the highest-privilege role in *roles*.

    An empty set resolves to :data:`DEFAULT_ROLE` (``operator``) so that
    unmapped users still have a well-defined, read-only role.
    """
    if not roles:
        return DEFAULT_ROLE
    return max(roles, key=role_rank)


def has_min_role(roles: set[RoleName], minimum: RoleName) -> bool:
    """Return ``True`` if *roles* satisfies *minimum* (or higher)."""
    return role_rank(max_role(roles)) >= role_rank(minimum)


async def get_user_roles(session: AsyncSession, user_id: str) -> set[RoleName]:
    """Return the set of roles granted to *user_id*.

    Args:
        session: Active async session.
        user_id: HA user id (``users.id``).

    Returns:
        Every :class:`RoleName` the user holds. Empty if the user has no
        ``user_roles`` rows — callers should treat empty as
        :data:`DEFAULT_ROLE` via :func:`max_role` / :func:`has_min_role`.
    """
    result = await session.execute(
        select(UserRole.role_name).where(UserRole.user_id == user_id)
    )
    return set(result.scalars())


async def effective_role(session: AsyncSession, user_id: str) -> RoleName:
    """Return the single highest role for *user_id* (default ``operator``)."""
    return max_role(await get_user_roles(session, user_id))


def require_role(
    minimum: RoleName,
) -> Callable[..., Coroutine[Any, Any, Identity]]:
    """Build a FastAPI dependency enforcing a minimum role.

    The returned dependency resolves the caller's :class:`Identity`
    (HA Ingress headers in add-on mode, JWT in standalone mode — see
    :mod:`app.core.auth`), looks up their roles, and raises HTTP 403 if
    they lack *minimum* or higher. Unmapped users are treated as
    ``operator`` (plan locked decision #7), so an ``operator``-gated
    route is open to every authenticated identity.

    Args:
        minimum: Lowest role permitted to call the route.

    Returns:
        An ``async`` dependency that returns the authorized
        :class:`Identity` (so handlers can record the actor) or raises
        :class:`fastapi.HTTPException` 403.

    Example:
        >>> @router.get("/secret")
        ... async def secret(
        ...     identity: Annotated[Identity, Depends(require_role(RoleName.qap))],
        ... ) -> dict[str, str]:
        ...     return {"actor": identity.user_id}
    """
    async def _dependency(
        identity: Annotated[Identity, Depends(current_identity)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> Identity:
        roles = await get_user_roles(session, identity.user_id)
        if not has_min_role(roles, minimum):
            log.warning(
                "rbac_denied",
                user_id=identity.user_id,
                required=minimum.value,
                effective=max_role(roles).value,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"role '{minimum.value}' or higher required; "
                    f"you have '{max_role(roles).value}'"
                ),
            )
        # Stamp the resolved roles onto the identity for downstream use.
        identity.roles = roles
        return identity

    return _dependency
