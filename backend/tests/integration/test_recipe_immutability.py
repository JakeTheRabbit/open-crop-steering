"""Verify recipe_revision + recipe_revision_param immutability triggers.

Inserts use direct FK assignment rather than relationship-collection
append + lazy-load read-back, because async SQLAlchemy lazy loads
require a greenlet context the test body doesn't have. When a test
needs to read the params back, it does an explicit ``selectinload``.
"""

from __future__ import annotations

import pytest
from app.models.recipe_revision import (
    RecipeRevision,
    RecipeRevisionParam,
    RecipeStatus,
)
from app.models.user import User
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, InternalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

pytestmark = pytest.mark.integration


async def _seed_user(session: AsyncSession, uid: str) -> None:
    session.add(User(id=uid, display_name=f"User {uid}"))
    await session.flush()


async def _approved_revision(
    session: AsyncSession, room_id: str, uid: str
) -> RecipeRevision:
    """Create a draft revision with one param, then flip it to approved.

    Returns the revision with ``params`` eagerly loaded.
    """
    await _seed_user(session, uid)

    rev = RecipeRevision(
        room_id=room_id,
        version=1,
        name=f"Test recipe for {room_id}",
        cycle_day_count=84,
        status=RecipeStatus.draft,
        created_by=uid,
    )
    session.add(rev)
    await session.flush()

    param = RecipeRevisionParam(
        recipe_revision_id=rev.id,
        room_id=room_id,
        day_index=1,
        param_name="temp_day",
        value=24.0,
        tolerance=0.5,
        unit="C",
    )
    session.add(param)
    await session.flush()

    rev.status = RecipeStatus.approved
    rev.approved_by = uid
    await session.flush()

    # Re-fetch so the test can read .params without tripping lazy loader.
    result = await session.execute(
        select(RecipeRevision)
        .options(selectinload(RecipeRevision.params))
        .where(RecipeRevision.id == rev.id)
    )
    return result.scalar_one()


async def test_can_create_and_approve_revision(session: AsyncSession) -> None:
    rev = await _approved_revision(session, "f-create", "user-create")
    assert rev.status == RecipeStatus.approved
    assert len(rev.params) == 1
    assert rev.params[0].param_name == "temp_day"


async def test_approved_revision_cycle_day_count_immutable(
    session: AsyncSession,
) -> None:
    rev = await _approved_revision(session, "f-cdc", "user-cdc")
    rev.cycle_day_count = 99
    with pytest.raises((InternalError, DBAPIError)) as exc_info:
        await session.flush()
    assert "approved" in str(exc_info.value).lower()


async def test_approved_revision_can_supersede(session: AsyncSession) -> None:
    rev = await _approved_revision(session, "f-sup", "user-sup")
    rev.status = RecipeStatus.superseded
    await session.flush()
    assert rev.status == RecipeStatus.superseded


async def test_approved_revision_cannot_revert_to_draft(
    session: AsyncSession,
) -> None:
    rev = await _approved_revision(session, "f-revert", "user-revert")
    rev.status = RecipeStatus.draft
    with pytest.raises((InternalError, DBAPIError)):
        await session.flush()


async def test_approved_revision_param_update_blocked(
    session: AsyncSession,
) -> None:
    rev = await _approved_revision(session, "f-pup", "user-pup")
    pid = rev.params[0].id
    with pytest.raises((InternalError, DBAPIError)) as exc_info:
        await session.execute(
            text("UPDATE recipe_revision_param SET value = 99.9 WHERE id = :i"),
            {"i": pid},
        )
        await session.flush()
    assert "approved" in str(exc_info.value).lower()


async def test_approved_revision_param_delete_blocked(
    session: AsyncSession,
) -> None:
    rev = await _approved_revision(session, "f-pdel", "user-pdel")
    pid = rev.params[0].id
    with pytest.raises((InternalError, DBAPIError)):
        await session.execute(
            text("DELETE FROM recipe_revision_param WHERE id = :i"),
            {"i": pid},
        )
        await session.flush()


async def test_draft_revision_param_can_be_updated(
    session: AsyncSession,
) -> None:
    """While a revision is draft, params remain mutable."""
    await _seed_user(session, "user-draft-edit")
    rev = RecipeRevision(
        room_id="f-draft",
        version=1,
        name="Draft",
        status=RecipeStatus.draft,
        created_by="user-draft-edit",
    )
    session.add(rev)
    await session.flush()

    param = RecipeRevisionParam(
        recipe_revision_id=rev.id,
        room_id="f-draft",
        day_index=1,
        param_name="rh_day",
        value=60.0,
        unit="%",
    )
    session.add(param)
    await session.flush()

    # Mutating while draft must succeed.
    await session.execute(
        text("UPDATE recipe_revision_param SET value = 65.0 WHERE id = :i"),
        {"i": param.id},
    )
    await session.flush()

    result = await session.execute(
        text("SELECT value FROM recipe_revision_param WHERE id = :i"),
        {"i": param.id},
    )
    assert result.scalar_one() == 65.0
