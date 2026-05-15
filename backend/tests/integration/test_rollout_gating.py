"""Integration tests for the Phase-9 rollout-advance gating.

Runs against real Postgres. Covered (plan REQ-010):

* :func:`app.core.rollout.can_advance` reports ``can_advance=False`` for
  a room with an unresolved formal deviation;
* :func:`app.core.rollout.advance_stage` raises while a deviation is
  open;
* :func:`app.core.deviations.acknowledge_deviation` clears the deviation
  and unblocks the advance — which then bumps ``rollout_stage`` and
  writes a ``rollout_advanced`` audit row;
* a clean room (no deviation) advances directly;
* a room already at the final stage cannot advance.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.deviations import acknowledge_deviation, list_open_deviations
from app.core.rollout import ROLLOUT_STAGES, advance_stage, can_advance
from app.models.audit_event import AuditEvent, AuditEventType
from app.models.event_log import EventLogEntry, EventSeverity
from app.models.room_runtime import RoomRuntime
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(async_engine: AsyncEngine) -> AsyncIterator[None]:
    """Clear the room_runtime + event_log rows these tests use."""
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with maker() as cleanup:
        await cleanup.execute(
            text("DELETE FROM event_log WHERE room_id LIKE 'rg-%'")
        )
        await cleanup.execute(
            text("DELETE FROM room_runtime WHERE room_id LIKE 'rg-%'")
        )
        await cleanup.commit()
    yield


async def _seed_room(
    session: AsyncSession, room_id: str, *, stage: str = "report_only"
) -> RoomRuntime:
    """Seed a ``room_runtime`` row at a given rollout stage."""
    runtime = RoomRuntime(room_id=room_id, rollout_stage=stage)
    session.add(runtime)
    await session.flush()
    return runtime


async def _seed_deviation(
    session: AsyncSession, room_id: str
) -> EventLogEntry:
    """Seed an open (unacknowledged) formal-deviation event_log row."""
    deviation = EventLogEntry(
        event_type=AuditEventType.formal_deviation,
        severity=EventSeverity.critical,
        room_id=room_id,
        summary=f"formal deviation for {room_id}",
        payload={"pattern": "test"},
        reason_codes=["AP-02", "formal_deviation"],
    )
    session.add(deviation)
    await session.flush()
    return deviation


# --------------------------------------------------------------------- tests


async def test_open_deviation_blocks_advance(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A room with an unresolved formal deviation cannot advance."""
    await _seed_room(session, "rg-r1", stage="report_only")
    await _seed_deviation(session, "rg-r1")
    await session.flush()

    check = await can_advance(session, "rg-r1")
    assert check.can_advance is False
    assert check.no_open_deviations is False
    assert check.open_deviation_count == 1
    assert any("formal deviation" in b for b in check.blockers)

    # advance_stage refuses while the deviation is open.
    with pytest.raises(ValueError, match="formal deviation"):
        await advance_stage(session, "rg-r1", qap_user="rg-qap")


async def test_acknowledging_deviation_unblocks_advance(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """Acknowledging the deviation clears the block; the advance then runs.

    Before ack: ``can_advance`` is ``False``. After a QAP acknowledges
    the deviation, ``can_advance`` is ``True`` and ``advance_stage``
    bumps the room from ``report_only`` (index 0) to ``stage_1``
    (index 1) and writes a ``rollout_advanced`` audit row.
    """
    await _seed_room(session, "rg-r2", stage="report_only")
    deviation = await _seed_deviation(session, "rg-r2")
    await session.flush()

    assert (await can_advance(session, "rg-r2")).can_advance is False

    # A QAP acknowledges the deviation.
    acked = await acknowledge_deviation(
        session,
        deviation.id,
        qap_user="rg-qap",
        notes="root cause addressed",
    )
    assert acked.acknowledged_by == "rg-qap"
    assert acked.acknowledged_at is not None

    # The deviation no longer appears as open.
    assert await list_open_deviations(session, "rg-r2") == []

    # The advance is now permitted.
    check = await can_advance(session, "rg-r2")
    assert check.can_advance is True
    assert check.no_open_deviations is True

    runtime = await advance_stage(session, "rg-r2", qap_user="rg-qap")
    await session.flush()
    assert runtime.rollout_stage == "stage_1"

    # A rollout_advanced audit row was written.
    audit_count = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.event_type == AuditEventType.rollout_advanced,
            AuditEvent.room_id == "rg-r2",
        )
    )
    assert int(audit_count or 0) == 1


async def test_clean_room_advances_directly(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A room with no open deviation advances one rung directly."""
    await _seed_room(session, "rg-r3", stage="stage_2")
    await session.flush()

    check = await can_advance(session, "rg-r3")
    assert check.can_advance is True
    assert check.current_stage.name == "stage_2"
    assert check.next_stage is not None
    assert check.next_stage.name == "stage_3"

    runtime = await advance_stage(session, "rg-r3", qap_user="rg-qap")
    await session.flush()
    assert runtime.rollout_stage == "stage_3"


async def test_room_at_final_stage_cannot_advance(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A room already at the final rollout stage cannot advance."""
    final = ROLLOUT_STAGES[-1].name
    await _seed_room(session, "rg-r4", stage=final)
    await session.flush()

    check = await can_advance(session, "rg-r4")
    assert check.can_advance is False
    assert check.next_stage is None
    assert any("final" in b for b in check.blockers)

    with pytest.raises(ValueError, match="final"):
        await advance_stage(session, "rg-r4", qap_user="rg-qap")


async def test_acknowledge_rejects_already_acked_deviation(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """Acknowledging an already-acknowledged deviation raises ValueError."""
    await _seed_room(session, "rg-r5")
    deviation = await _seed_deviation(session, "rg-r5")
    await session.flush()

    await acknowledge_deviation(session, deviation.id, qap_user="rg-qap")
    with pytest.raises(ValueError, match="already acknowledged"):
        await acknowledge_deviation(
            session, deviation.id, qap_user="rg-qap"
        )


async def test_acknowledge_rejects_non_deviation_event(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """Acknowledging a non-deviation event_log row raises ValueError."""
    info = EventLogEntry(
        event_type=AuditEventType.info_event,
        severity=EventSeverity.info,
        room_id="rg-r6",
        summary="just an info event",
        payload={},
        reason_codes=[],
    )
    session.add(info)
    await session.flush()

    with pytest.raises(ValueError, match="not a formal_deviation"):
        await acknowledge_deviation(session, info.id, qap_user="rg-qap")
