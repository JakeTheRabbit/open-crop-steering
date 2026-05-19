"""Integration tests for :mod:`app.core.seal`.

Covers daily-seal computation (idempotency + linkage to the audit
chain head) and audit-chain verification on an intact chain. Runs
against real Postgres.

Seals are computed for *today* (UTC) because :func:`log_audit` stamps
``occurred_at`` server-side with ``now()`` and ``audit_event`` rows are
append-only — there is no way to back-date a row, so "today" is the
only date guaranteed to contain the events this test just inserted.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.config import get_settings
from app.core.audit import log_audit
from app.core.seal import compute_daily_seal, verify_chain, verify_seal
from app.models.audit_event import AuditEvent, AuditEventType
from app.models.user import User
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fixtures.audit_chain import construct_id_chain_divergence

pytestmark = pytest.mark.integration


async def _seed_events(
    session: AsyncSession, uid: str, count: int
) -> list[AuditEvent]:
    """Insert *count* audit events for a fresh user and return them."""
    session.add(User(id=uid, display_name=f"Seal User {uid}"))
    await session.flush()
    rows: list[AuditEvent] = []
    for i in range(count):
        rows.append(
            await log_audit(
                session,
                event_type=AuditEventType.info_event,
                actor_id=uid,
                summary=f"{uid}-event-{i}",
                params={"i": i},
            )
        )
    return rows


async def test_compute_daily_seal_links_to_chain_head(
    session: AsyncSession,
) -> None:
    """The seal records the day's last event id + its HMAC as the head."""
    rows = await _seed_events(session, "seal-head", 3)
    await session.flush()
    today = dt.datetime.now(dt.UTC).date()

    seal = await compute_daily_seal(session, today)
    await session.flush()

    # The seal's last_event_id is the latest audit row of the day; its
    # last_event_hmac equals that row's stored HMAC (the chain head).
    assert seal.seal_date == today
    assert seal.last_event_id >= rows[-1].id
    assert seal.first_event_id <= rows[0].id

    head = await session.execute(
        text("SELECT hmac FROM audit_event WHERE id = :id"),
        {"id": seal.last_event_id},
    )
    assert bytes(seal.last_event_hmac) == bytes(head.scalar_one())


async def test_compute_daily_seal_is_idempotent(
    session: AsyncSession,
) -> None:
    """A second call for the same date returns the same seal row."""
    await _seed_events(session, "seal-idem", 2)
    await session.flush()
    today = dt.datetime.now(dt.UTC).date()

    first = await compute_daily_seal(session, today)
    await session.flush()
    first_id = first.id
    first_hmac = bytes(first.seal_hmac)

    second = await compute_daily_seal(session, today)
    await session.flush()

    assert second.id == first_id
    assert bytes(second.seal_hmac) == first_hmac


async def test_compute_daily_seal_hmac_verifies(
    session: AsyncSession,
) -> None:
    """The stored seal HMAC verifies against the configured key."""
    await _seed_events(session, "seal-verify", 2)
    await session.flush()
    today = dt.datetime.now(dt.UTC).date()

    seal = await compute_daily_seal(session, today)
    await session.flush()

    settings = get_settings()
    key_hex = settings.hmac_keys[settings.hmac_key_id_current]
    assert verify_seal(seal, key_hex) is True

    # A wrong key fails the check.
    wrong_key = "00" * 32
    assert verify_seal(seal, wrong_key) is False


async def test_compute_daily_seal_no_events_raises(
    session: AsyncSession,
) -> None:
    """Sealing a day with no audit events raises ValueError."""
    # A date far in the past is guaranteed empty.
    empty_day = dt.date(2000, 1, 1)
    with pytest.raises(ValueError, match="nothing to seal"):
        await compute_daily_seal(session, empty_day)


async def test_verify_chain_ok_on_intact_chain(
    session: AsyncSession,
) -> None:
    """verify_chain reports ok=True for an untampered range."""
    rows = await _seed_events(session, "seal-chain", 4)
    await session.flush()

    result = await verify_chain(
        session, start_id=rows[0].id, end_id=rows[-1].id
    )
    assert result.ok is True
    assert result.first_bad_id is None
    assert result.rows_checked == 4


async def test_verify_chain_empty_range_is_ok(
    session: AsyncSession,
) -> None:
    """An empty id range verifies trivially as ok."""
    result = await verify_chain(
        session, start_id=10**15, end_id=10**15 + 1
    )
    assert result.ok is True
    assert result.rows_checked == 0


async def test_verify_chain_seeded_segment_ok(session: AsyncSession) -> None:
    """verify_chain reports ok=True for a ``start_id``-bounded range.

    The bound covers this test's three sequentially-seeded rows; the
    range's first row links to an out-of-range predecessor, which
    verify_chain accepts as a legitimate fragment start.
    """
    rows = await _seed_events(session, "seal-full", 3)
    await session.flush()
    result = await verify_chain(session, start_id=rows[0].id)
    assert result.ok is True
    assert result.first_bad_id is None
    assert result.rows_checked == 3


async def test_verify_chain_ok_on_id_chain_divergence(
    session: AsyncSession,
) -> None:
    """verify_chain tolerates id order != chain order — no false alarm.

    Concurrent inserts can commit with id order reversed relative to
    chain order. The chain is still a valid linked list; verify_chain
    follows the ``prev_event_hash`` linkage and must report ok. This is
    the exact scenario the old id-ordered walk false-flagged as
    tampering.
    """
    pair = await construct_id_chain_divergence(session)
    assert pair.lo_id < pair.hi_id  # divergence really constructed

    # Full-table verify — the unbounded scenario that used to false-alarm.
    full = await verify_chain(session)
    assert full.ok is True, full.message

    # Bounded range covering the diverged pair.
    bounded = await verify_chain(session, start_id=pair.lo_id, end_id=pair.hi_id)
    assert bounded.ok is True, bounded.message
    assert bounded.rows_checked == 2


async def test_verify_chain_detects_fork(session: AsyncSession) -> None:
    """verify_chain reports ok=False on a genuine chain fork.

    A fork — two rows chained onto one predecessor — is real corruption,
    exactly what the baseline MAX(id) trigger produced after a
    divergence. Rewinding the head pointer reproduces a fork through the
    ordinary insert path; verify_chain must catch it.
    """
    a = await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id="fork-test",
        summary="a",
    )
    # b and c both chain onto `a`. Their params differ so their HMACs
    # differ — the defect is a shared *predecessor* (a fork), not a
    # duplicate row. (summary is not part of the hashed payload, and
    # same-transaction rows share occurred_at, so params is the lever.)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id="fork-test",
        summary="b",
        params={"branch": "b"},
    )

    # Rewind the head pointer so the next insert also chains onto `a`.
    await session.execute(
        text("UPDATE audit_chain_head SET head_hmac = :h WHERE singleton"),
        {"h": bytes(a.hmac)},
    )
    c = await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id="fork-test",
        summary="c",
        params={"branch": "c"},
    )
    assert bytes(c.prev_event_hash) == bytes(a.hmac)  # fork created

    result = await verify_chain(session)
    assert result.ok is False
    assert result.first_bad_id is not None
    assert "fork" in result.message.lower()


async def test_compute_daily_seal_uses_true_chain_tip(
    session: AsyncSession,
) -> None:
    """The daily seal head is the chain tip, not MAX(id), under divergence."""
    pair = await construct_id_chain_divergence(session)
    await session.flush()
    today = dt.datetime.now(dt.UTC).date()

    seal = await compute_daily_seal(session, today)
    await session.flush()

    # `lo` has the lower id but is last in chain order — the true tip.
    assert seal.last_event_id == pair.lo_id
    assert seal.last_event_id != pair.hi_id  # NOT MAX(id)
    assert bytes(seal.last_event_hmac) == pair.lo_hmac
