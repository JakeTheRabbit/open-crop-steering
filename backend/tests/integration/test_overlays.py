"""Integration tests for :mod:`app.core.overlays`.

Verifies that adding an overlay shifts the materialized
``effective_target``, that reverting removes it, and that the
expiry sweep deactivates past-due overlays. Runs against real Postgres.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.core.overlays import (
    add_adjustment,
    expire_due_adjustments,
    revert_adjustment,
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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _seed_recipe(
    session: AsyncSession,
    *,
    uid: str,
    room_id: str,
    day_index: int,
    param_name: str,
    value: float,
) -> None:
    """Insert one approved recipe with a single param + commit."""
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

    session.add(
        RecipeRevisionParam(
            recipe_revision_id=rev.id,
            room_id=room_id,
            day_index=day_index,
            param_name=param_name,
            value=value,
            unit="C",
        )
    )
    await session.commit()


async def _effective(
    session: AsyncSession, room_id: str, day_index: int, param_name: str
) -> float | None:
    """Read one value straight from the effective_target matview."""
    result = await session.execute(
        text(
            "SELECT value FROM effective_target "
            "WHERE room_id = :r AND day_index = :d AND param_name = :p"
        ),
        {"r": room_id, "d": day_index, "p": param_name},
    )
    return result.scalar_one_or_none()


async def test_add_adjustment_shifts_effective_target(
    session: AsyncSession,
) -> None:
    """An overlay's delta is reflected in the materialized effective value."""
    await _seed_recipe(
        session, uid="ov-add", room_id="ov-room-1", day_index=5,
        param_name="temp_day", value=26.0,
    )

    await add_adjustment(
        session,
        room_id="ov-room-1",
        day_index=5,
        param_name="temp_day",
        delta=-0.5,
        source=AdjustmentSource.ai_auto,
        mode=AdjustmentMode.bounded_auto_adjust,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=4),
        created_by="ov-add",
    )
    await session.commit()

    assert await _effective(session, "ov-room-1", 5, "temp_day") == pytest.approx(
        25.5
    )


async def test_add_adjustment_writes_audit_event(
    session: AsyncSession,
) -> None:
    """add_adjustment emits a runtime_adjustment_added audit row."""
    await _seed_recipe(
        session, uid="ov-audit", room_id="ov-room-a", day_index=2,
        param_name="rh_day", value=60.0,
    )
    adj = await add_adjustment(
        session,
        room_id="ov-room-a",
        day_index=2,
        param_name="rh_day",
        delta=2.0,
        source=AdjustmentSource.cultivator,
        mode=AdjustmentMode.supervised_approval,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=2),
        created_by="ov-audit",
    )
    await session.commit()

    row = await session.execute(
        text(
            "SELECT event_type FROM audit_event "
            "WHERE runtime_adjustment_id = :aid "
            "AND event_type = 'runtime_adjustment_added'"
        ),
        {"aid": adj.id},
    )
    assert row.scalar_one() == "runtime_adjustment_added"


async def test_multiple_overlays_sum(session: AsyncSession) -> None:
    """Stacked active overlays sum into the effective target."""
    await _seed_recipe(
        session, uid="ov-sum", room_id="ov-room-2", day_index=8,
        param_name="temp_day", value=24.0,
    )
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(hours=4)
    for delta in (0.3, 0.4):
        await add_adjustment(
            session,
            room_id="ov-room-2",
            day_index=8,
            param_name="temp_day",
            delta=delta,
            source=AdjustmentSource.ai_auto,
            mode=AdjustmentMode.bounded_auto_adjust,
            expires_at=expires,
            created_by="ov-sum",
        )
    await session.commit()

    assert await _effective(session, "ov-room-2", 8, "temp_day") == pytest.approx(
        24.7
    )


async def test_revert_adjustment_removes_it(session: AsyncSession) -> None:
    """Reverting an overlay drops it from the effective target."""
    await _seed_recipe(
        session, uid="ov-revert", room_id="ov-room-3", day_index=12,
        param_name="vwc_target", value=55.0,
    )
    adj = await add_adjustment(
        session,
        room_id="ov-room-3",
        day_index=12,
        param_name="vwc_target",
        delta=5.0,
        source=AdjustmentSource.ai_auto,
        mode=AdjustmentMode.bounded_auto_adjust,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=4),
        created_by="ov-revert",
    )
    await session.commit()
    assert await _effective(session, "ov-room-3", 12, "vwc_target") == pytest.approx(
        60.0
    )

    reverted = await revert_adjustment(session, adj.id, "ov-revert")
    await session.commit()
    assert reverted.active is False
    assert reverted.reverted_at is not None
    assert reverted.reverted_by == "ov-revert"
    # Effective target falls back to the pure recipe value.
    assert await _effective(session, "ov-room-3", 12, "vwc_target") == 55.0


async def test_revert_adjustment_writes_audit_event(
    session: AsyncSession,
) -> None:
    """revert_adjustment emits a runtime_adjustment_reverted audit row."""
    await _seed_recipe(
        session, uid="ov-rev-audit", room_id="ov-room-b", day_index=3,
        param_name="temp_day", value=25.0,
    )
    adj = await add_adjustment(
        session,
        room_id="ov-room-b",
        day_index=3,
        param_name="temp_day",
        delta=0.5,
        source=AdjustmentSource.ai_auto,
        mode=AdjustmentMode.bounded_auto_adjust,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=4),
        created_by="ov-rev-audit",
    )
    await session.commit()
    await revert_adjustment(session, adj.id, "ov-rev-audit")
    await session.commit()

    row = await session.execute(
        text(
            "SELECT event_type FROM audit_event "
            "WHERE runtime_adjustment_id = :aid "
            "AND event_type = 'runtime_adjustment_reverted'"
        ),
        {"aid": adj.id},
    )
    assert row.scalar_one() == "runtime_adjustment_reverted"


async def test_revert_adjustment_unknown_raises(
    session: AsyncSession,
) -> None:
    """Reverting a non-existent overlay raises ValueError."""
    with pytest.raises(ValueError, match="not found"):
        await revert_adjustment(session, 999_999, "nobody")


async def test_expire_due_adjustments_expires_past_due(
    session: AsyncSession,
) -> None:
    """expire_due_adjustments deactivates overlays past their expiry."""
    await _seed_recipe(
        session, uid="ov-expire", room_id="ov-room-4", day_index=20,
        param_name="temp_day", value=26.0,
    )

    # One already-expired overlay, inserted directly so its expiry is past.
    expired = RuntimeAdjustment(
        room_id="ov-room-4",
        day_index=20,
        param_name="temp_day",
        delta=1.0,
        source=AdjustmentSource.ai_auto,
        mode=AdjustmentMode.bounded_auto_adjust,
        created_by="ov-expire",
        expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10),
        active=True,
    )
    session.add(expired)
    await session.commit()

    count = await expire_due_adjustments(session)
    await session.commit()

    assert count >= 1
    refreshed = await session.get(RuntimeAdjustment, expired.id)
    assert refreshed is not None
    assert refreshed.active is False
    # A natural expiry is not a deliberate revert.
    assert refreshed.reverted_at is None
    assert refreshed.reverted_by is None


async def test_expire_due_adjustments_keeps_active(
    session: AsyncSession,
) -> None:
    """In-window overlays survive the expiry sweep."""
    await _seed_recipe(
        session, uid="ov-keep", room_id="ov-room-5", day_index=25,
        param_name="rh_day", value=55.0,
    )
    live = await add_adjustment(
        session,
        room_id="ov-room-5",
        day_index=25,
        param_name="rh_day",
        delta=-2.0,
        source=AdjustmentSource.ai_auto,
        mode=AdjustmentMode.bounded_auto_adjust,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=6),
        created_by="ov-keep",
    )
    await session.commit()

    await expire_due_adjustments(session)
    await session.commit()

    refreshed = await session.get(RuntimeAdjustment, live.id)
    assert refreshed is not None
    assert refreshed.active is True


async def test_expire_due_adjustments_returns_zero_when_none(
    session: AsyncSession,
) -> None:
    """The sweep returns 0 when nothing is due."""
    assert await expire_due_adjustments(session) == 0
