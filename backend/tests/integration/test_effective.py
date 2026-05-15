"""Integration tests for :mod:`app.core.effective`.

Verifies the read helpers (:func:`effective_target`,
:func:`effective_targets_for_day`) return the materialized values, and
that :func:`refresh_effective_targets` repopulates the view after
overlays change. Runs against real Postgres.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.core.effective import (
    effective_target,
    effective_targets_for_day,
    refresh_effective_targets,
)
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
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _seed_recipe_day(
    session: AsyncSession,
    *,
    uid: str,
    room_id: str,
    day_index: int,
    params: dict[str, float],
) -> int:
    """Insert one approved recipe carrying several params for one day.

    Returns the recipe revision id.
    """
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

    for param_name, value in params.items():
        session.add(
            RecipeRevisionParam(
                recipe_revision_id=rev.id,
                room_id=room_id,
                day_index=day_index,
                param_name=param_name,
                value=value,
            )
        )
    await session.commit()
    return rev.id


async def test_effective_target_reads_recipe_value(
    session: AsyncSession,
) -> None:
    """With no overlay, effective_target equals the recipe value."""
    await _seed_recipe_day(
        session, uid="ef-1", room_id="ef-room-1", day_index=5,
        params={"temp_day": 26.5},
    )
    await refresh_effective_targets(session)
    await session.commit()

    value = await effective_target(session, "ef-room-1", 5, "temp_day")
    assert value == pytest.approx(26.5)


async def test_effective_target_reflects_overlay_after_refresh(
    session: AsyncSession,
) -> None:
    """Adding an overlay then refreshing shifts the read value."""
    await _seed_recipe_day(
        session, uid="ef-2", room_id="ef-room-2", day_index=10,
        params={"rh_day": 60.0},
    )
    await refresh_effective_targets(session)
    await session.commit()
    assert await effective_target(session, "ef-room-2", 10, "rh_day") == pytest.approx(
        60.0
    )

    session.add(
        RuntimeAdjustment(
            room_id="ef-room-2",
            day_index=10,
            param_name="rh_day",
            delta=-4.0,
            source=AdjustmentSource.ai_auto,
            mode=AdjustmentMode.bounded_auto_adjust,
            created_by="ef-2",
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=4),
            active=True,
        )
    )
    await session.commit()

    # Stale until refreshed.
    await refresh_effective_targets(session)
    await session.commit()
    assert await effective_target(session, "ef-room-2", 10, "rh_day") == pytest.approx(
        56.0
    )


async def test_effective_target_none_for_missing(
    session: AsyncSession,
) -> None:
    """A (room, day, param) with no recipe row reads as None."""
    await refresh_effective_targets(session)
    await session.commit()
    assert await effective_target(session, "ef-nope", 1, "temp_day") is None


async def test_effective_targets_for_day_returns_all_params(
    session: AsyncSession,
) -> None:
    """effective_targets_for_day returns every param for a room + day."""
    await _seed_recipe_day(
        session,
        uid="ef-3",
        room_id="ef-room-3",
        day_index=15,
        params={"temp_day": 27.0, "rh_day": 55.0, "vwc_target": 50.0},
    )
    await refresh_effective_targets(session)
    await session.commit()

    rows = await effective_targets_for_day(session, "ef-room-3", 15)
    assert set(rows) == {"temp_day", "rh_day", "vwc_target"}
    assert rows["temp_day"].value == pytest.approx(27.0)
    assert rows["rh_day"].value == pytest.approx(55.0)
    assert rows["vwc_target"].value == pytest.approx(50.0)
    assert rows["temp_day"].day_index == 15
    assert rows["temp_day"].room_id == "ef-room-3"


async def test_effective_targets_for_day_empty_for_unknown_room(
    session: AsyncSession,
) -> None:
    """A room with no recipe yields an empty mapping."""
    await refresh_effective_targets(session)
    await session.commit()
    assert await effective_targets_for_day(session, "ef-unknown", 1) == {}


async def test_effective_targets_for_day_isolates_day(
    session: AsyncSession,
) -> None:
    """effective_targets_for_day only returns the requested day's rows."""
    uid = "ef-4"
    session.add(User(id=uid, display_name="User ef-4"))
    await session.flush()
    rev = RecipeRevision(
        room_id="ef-room-4",
        version=1,
        name="multi-day",
        status=RecipeStatus.approved,
        created_by=uid,
        approved_by=uid,
    )
    session.add(rev)
    await session.flush()
    for day, value in ((1, 24.0), (2, 25.0)):
        session.add(
            RecipeRevisionParam(
                recipe_revision_id=rev.id,
                room_id="ef-room-4",
                day_index=day,
                param_name="temp_day",
                value=value,
            )
        )
    await session.commit()
    await refresh_effective_targets(session)
    await session.commit()

    day1 = await effective_targets_for_day(session, "ef-room-4", 1)
    day2 = await effective_targets_for_day(session, "ef-room-4", 2)
    assert day1["temp_day"].value == pytest.approx(24.0)
    assert day2["temp_day"].value == pytest.approx(25.0)
