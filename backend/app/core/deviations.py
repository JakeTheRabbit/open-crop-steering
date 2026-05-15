"""Formal-deviation lifecycle — list + acknowledge.

A *formal deviation* is the second-highest rung of the event taxonomy
(plan locked decision #15): an applied value out of bounds, stale data
used in real control, an unauthorized change, an audit-chain failure, a
missed control event with crop impact, a backup/restore failure — or the
Phase 9 pattern-detection escalation (>= 3 same-``AP-*`` guardrail
rejections in 1h, see :func:`app.core.guardrails.detect_rejection_pattern`).

Formal deviations are recorded as ``formal_deviation``
:class:`~app.models.event_log.EventLogEntry` rows. A deviation is *open*
until a QAP acknowledges it; an open deviation **blocks rollout-stage
advancement** for its room (REQ-010 — see :func:`app.core.rollout.can_advance`).

This module is the deviation half of that lifecycle:

* :func:`list_open_deviations` — the open (unacknowledged) deviations,
  optionally scoped to one room.
* :func:`acknowledge_deviation` — a QAP acknowledges a deviation,
  clearing it and writing a ``deviation_acknowledged`` audit row.

The acknowledgement is QAP-gated at the API layer
(:mod:`app.api.rollout`); this module is transport-agnostic and takes
the resolved QAP user id.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from app.core.audit import log_audit
from app.models.audit_event import AuditEventType
from app.models.event_log import EventLogEntry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


async def list_open_deviations(
    session: AsyncSession, room_id: str | None = None
) -> list[EventLogEntry]:
    """Return the open (unacknowledged) formal-deviation event rows.

    A deviation is *open* while ``acknowledged_at IS NULL``. Rows are
    returned newest first.

    Args:
        session: Active async session.
        room_id: Optional room filter — when given, only that room's
            open deviations are returned; otherwise every room's.

    Returns:
        The open ``formal_deviation`` :class:`~app.models.event_log.EventLogEntry`
        rows.
    """
    stmt = (
        select(EventLogEntry)
        .where(
            EventLogEntry.event_type == AuditEventType.formal_deviation,
            EventLogEntry.acknowledged_at.is_(None),
        )
        .order_by(EventLogEntry.occurred_at.desc())
    )
    if room_id is not None:
        stmt = stmt.where(EventLogEntry.room_id == room_id)
    rows = (await session.execute(stmt)).scalars().all()
    log.debug(
        "open_deviations_listed",
        room_id=room_id,
        count=len(rows),
    )
    return list(rows)


async def acknowledge_deviation(
    session: AsyncSession,
    event_id: int,
    *,
    qap_user: str,
    notes: str | None = None,
) -> EventLogEntry:
    """Acknowledge a formal deviation — clear it, audit-row the ack.

    Sets ``acknowledged_by`` / ``acknowledged_at`` on the deviation's
    :class:`~app.models.event_log.EventLogEntry` row and writes a
    ``deviation_acknowledged`` audit event (the acknowledgement itself is
    part of the tamper-evident chain — plan locked decision #6).

    Once acknowledged the deviation no longer blocks rollout-stage
    advancement for its room (:func:`app.core.rollout.can_advance`).

    Args:
        session: Active async session.
        event_id: The ``event_log`` id of the deviation to acknowledge.
        qap_user: ``users.id`` of the acknowledging QAP (the caller has
            already role-checked them).
        notes: Optional free-text acknowledgement note (e.g. the
            corrective action taken), recorded in the audit ``params``.

    Returns:
        The updated, now-acknowledged :class:`~app.models.event_log.EventLogEntry`.

    Raises:
        ValueError: If ``event_id`` is not a ``formal_deviation`` event,
            or it is already acknowledged (acknowledgement is one-shot).
    """
    event = await session.get(EventLogEntry, event_id)
    if event is None:
        raise ValueError(f"event_log row {event_id} not found")
    if event.event_type is not AuditEventType.formal_deviation:
        raise ValueError(
            f"event_log row {event_id} is {event.event_type.value}, "
            "not a formal_deviation — only deviations are acknowledged"
        )
    if event.acknowledged_at is not None:
        raise ValueError(
            f"formal deviation {event_id} is already acknowledged "
            f"(by {event.acknowledged_by} at "
            f"{event.acknowledged_at.isoformat()})"
        )

    now = dt.datetime.now(dt.UTC)
    event.acknowledged_by = qap_user
    event.acknowledged_at = now
    await session.flush()

    await log_audit(
        session,
        event_type=AuditEventType.deviation_acknowledged,
        actor_id=qap_user,
        actor_role="qap",
        room_id=event.room_id,
        summary=(
            f"Formal deviation #{event_id} acknowledged by {qap_user}"
        ),
        params={
            "event_id": event_id,
            "deviation_summary": event.summary,
            "notes": notes,
        },
        reason_codes=list(event.reason_codes or []),
    )

    log.info(
        "deviation_acknowledged",
        event_id=event_id,
        room_id=event.room_id,
        qap_user=qap_user,
    )
    return event
