"""Integration tests for :mod:`app.core.recipe_store`.

Covers create -> version increment, approve -> status flip + supersede,
active-revision lookup, the no-mutation guarantee, and self-heal
fallback. Runs against a real Postgres so the immutability triggers are
in force.
"""

from __future__ import annotations

import pytest
from app.core.recipe_store import (
    approve_revision,
    create_revision,
    get_active_revision,
    self_heal,
)
from app.models.recipe_revision import RecipeRevision, RecipeStatus
from app.models.user import User
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

pytestmark = pytest.mark.integration


async def _seed_user(session: AsyncSession, uid: str) -> str:
    """Insert a user and return its id."""
    session.add(User(id=uid, display_name=f"User {uid}"))
    await session.flush()
    return uid


def _params(day: int = 1) -> list[dict[str, object]]:
    """A minimal one-param recipe spec for a given day."""
    return [
        {
            "day_index": day,
            "param_name": "temp_day",
            "value": 25.0,
            "tolerance": 0.5,
            "unit": "C",
        }
    ]


async def test_create_revision_produces_draft(session: AsyncSession) -> None:
    """A new revision lands in draft status with its params attached."""
    uid = await _seed_user(session, "rs-create")
    rev = await create_revision(
        session,
        room_id="rs-room-1",
        name="First recipe",
        params=_params(),
        created_by=uid,
    )
    assert rev.status == RecipeStatus.draft
    assert rev.version == 1
    assert len(rev.params) == 1
    assert rev.params[0].param_name == "temp_day"


async def test_create_revision_increments_version(
    session: AsyncSession,
) -> None:
    """Successive creates for one room get monotonically rising versions."""
    uid = await _seed_user(session, "rs-version")
    room = "rs-room-2"
    v1 = await create_revision(
        session, room_id=room, name="v1", params=_params(), created_by=uid
    )
    v2 = await create_revision(
        session, room_id=room, name="v2", params=_params(), created_by=uid
    )
    v3 = await create_revision(
        session, room_id=room, name="v3", params=_params(), created_by=uid
    )
    assert [v1.version, v2.version, v3.version] == [1, 2, 3]


async def test_create_revision_writes_audit_event(
    session: AsyncSession,
) -> None:
    """create_revision emits a recipe_revision_created audit row."""
    uid = await _seed_user(session, "rs-audit")
    rev = await create_revision(
        session, room_id="rs-room-a", name="audited", params=_params(),
        created_by=uid,
    )
    row = await session.execute(
        text(
            "SELECT event_type FROM audit_event "
            "WHERE recipe_revision_id = :rid "
            "AND event_type = 'recipe_revision_created'"
        ),
        {"rid": rev.id},
    )
    assert row.scalar_one() == "recipe_revision_created"


async def test_approve_revision_flips_status(session: AsyncSession) -> None:
    """approve_revision flips draft -> approved and stamps approver."""
    uid = await _seed_user(session, "rs-approve")
    rev = await create_revision(
        session, room_id="rs-room-3", name="to approve", params=_params(),
        created_by=uid,
    )
    approved = await approve_revision(session, rev.id, uid)
    assert approved.status == RecipeStatus.approved
    assert approved.approved_by == uid
    assert approved.approved_at is not None


async def test_approve_revision_supersedes_prior(
    session: AsyncSession,
) -> None:
    """Approving a new revision supersedes the room's prior approved one."""
    uid = await _seed_user(session, "rs-supersede")
    room = "rs-room-4"
    v1 = await create_revision(
        session, room_id=room, name="v1", params=_params(), created_by=uid
    )
    await approve_revision(session, v1.id, uid)

    v2 = await create_revision(
        session, room_id=room, name="v2", params=_params(), created_by=uid
    )
    await approve_revision(session, v2.id, uid)

    refreshed_v1 = await session.get(RecipeRevision, v1.id)
    refreshed_v2 = await session.get(RecipeRevision, v2.id)
    assert refreshed_v1 is not None
    assert refreshed_v2 is not None
    assert refreshed_v1.status == RecipeStatus.superseded
    assert refreshed_v2.status == RecipeStatus.approved


async def test_approve_revision_rejects_non_draft(
    session: AsyncSession,
) -> None:
    """Approving an already-approved revision raises ValueError."""
    uid = await _seed_user(session, "rs-double")
    rev = await create_revision(
        session, room_id="rs-room-5", name="once", params=_params(),
        created_by=uid,
    )
    await approve_revision(session, rev.id, uid)
    with pytest.raises(ValueError, match="only draft"):
        await approve_revision(session, rev.id, uid)


async def test_get_active_revision_returns_highest_approved(
    session: AsyncSession,
) -> None:
    """get_active_revision returns the highest-version approved revision."""
    uid = await _seed_user(session, "rs-active")
    room = "rs-room-6"
    v1 = await create_revision(
        session, room_id=room, name="v1", params=_params(), created_by=uid
    )
    await approve_revision(session, v1.id, uid)
    v2 = await create_revision(
        session, room_id=room, name="v2", params=_params(), created_by=uid
    )
    await approve_revision(session, v2.id, uid)
    # A draft v3 must NOT be considered active.
    await create_revision(
        session, room_id=room, name="v3 draft", params=_params(),
        created_by=uid,
    )

    active = await get_active_revision(session, room)
    assert active is not None
    assert active.id == v2.id
    assert active.version == 2
    assert len(active.params) == 1


async def test_get_active_revision_none_when_no_approved(
    session: AsyncSession,
) -> None:
    """A room with only draft revisions has no active revision."""
    uid = await _seed_user(session, "rs-noactive")
    await create_revision(
        session, room_id="rs-room-7", name="draft only", params=_params(),
        created_by=uid,
    )
    assert await get_active_revision(session, "rs-room-7") is None


async def test_create_never_mutates_existing_revision(
    session: AsyncSession,
) -> None:
    """Creating a second revision leaves the first row byte-for-byte intact."""
    uid = await _seed_user(session, "rs-immut")
    room = "rs-room-8"
    v1 = await create_revision(
        session, room_id=room, name="original", params=_params(day=3),
        created_by=uid,
    )
    await approve_revision(session, v1.id, uid)

    # Snapshot v1's identifying fields.
    before = await session.execute(
        select(RecipeRevision)
        .options(selectinload(RecipeRevision.params))
        .where(RecipeRevision.id == v1.id)
    )
    v1_before = before.scalar_one()
    name_before = v1_before.name
    param_value_before = v1_before.params[0].value

    # Creating v2 must not touch v1.
    await create_revision(
        session, room_id=room, name="new revision", params=_params(day=3),
        created_by=uid,
    )

    after = await session.execute(
        select(RecipeRevision)
        .options(selectinload(RecipeRevision.params))
        .where(RecipeRevision.id == v1.id)
    )
    v1_after = after.scalar_one()
    assert v1_after.name == name_before == "original"
    assert v1_after.params[0].value == param_value_before == 25.0
    assert len(v1_after.params) == 1


async def test_self_heal_returns_active_when_healthy(
    session: AsyncSession,
) -> None:
    """self_heal is a no-op (returns the active revision) when it has params."""
    uid = await _seed_user(session, "rs-heal-ok")
    room = "rs-room-9"
    v1 = await create_revision(
        session, room_id=room, name="healthy", params=_params(),
        created_by=uid,
    )
    await approve_revision(session, v1.id, uid)

    healed = await self_heal(session, room)
    assert healed is not None
    assert healed.id == v1.id


async def test_self_heal_falls_back_to_prior_revision(
    session: AsyncSession,
) -> None:
    """A param-less active revision heals back to a prior healthy revision."""
    uid = await _seed_user(session, "rs-heal-fallback")
    room = "rs-room-10"

    # v1: healthy approved revision with a param.
    v1 = await create_revision(
        session, room_id=room, name="good v1", params=_params(),
        created_by=uid,
    )
    await approve_revision(session, v1.id, uid)

    # v2: approved revision, then its params are corrupted away. We
    # delete params while v2 is still a draft (triggers block deletes on
    # approved revisions), then approve it.
    v2 = await create_revision(
        session, room_id=room, name="corrupt v2", params=_params(),
        created_by=uid,
    )
    await session.execute(
        text("DELETE FROM recipe_revision_param WHERE recipe_revision_id = :r"),
        {"r": v2.id},
    )
    await session.flush()
    await approve_revision(session, v2.id, uid)

    healed = await self_heal(session, room)
    assert healed is not None
    assert healed.id == v1.id, "should fall back to the healthy prior revision"
    assert len(healed.params) == 1


async def test_self_heal_none_when_no_revision(
    session: AsyncSession,
) -> None:
    """self_heal on a room with no approved revision returns None."""
    assert await self_heal(session, "rs-room-empty") is None
