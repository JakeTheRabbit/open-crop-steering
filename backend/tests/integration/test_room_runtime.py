"""Integration tests for :mod:`app.core.room_runtime`.

Exercises the ``room_runtime`` helpers against real Postgres: lazy
get-or-create, cycle-day derivation from ``cycle_start_date``, and the
state / tick bookkeeping writers.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.core.room_runtime import (
    MIN_CYCLE_DAY,
    cycle_day_for,
    get_or_create,
    mark_ticked,
    update_state,
)
from app.core.state_machine import RoomState
from app.models.room_runtime import DEFAULT_ROLLOUT_STAGE, RoomRuntime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


class TestGetOrCreate:
    async def test_creates_row_on_first_access(self, session: AsyncSession) -> None:
        runtime = await get_or_create(session, "room-a")
        assert runtime.room_id == "room-a"
        assert runtime.rollout_stage == DEFAULT_ROLLOUT_STAGE
        assert runtime.current_state == RoomState.healthy.value
        assert runtime.muted is False
        assert runtime.paused is False
        assert runtime.cycle_start_date is None

    async def test_returns_existing_row(self, session: AsyncSession) -> None:
        first = await get_or_create(session, "room-b")
        first.muted = True
        await session.flush()

        second = await get_or_create(session, "room-b")
        assert second.muted is True
        # Only one row exists.
        rows = (
            await session.execute(
                select(RoomRuntime).where(RoomRuntime.room_id == "room-b")
            )
        ).scalars().all()
        assert len(rows) == 1

    async def test_default_stage_is_report_only(
        self, session: AsyncSession
    ) -> None:
        runtime = await get_or_create(session, "room-c")
        assert runtime.rollout_stage == "report_only"


class TestCycleDayFor:
    def test_day_one_on_cycle_start_date(self) -> None:
        runtime = RoomRuntime(
            room_id="r", cycle_start_date=dt.date(2026, 5, 1)
        )
        assert cycle_day_for(runtime, today=dt.date(2026, 5, 1)) == 1

    def test_counts_elapsed_days(self) -> None:
        runtime = RoomRuntime(
            room_id="r", cycle_start_date=dt.date(2026, 5, 1)
        )
        # 2026-05-15 is 14 days after the start -> cycle day 15.
        assert cycle_day_for(runtime, today=dt.date(2026, 5, 15)) == 15

    def test_no_cycle_start_returns_min_day(self) -> None:
        runtime = RoomRuntime(room_id="r", cycle_start_date=None)
        assert cycle_day_for(runtime) == MIN_CYCLE_DAY

    def test_clamped_at_least_one_for_future_start(self) -> None:
        runtime = RoomRuntime(
            room_id="r", cycle_start_date=dt.date(2026, 6, 1)
        )
        # today before start -> negative elapsed, clamped to 1.
        assert cycle_day_for(runtime, today=dt.date(2026, 5, 1)) == 1

    def test_does_not_clamp_past_end(self) -> None:
        # Cycle day is reported truthfully past recipe length; overflow
        # handling is the executor's concern, not this helper's.
        runtime = RoomRuntime(
            room_id="r", cycle_start_date=dt.date(2026, 1, 1)
        )
        assert cycle_day_for(runtime, today=dt.date(2026, 5, 1)) == 121


class TestUpdateStateAndTick:
    async def test_update_state_persists(self, session: AsyncSession) -> None:
        runtime = await get_or_create(session, "room-d")
        await update_state(session, runtime, RoomState.impaired)

        session.expire_all()
        reloaded = await session.get(RoomRuntime, "room-d")
        assert reloaded is not None
        assert reloaded.current_state == RoomState.impaired.value

    async def test_mark_ticked_stamps_timestamp(
        self, session: AsyncSession
    ) -> None:
        runtime = await get_or_create(session, "room-e")
        assert runtime.last_tick_at is None

        at = dt.datetime(2026, 5, 16, 12, 0, tzinfo=dt.UTC)
        await mark_ticked(session, runtime, at=at)

        session.expire_all()
        reloaded = await session.get(RoomRuntime, "room-e")
        assert reloaded is not None
        assert reloaded.last_tick_at == at

    async def test_mark_ticked_defaults_to_now(
        self, session: AsyncSession
    ) -> None:
        runtime = await get_or_create(session, "room-f")
        before = dt.datetime.now(dt.UTC)
        await mark_ticked(session, runtime)
        assert runtime.last_tick_at is not None
        assert runtime.last_tick_at >= before
