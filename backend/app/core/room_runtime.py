"""Helpers over the ``room_runtime`` table.

The supervisor reads a room's mutable state every tick and the
deterministic monitor writes its health state — both go through here so
the lazy-create and the cycle-day math live in one place.

* :func:`get_or_create` — fetch a room's runtime row, creating the
  default row on first access (so adding a room never needs a migration).
* :func:`cycle_day_for` — derive the 1-based cultivation cycle day from
  the room's ``cycle_start_date``.
* :func:`update_state` / :func:`mark_ticked` — write the operational
  bookkeeping fields.
"""

from __future__ import annotations

import datetime as dt

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.state_machine import RoomState
from app.models.room_runtime import DEFAULT_ROLLOUT_STAGE, RoomRuntime

log = structlog.get_logger(__name__)

#: The cycle day is always at least 1 — day 0 is not a real recipe day.
MIN_CYCLE_DAY = 1


async def get_or_create(session: AsyncSession, room_id: str) -> RoomRuntime:
    """Return the ``room_runtime`` row for ``room_id``, creating it lazily.

    A room that has never been ticked has no row yet; the first call
    creates one with the report-only default stage and a HEALTHY state.

    Args:
        session: Active async session.
        room_id: Room to fetch.

    Returns:
        The room's :class:`~app.models.room_runtime.RoomRuntime` row.
    """
    runtime = await session.get(RoomRuntime, room_id)
    if runtime is not None:
        return runtime

    runtime = RoomRuntime(
        room_id=room_id,
        rollout_stage=DEFAULT_ROLLOUT_STAGE,
        current_state=RoomState.healthy.value,
        muted=False,
        paused=False,
    )
    session.add(runtime)
    await session.flush()
    log.info("room_runtime_created", room_id=room_id)
    return runtime


def cycle_day_for(
    runtime: RoomRuntime, *, today: dt.date | None = None
) -> int:
    """Return the 1-based cultivation cycle day for a room.

    The cycle day is ``(today - cycle_start_date) + 1`` so the cycle's
    first calendar day is day 1. The result is clamped to be at least
    :data:`MIN_CYCLE_DAY`; a room with no ``cycle_start_date`` set (cycle
    not started) also reports day 1 — the recipe's first day — rather
    than failing.

    Note:
        The day is *not* clamped at the top end here. A cycle that has
        run past the end of its recipe is the executor's
        ``handle_day_overflow`` concern; this helper reports the true
        elapsed day so the overflow can be detected.

    Args:
        runtime: The room's runtime row.
        today: The date to measure against (defaults to ``date.today()``;
            injectable for deterministic tests).

    Returns:
        The cycle day, an integer ``>= MIN_CYCLE_DAY``.
    """
    if runtime.cycle_start_date is None:
        return MIN_CYCLE_DAY
    reference = today if today is not None else dt.date.today()  # noqa: DTZ011
    elapsed = (reference - runtime.cycle_start_date).days + 1
    return max(MIN_CYCLE_DAY, elapsed)


async def update_state(
    session: AsyncSession, runtime: RoomRuntime, state: RoomState
) -> RoomRuntime:
    """Persist a room's last-known health state.

    Written by the deterministic monitor / supervisor after evaluating a
    room so the next tick can cheaply read whether the room is HEALTHY
    without re-running every predicate.

    Args:
        session: Active async session.
        runtime: The room's runtime row.
        state: The newly-computed :class:`~app.core.state_machine.RoomState`.

    Returns:
        The updated runtime row.
    """
    runtime.current_state = state.value
    await session.flush()
    return runtime


async def mark_ticked(
    session: AsyncSession,
    runtime: RoomRuntime,
    *,
    at: dt.datetime | None = None,
) -> RoomRuntime:
    """Stamp ``last_tick_at`` after a supervisor tick processed the room.

    Args:
        session: Active async session.
        runtime: The room's runtime row.
        at: The tick timestamp (defaults to ``now()`` UTC; injectable for
            tests).

    Returns:
        The updated runtime row.
    """
    runtime.last_tick_at = at if at is not None else dt.datetime.now(dt.UTC)
    await session.flush()
    return runtime
