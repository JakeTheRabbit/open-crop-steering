"""Postgres advisory-lock helpers.

Used by the supervisor / executor / seal workers so that only one
instance of each loop is active at a time across processes (or restarts
mid-tick). Locks are session-scoped — they hang off the AsyncSession's
underlying connection and release explicitly via ``pg_advisory_unlock``
(or implicitly when the session closes).

Naming: keep keys human-readable (``"supervisor_tick"``, ``"executor_consume"``,
``"seal_daily"``); the helper hashes them into the bigint that
``pg_advisory_lock`` expects.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class LockBusyError(RuntimeError):
    """Non-blocking acquire failed — another session holds the lock."""

    def __init__(self, lock_name: str) -> None:
        super().__init__(f"advisory lock '{lock_name}' is busy")
        self.lock_name = lock_name


async def _lock_key(session: AsyncSession, name: str) -> int:
    """Stable bigint hash of a lock name."""
    result = await session.execute(
        text("SELECT hashtextextended(:n, 0)::bigint"), {"n": name}
    )
    return int(result.scalar_one())


@asynccontextmanager
async def advisory_lock(
    session: AsyncSession,
    name: str,
    *,
    blocking: bool = False,
) -> AsyncIterator[None]:
    """Hold a Postgres session-level advisory lock for the duration of the block.

    Args:
        session: Bound to the connection that should own the lock. The lock
            attaches to *the connection*; it persists across statements but
            releases when ``pg_advisory_unlock`` runs or the connection drops.
        name: Human-readable identifier, hashed to a bigint key.
        blocking: ``True`` waits indefinitely for the lock; ``False`` raises
            :class:`LockBusyError` immediately if held elsewhere.

    Raises:
        LockBusyError: in non-blocking mode when another session has the lock.
    """
    key = await _lock_key(session, name)

    if blocking:
        await session.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
    else:
        result = await session.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": key}
        )
        if not bool(result.scalar_one()):
            raise LockBusyError(name)

    try:
        yield
    finally:
        await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
