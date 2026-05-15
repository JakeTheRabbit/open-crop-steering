"""Sanity checks for the ``effective_target`` materialized view.

Full materialization-on-overlay-change behaviour lives in P3; this just
verifies the view DDL is sane and refreshable.

Inserts use direct FK assignment instead of relationship collections to
keep async SQLAlchemy out of the lazy-load path.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.models.recipe_revision import (
    RecipeRevision,
    RecipeRevisionParam,
    RecipeStatus,
)
from app.models.runtime_adjustment import (
    AdjustmentMode,
    AdjustmentSource,
    RuntimeAdjustment,
)
from app.models.user import User
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _seed_user_and_recipe(
    session: AsyncSession,
    *,
    uid: str,
    room_id: str,
    day_index: int,
    param_name: str,
    value: float,
    unit: str | None = None,
    tolerance: float | None = None,
) -> int:
    """Insert one approved recipe with one param. Returns recipe revision id."""
    session.add(User(id=uid, display_name=f"User {uid}"))
    await session.flush()

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

    param = RecipeRevisionParam(
        recipe_revision_id=rev.id,
        room_id=room_id,
        day_index=day_index,
        param_name=param_name,
        value=value,
        tolerance=tolerance,
        unit=unit,
    )
    session.add(param)
    await session.commit()
    return rev.id


async def test_effective_target_reflects_recipe_value(
    session: AsyncSession,
) -> None:
    """Without overlays, ``effective_target.value`` equals the recipe value."""
    await _seed_user_and_recipe(
        session,
        uid="et-user-1",
        room_id="et-room-1",
        day_index=5,
        param_name="temp_day",
        value=26.5,
        unit="C",
        tolerance=0.5,
    )

    await session.execute(text("REFRESH MATERIALIZED VIEW effective_target"))
    result = await session.execute(
        text(
            "SELECT value FROM effective_target "
            "WHERE room_id = :r AND day_index = :d AND param_name = :p"
        ),
        {"r": "et-room-1", "d": 5, "p": "temp_day"},
    )
    assert result.scalar_one() == 26.5


async def test_effective_target_applies_active_overlay(
    session: AsyncSession,
) -> None:
    """An active in-window overlay shifts the effective value."""
    await _seed_user_and_recipe(
        session,
        uid="et-user-2",
        room_id="et-room-2",
        day_index=10,
        param_name="rh_day",
        value=60.0,
        unit="%",
    )

    overlay = RuntimeAdjustment(
        room_id="et-room-2",
        day_index=10,
        param_name="rh_day",
        delta=-3.0,
        source=AdjustmentSource.ai_auto,
        mode=AdjustmentMode.bounded_auto_adjust,
        created_by="et-user-2",
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=4),
        active=True,
    )
    session.add(overlay)
    await session.commit()

    await session.execute(text("REFRESH MATERIALIZED VIEW effective_target"))
    result = await session.execute(
        text(
            "SELECT value FROM effective_target "
            "WHERE room_id = :r AND day_index = :d AND param_name = :p"
        ),
        {"r": "et-room-2", "d": 10, "p": "rh_day"},
    )
    assert result.scalar_one() == pytest.approx(57.0)


async def test_effective_target_ignores_expired_overlay(
    session: AsyncSession,
) -> None:
    """Overlays past ``expires_at`` are excluded from the materialization."""
    await _seed_user_and_recipe(
        session,
        uid="et-user-3",
        room_id="et-room-3",
        day_index=20,
        param_name="vwc_target",
        value=55.0,
        unit="%",
    )

    expired = RuntimeAdjustment(
        room_id="et-room-3",
        day_index=20,
        param_name="vwc_target",
        delta=10.0,  # would push to 65 if active
        source=AdjustmentSource.ai_auto,
        mode=AdjustmentMode.bounded_auto_adjust,
        created_by="et-user-3",
        expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
        active=True,
    )
    session.add(expired)
    await session.commit()

    await session.execute(text("REFRESH MATERIALIZED VIEW effective_target"))
    result = await session.execute(
        text(
            "SELECT value FROM effective_target "
            "WHERE room_id = :r AND day_index = :d AND param_name = :p"
        ),
        {"r": "et-room-3", "d": 20, "p": "vwc_target"},
    )
    assert result.scalar_one() == 55.0
