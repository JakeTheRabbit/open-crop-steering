"""Integration tests for :func:`app.core.guardrails.detect_rejection_pattern`.

Runs against real Postgres. The Phase-9 pattern detector escalates a run
of >= 3 ``guardrail_rejection`` events citing the **same** ``AP-*`` id
for one room within an hour to a ``formal_deviation`` (plan
formal-deviation criterion: repeated known-bad proposal == AI drift).

Covered:

* two same-AP rejections -> no escalation; the third -> a
  ``formal_deviation`` ``event_log`` row + audit row are written;
* a second call after escalation does not write a duplicate;
* rejections citing *different* AP ids do not aggregate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.audit import log_audit
from app.core.guardrails import detect_rejection_pattern
from app.models.audit_event import AuditEvent, AuditEventType
from app.models.event_log import EventLogEntry
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(async_engine: AsyncEngine) -> AsyncIterator[None]:
    """Clear the event_log rows these tests write (audit_event is append-only)."""
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with maker() as cleanup:
        await cleanup.execute(
            text("DELETE FROM event_log WHERE room_id LIKE 'pd-%'")
        )
        await cleanup.commit()
    yield


async def _seed_rejection(
    session: AsyncSession, room_id: str, ap_id: str
) -> None:
    """Write one ``guardrail_rejection`` audit row citing ``ap_id``."""
    await log_audit(
        session,
        event_type=AuditEventType.guardrail_rejection,
        actor_id="supervisor",
        room_id=room_id,
        summary=f"guardrail rejection citing {ap_id}",
        params={"param_name": "temp_day"},
        reason_codes=[ap_id],
    )


async def _formal_deviation_count(
    session: AsyncSession, room_id: str
) -> int:
    """Count ``formal_deviation`` event_log rows for a room."""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(EventLogEntry)
                .where(
                    EventLogEntry.event_type
                    == AuditEventType.formal_deviation,
                    EventLogEntry.room_id == room_id,
                )
            )
        ).scalar_one()
    )


# --------------------------------------------------------------------- tests


async def test_third_same_ap_rejection_escalates_to_formal_deviation(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """Two same-AP rejections do not escalate; the third does.

    After the first two rejections the detector returns ``False`` (below
    the threshold of 3). The third rejection takes the count to 3 and
    the detector emits a ``formal_deviation`` event + audit row.
    """
    room = "pd-r1"

    # First rejection -> count 1 -> no escalation.
    await _seed_rejection(session, room, "AP-02")
    escalated = await detect_rejection_pattern(session, room, "AP-02")
    await session.commit()
    assert escalated is False
    assert await _formal_deviation_count(session, room) == 0

    # Second rejection -> count 2 -> still no escalation.
    await _seed_rejection(session, room, "AP-02")
    escalated = await detect_rejection_pattern(session, room, "AP-02")
    await session.commit()
    assert escalated is False
    assert await _formal_deviation_count(session, room) == 0

    # Third rejection -> count 3 -> escalation.
    await _seed_rejection(session, room, "AP-02")
    escalated = await detect_rejection_pattern(session, room, "AP-02")
    await session.commit()
    assert escalated is True

    # A formal_deviation event_log row was written.
    assert await _formal_deviation_count(session, room) == 1
    deviation = (
        await session.execute(
            select(EventLogEntry).where(
                EventLogEntry.event_type
                == AuditEventType.formal_deviation,
                EventLogEntry.room_id == room,
            )
        )
    ).scalar_one()
    assert "AP-02" in deviation.reason_codes
    assert deviation.payload["rejection_count"] == 3

    # ...and a matching formal_deviation audit row.
    audit_count = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.event_type == AuditEventType.formal_deviation,
            AuditEvent.room_id == room,
        )
    )
    assert int(audit_count or 0) == 1


async def test_no_duplicate_formal_deviation_while_one_is_open(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A re-run after escalation does not write a second deviation.

    Once an unacknowledged ``formal_deviation`` exists for the room +
    AP, a further rejection + detector call returns ``True`` (the
    pattern is still open) but writes no duplicate row.
    """
    room = "pd-r2"
    for _ in range(3):
        await _seed_rejection(session, room, "AP-03")
    escalated = await detect_rejection_pattern(session, room, "AP-03")
    await session.commit()
    assert escalated is True
    assert await _formal_deviation_count(session, room) == 1

    # A fourth rejection + another detector call — still just one row.
    await _seed_rejection(session, room, "AP-03")
    escalated = await detect_rejection_pattern(session, room, "AP-03")
    await session.commit()
    assert escalated is True
    assert await _formal_deviation_count(session, room) == 1


async def test_different_ap_ids_do_not_aggregate(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """Rejections citing different AP ids do not sum toward escalation.

    Three rejections — one each of AP-01, AP-02, AP-03 — never reach the
    threshold for any single AP id, so nothing escalates.
    """
    room = "pd-r3"
    for ap_id in ("AP-01", "AP-02", "AP-03"):
        await _seed_rejection(session, room, ap_id)

    for ap_id in ("AP-01", "AP-02", "AP-03"):
        escalated = await detect_rejection_pattern(session, room, ap_id)
        assert escalated is False
    await session.commit()
    assert await _formal_deviation_count(session, room) == 0


async def test_non_anti_pattern_code_never_escalates(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A non-``AP-*`` reason code (e.g. out_of_bounds) never escalates.

    Only repeated *anti-pattern* rejections indicate model drift; a run
    of out-of-bounds rejections is just clamping and is not escalated.
    """
    room = "pd-r4"
    escalated = await detect_rejection_pattern(
        session, room, "out_of_bounds"
    )
    assert escalated is False
    await session.commit()
    assert await _formal_deviation_count(session, room) == 0
