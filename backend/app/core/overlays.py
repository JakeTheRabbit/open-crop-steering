"""Runtime overlays — bounded, time-limited deltas on top of the recipe.

The executor writes ``recipe value + sum(active overlay deltas)`` to HA.
This module manages the ``runtime_adjustment`` (overlay) rows:

* :func:`add_adjustment` — insert one overlay.
* :func:`revert_adjustment` — deactivate an overlay before it expires.
* :func:`expire_due_adjustments` — sweep overlays past ``expires_at``.

Every mutation writes an ``audit_event`` and refreshes the
``effective_target`` materialized view so the hot path the executor
reads stays current. The AI calls :func:`add_adjustment` (within
guardrails, validated upstream); humans call all three.
"""

from __future__ import annotations

import datetime as dt

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_audit
from app.core.effective import refresh_effective_targets
from app.models.audit_event import AuditEventType
from app.models.runtime_adjustment import (
    AdjustmentMode,
    AdjustmentSource,
    RuntimeAdjustment,
)

log = structlog.get_logger(__name__)


async def add_adjustment(
    session: AsyncSession,
    *,
    room_id: str,
    day_index: int,
    param_name: str,
    delta: float,
    source: AdjustmentSource,
    mode: AdjustmentMode,
    expires_at: dt.datetime,
    created_by: str,
    snapshot_id: int | None = None,
    novel_proposal: bool = False,
    reason_codes: list[str] | None = None,
) -> RuntimeAdjustment:
    """Insert a runtime overlay and refresh the effective-target view.

    Args:
        session: Active async session.
        room_id: Room the overlay applies to.
        day_index: Cycle day the overlay applies to.
        param_name: Parameter being shifted.
        delta: Signed delta added to the recipe value.
        source: Who/what produced the overlay (AI vs human).
        mode: Rollout mode in effect when the overlay was created.
        expires_at: Timezone-aware expiry; the overlay stops counting
            once ``now() > expires_at``.
        created_by: ``users.id`` of the actor.
        snapshot_id: Optional sensor-snapshot id the decision was based on.
        novel_proposal: ``True`` if the AI proposed outside the
            deterministic allowed-action set (plan risk #13 / #8).
        reason_codes: Coupling / anti-pattern / saturation IDs supporting
            the decision.

    Returns:
        The persisted :class:`RuntimeAdjustment`.
    """
    adjustment = RuntimeAdjustment(
        room_id=room_id,
        day_index=day_index,
        param_name=param_name,
        delta=delta,
        source=source,
        mode=mode,
        created_by=created_by,
        expires_at=expires_at,
        active=True,
        snapshot_id=snapshot_id,
        novel_proposal=novel_proposal,
        reason_codes=reason_codes or [],
    )
    session.add(adjustment)
    await session.flush()  # assigns adjustment.id

    await log_audit(
        session,
        event_type=AuditEventType.runtime_adjustment_added,
        actor_id=created_by,
        room_id=room_id,
        summary=(
            f"Added overlay {param_name} delta={delta:+g} "
            f"(day {day_index}, source={source.value}, mode={mode.value})"
        ),
        runtime_adjustment_id=adjustment.id,
        snapshot_id=snapshot_id,
        reason_codes=reason_codes or [],
        params={
            "day_index": day_index,
            "param_name": param_name,
            "delta": delta,
            "expires_at": expires_at.isoformat(),
            "novel_proposal": novel_proposal,
        },
    )

    await refresh_effective_targets(session)

    log.info(
        "runtime_adjustment_added",
        room_id=room_id,
        adjustment_id=adjustment.id,
        param_name=param_name,
        delta=delta,
        source=source.value,
    )
    return adjustment


async def revert_adjustment(
    session: AsyncSession,
    adjustment_id: int,
    reverted_by: str,
) -> RuntimeAdjustment:
    """Deactivate an overlay before its natural expiry.

    Args:
        session: Active async session.
        adjustment_id: Overlay to revert.
        reverted_by: ``users.id`` of the actor reverting it.

    Returns:
        The updated :class:`RuntimeAdjustment`.

    Raises:
        ValueError: If the overlay does not exist.
    """
    adjustment = await session.get(RuntimeAdjustment, adjustment_id)
    if adjustment is None:
        raise ValueError(f"runtime adjustment {adjustment_id} not found")

    adjustment.active = False
    adjustment.reverted_at = dt.datetime.now(dt.UTC)
    adjustment.reverted_by = reverted_by
    await session.flush()

    await log_audit(
        session,
        event_type=AuditEventType.runtime_adjustment_reverted,
        actor_id=reverted_by,
        room_id=adjustment.room_id,
        summary=(
            f"Reverted overlay {adjustment.param_name} "
            f"delta={adjustment.delta:+g} (day {adjustment.day_index})"
        ),
        runtime_adjustment_id=adjustment.id,
        params={
            "day_index": adjustment.day_index,
            "param_name": adjustment.param_name,
            "delta": adjustment.delta,
        },
    )

    await refresh_effective_targets(session)

    log.info(
        "runtime_adjustment_reverted",
        room_id=adjustment.room_id,
        adjustment_id=adjustment.id,
    )
    return adjustment


async def expire_due_adjustments(session: AsyncSession) -> int:
    """Deactivate every active overlay whose ``expires_at`` has passed.

    Intended to be driven by a periodic worker. Reverting is left
    unset — natural expiry is distinct from a deliberate revert, so
    ``reverted_at`` / ``reverted_by`` stay ``NULL``.

    Args:
        session: Active async session.

    Returns:
        Number of overlays expired by this call.
    """
    now = dt.datetime.now(dt.UTC)

    due = await session.execute(
        select(RuntimeAdjustment.id).where(
            RuntimeAdjustment.active.is_(True),
            RuntimeAdjustment.expires_at <= now,
        )
    )
    due_ids = list(due.scalars())
    if not due_ids:
        return 0

    await session.execute(
        update(RuntimeAdjustment)
        .where(RuntimeAdjustment.id.in_(due_ids))
        .values(active=False)
    )
    await session.flush()

    await refresh_effective_targets(session)

    log.info("runtime_adjustments_expired", count=len(due_ids), ids=due_ids)
    return len(due_ids)
