"""Shared helpers for audit-chain tests.

The headline helper, :func:`construct_id_chain_divergence`, deterministically
reproduces the committed state a lost advisory-lock race produces — a
lower-id ``audit_event`` row chained *after* a higher-id one — without
needing a flaky real race.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.audit_event import AuditEvent, AuditEventType
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class DivergedPair:
    """Two audit rows whose ``id`` order is reversed vs chain order.

    ``lo_id < hi_id``, but in *chain* order ``hi`` precedes ``lo``
    (``lo``'s ``prev_event_hash`` equals ``hi``'s ``hmac``). ``lo`` is
    therefore the true chain tip even though it has the lower id.
    """

    lo_id: int
    hi_id: int
    lo_hmac: bytes
    hi_hmac: bytes


async def _insert_with_id(
    session: AsyncSession, event_id: int, actor: str
) -> AuditEvent:
    """Insert one audit row with an explicit id; the trigger chains it."""
    row = AuditEvent(
        id=event_id,
        event_type=AuditEventType.info_event,
        actor_id=actor,
        params={},
        reason_codes=[],
        key_id=1,
        prev_event_hash=b"\x00" * 32,  # overwritten by the trigger
        hmac=b"\x00" * 32,  # overwritten by the trigger
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def construct_id_chain_divergence(
    session: AsyncSession, *, actor: str = "divergence"
) -> DivergedPair:
    """Deterministically build an id-order / chain-order divergence.

    Reserves two consecutive sequence ids, then inserts the **higher**
    id first so the chain trigger links the lower-id row onto it. The
    result is byte-identical to what a genuine concurrent race produces
    when the higher-id transaction wins the advisory lock — the trigger
    chains purely on insert order, never on how the id was assigned.

    Runs inside *session*'s transaction; nothing is committed.

    Returns:
        A :class:`DivergedPair` describing the two rows.
    """
    seq = (
        await session.execute(
            text("SELECT pg_get_serial_sequence('audit_event', 'id')")
        )
    ).scalar_one()
    lo_id = int(
        (
            await session.execute(
                text("SELECT nextval(CAST(:s AS regclass))"), {"s": seq}
            )
        ).scalar_one()
    )
    hi_id = int(
        (
            await session.execute(
                text("SELECT nextval(CAST(:s AS regclass))"), {"s": seq}
            )
        ).scalar_one()
    )

    # Higher id first → it chains onto the current tip. Lower id second
    # → the trigger chains it onto `hi`, reversing id order vs chain
    # order while keeping the chain a valid linked list.
    hi = await _insert_with_id(session, hi_id, actor)
    lo = await _insert_with_id(session, lo_id, actor)

    return DivergedPair(
        lo_id=lo_id,
        hi_id=hi_id,
        lo_hmac=bytes(lo.hmac),
        hi_hmac=bytes(hi.hmac),
    )
