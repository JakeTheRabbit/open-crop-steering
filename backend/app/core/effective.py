"""Effective-target reads + materialized-view refresh.

``effective_target`` is a Postgres MATERIALIZED VIEW (defined in the
baseline migration) equal to::

    active approved recipe value + sum(active, non-expired overlay deltas)

The executor reads this on the hot path, so it is materialized rather
than computed per request. This module is the thin read/refresh surface
over it.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.effective_target import EffectiveTarget

log = structlog.get_logger(__name__)

_MATVIEW = "effective_target"


async def _matview_is_populated(session: AsyncSession) -> bool:
    """Return True if the matview holds data (``WITH NO DATA`` -> False).

    ``REFRESH ... CONCURRENTLY`` is illegal on a never-populated view and
    aborts the transaction, so we probe ``pg_class.relispopulated`` first
    and pick the refresh mode without risking an abort.
    """
    result = await session.execute(
        text("SELECT relispopulated FROM pg_class WHERE relname = :n"),
        {"n": _MATVIEW},
    )
    populated = result.scalar_one_or_none()
    return bool(populated)


async def refresh_effective_targets(session: AsyncSession) -> None:
    """Refresh the ``effective_target`` materialized view.

    Uses a ``CONCURRENTLY`` refresh (does not block readers) once the
    view has been populated at least once; the very first refresh of the
    ``WITH NO DATA`` view falls back to a plain blocking refresh, which
    ``CONCURRENTLY`` cannot do.

    The refresh sees only committed data — overlays added in an
    uncommitted transaction are reflected only after that transaction
    commits and a subsequent refresh runs.

    Args:
        session: Active async session.
    """
    if await _matview_is_populated(session):
        await session.execute(
            text(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {_MATVIEW}")
        )
    else:
        log.debug("effective_target_first_refresh_non_concurrent")
        await session.execute(text(f"REFRESH MATERIALIZED VIEW {_MATVIEW}"))

    await session.flush()


async def effective_target(
    session: AsyncSession,
    room_id: str,
    day_index: int,
    param_name: str,
) -> float | None:
    """Return one effective-target value from the materialized view.

    Args:
        session: Active async session.
        room_id: Room to read.
        day_index: Cycle day to read.
        param_name: Parameter to read.

    Returns:
        The effective value (recipe + overlays), or ``None`` if there is
        no row for that ``(room, day, param)``.
    """
    result = await session.execute(
        select(EffectiveTarget.value).where(
            EffectiveTarget.room_id == room_id,
            EffectiveTarget.day_index == day_index,
            EffectiveTarget.param_name == param_name,
        )
    )
    return result.scalar_one_or_none()


async def effective_targets_for_day(
    session: AsyncSession,
    room_id: str,
    day_index: int,
) -> dict[str, EffectiveTarget]:
    """Return every effective-target row for a room + day.

    Args:
        session: Active async session.
        room_id: Room to read.
        day_index: Cycle day to read.

    Returns:
        Mapping of ``param_name`` to its :class:`EffectiveTarget` row.
        Empty if the room has no active recipe for that day.
    """
    result = await session.execute(
        select(EffectiveTarget).where(
            EffectiveTarget.room_id == room_id,
            EffectiveTarget.day_index == day_index,
        )
    )
    return {row.param_name: row for row in result.scalars()}
