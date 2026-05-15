"""Admin API — user / role management and the Telegram chat map.

Every route requires the ``admin`` role (plan v3 #7: only admins touch
users, role assignments, the HA entity map, and guardrail bounds —
Class E surfaces the AI never writes).

Role grants and revocations write ``user_role_changed`` audit events so
access-control changes are themselves part of the tamper-evident chain
(plan locked decision #6; an unaudited RBAC change is a formal
deviation per the plan's deviation criteria).
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.acl import get_user_roles, require_role
from app.core.audit import log_audit
from app.core.auth import Identity
from app.db import get_session
from app.models.audit_event import AuditEventType
from app.models.telegram_user_map import TelegramUserMap
from app.models.user import RoleName, User, UserRole

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# --- request bodies ----------------------------------------------------


class UserUpsert(BaseModel):
    """Create-or-update payload for a user."""

    id: str = Field(min_length=1, max_length=64, description="HA user id")
    display_name: str = Field(min_length=1, max_length=128)
    email: str | None = Field(default=None, max_length=256)
    active: bool = True
    roles: list[RoleName] = Field(
        default_factory=list, description="Full set of roles to assign"
    )


class TelegramMapUpsert(BaseModel):
    """Create-or-update payload for a Telegram chat-id mapping."""

    chat_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    label: str | None = Field(default=None, max_length=128)


# --- serializers -------------------------------------------------------


def _user_dict(user: User, roles: set[RoleName]) -> dict[str, Any]:
    """Serialize a :class:`User` plus its resolved role set."""
    return {
        "id": user.id,
        "display_name": user.display_name,
        "email": user.email,
        "active": user.active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "roles": sorted(r.value for r in roles),
    }


def _telegram_dict(mapping: TelegramUserMap) -> dict[str, Any]:
    """Serialize a :class:`TelegramUserMap` row."""
    return {
        "chat_id": mapping.chat_id,
        "user_id": mapping.user_id,
        "label": mapping.label,
        "created_at": mapping.created_at.isoformat() if mapping.created_at else None,
        "created_by": mapping.created_by,
        "last_verified_at": (
            mapping.last_verified_at.isoformat()
            if mapping.last_verified_at
            else None
        ),
    }


# --- users -------------------------------------------------------------


@router.get("/users")
async def list_users(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """List every user with their assigned roles.

    Args:
        identity: Authorized caller (``admin``).
        session: Active async session.

    Returns:
        ``{"users": [...]}``.
    """
    users = (
        await session.execute(select(User).order_by(User.id))
    ).scalars()
    out: list[dict[str, Any]] = []
    for user in users:
        roles = await get_user_roles(session, user.id)
        out.append(_user_dict(user, roles))
    log.info("admin_users_listed", actor=identity.user_id, count=len(out))
    return {"users": out}


@router.post("/users", status_code=status.HTTP_200_OK)
async def upsert_user(
    body: UserUpsert,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Create or update a user and reconcile their role set.

    The supplied ``roles`` list fully replaces the user's current
    roles. Each added or removed role writes its own
    ``user_role_changed`` audit event.

    Args:
        body: User upsert payload.
        identity: Authorized caller (``admin``).
        session: Active async session.

    Returns:
        The resulting user dict.
    """
    user = await session.get(User, body.id)
    if user is None:
        user = User(
            id=body.id,
            display_name=body.display_name,
            email=body.email,
            active=body.active,
        )
        session.add(user)
    else:
        user.display_name = body.display_name
        user.email = body.email
        user.active = body.active
    await session.flush()

    current = await get_user_roles(session, body.id)
    desired = set(body.roles)
    added = desired - current
    removed = current - desired

    for role in removed:
        await session.execute(
            delete(UserRole).where(
                UserRole.user_id == body.id, UserRole.role_name == role
            )
        )
    for role in added:
        session.add(
            UserRole(
                user_id=body.id,
                role_name=role,
                granted_by=identity.user_id,
            )
        )
    await session.flush()

    for role in sorted(added, key=lambda r: r.value):
        await log_audit(
            session,
            event_type=AuditEventType.user_role_changed,
            actor_id=identity.user_id,
            summary=f"Granted role '{role.value}' to {body.id}",
            params={"target_user": body.id, "role": role.value, "action": "grant"},
        )
    for role in sorted(removed, key=lambda r: r.value):
        await log_audit(
            session,
            event_type=AuditEventType.user_role_changed,
            actor_id=identity.user_id,
            summary=f"Revoked role '{role.value}' from {body.id}",
            params={"target_user": body.id, "role": role.value, "action": "revoke"},
        )
    await session.commit()

    log.info(
        "admin_user_upserted",
        actor=identity.user_id,
        target=body.id,
        roles_added=sorted(r.value for r in added),
        roles_removed=sorted(r.value for r in removed),
    )
    return _user_dict(user, desired)


# --- telegram map ------------------------------------------------------


@router.get("/telegram-map")
async def list_telegram_map(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """List every Telegram chat-id to HA-user mapping.

    Args:
        identity: Authorized caller (``admin``).
        session: Active async session.

    Returns:
        ``{"mappings": [...]}``.
    """
    rows = (
        await session.execute(
            select(TelegramUserMap).order_by(TelegramUserMap.chat_id)
        )
    ).scalars()
    mappings = [_telegram_dict(m) for m in rows]
    log.info(
        "admin_telegram_map_listed",
        actor=identity.user_id,
        count=len(mappings),
    )
    return {"mappings": mappings}


@router.post("/telegram-map", status_code=status.HTTP_200_OK)
async def upsert_telegram_map(
    body: TelegramMapUpsert,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Create or update a Telegram chat-id mapping.

    The mapped user must already exist — a mapping to an unknown user
    would silently grant nobody's approval authority.

    Args:
        body: Telegram-map upsert payload.
        identity: Authorized caller (``admin``).
        session: Active async session.

    Returns:
        The resulting mapping dict.

    Raises:
        fastapi.HTTPException: 404 if ``user_id`` is not a known user.
    """
    if await session.get(User, body.user_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"user '{body.user_id}' does not exist",
        )

    mapping = await session.get(TelegramUserMap, body.chat_id)
    if mapping is None:
        mapping = TelegramUserMap(
            chat_id=body.chat_id,
            user_id=body.user_id,
            label=body.label,
            created_by=identity.user_id,
            last_verified_at=dt.datetime.now(dt.UTC),
        )
        session.add(mapping)
    else:
        mapping.user_id = body.user_id
        mapping.label = body.label
        mapping.last_verified_at = dt.datetime.now(dt.UTC)
    await session.flush()

    # The Telegram map gates approval authority; a change to it is a
    # role-relevant access-control change and is audited as such.
    await log_audit(
        session,
        event_type=AuditEventType.user_role_changed,
        actor_id=identity.user_id,
        summary=(
            f"Telegram chat {body.chat_id} mapped to user {body.user_id}"
        ),
        params={
            "telegram_chat_id": body.chat_id,
            "target_user": body.user_id,
            "action": "telegram_map_upsert",
        },
    )
    await session.commit()

    log.info(
        "admin_telegram_map_upserted",
        actor=identity.user_id,
        chat_id=body.chat_id,
        target=body.user_id,
    )
    return _telegram_dict(mapping)
