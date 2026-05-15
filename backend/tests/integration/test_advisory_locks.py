"""Postgres advisory-lock helper tests."""

from __future__ import annotations

import asyncio
import os

import pytest
from app.core.locks import LockBusyError, advisory_lock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


async def _set_hmac_guc(s: AsyncSession) -> None:
    await s.execute(
        text("SELECT set_config('audit.hmac_key_1', :v, false)"),
        {"v": os.environ["HMAC_KEY_1"]},
    )


async def test_acquire_release_basic(session: AsyncSession) -> None:
    async with advisory_lock(session, "test_lock_basic"):
        pass


async def test_non_blocking_busy_raises(async_engine: AsyncEngine) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as s1, factory() as s2:
        await _set_hmac_guc(s1)
        await _set_hmac_guc(s2)

        async with advisory_lock(s1, "test_lock_contention"):
            with pytest.raises(LockBusyError):
                async with advisory_lock(
                    s2, "test_lock_contention", blocking=False
                ):
                    pass


async def test_blocking_waits_then_acquires(async_engine: AsyncEngine) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    acquired = asyncio.Event()

    async with factory() as s1, factory() as s2:
        await _set_hmac_guc(s1)
        await _set_hmac_guc(s2)

        async def _waiter() -> None:
            async with advisory_lock(s2, "test_lock_blocking", blocking=True):
                acquired.set()

        async with advisory_lock(s1, "test_lock_blocking", blocking=True):
            task = asyncio.create_task(_waiter())
            # Give s2 a moment to be parked on the lock
            await asyncio.sleep(0.2)
            assert not acquired.is_set()

        # After s1's lock context exits, s2 should acquire promptly
        await asyncio.wait_for(acquired.wait(), timeout=2.0)
        await task


async def test_lock_releases_on_exception(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="boom"):
        async with advisory_lock(session, "test_lock_exception"):
            raise ValueError("boom")
    # Re-acquire works → previous lock did release
    async with advisory_lock(session, "test_lock_exception", blocking=False):
        pass


async def test_distinct_names_dont_collide(async_engine: AsyncEngine) -> None:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as s1, factory() as s2:
        await _set_hmac_guc(s1)
        await _set_hmac_guc(s2)
        async with advisory_lock(s1, "alpha"), advisory_lock(s2, "beta"):
            pass  # Different names → no contention
