"""Integration tests for :mod:`app.core.command_queue`.

Verifies the outbox primitives against real Postgres: enqueue is
idempotent on the ``idempotency_key``, batches create a header plus
per-command rows, claiming takes the oldest row and flips it to
``applying``, ``FOR UPDATE SKIP LOCKED`` lets two concurrent claimers
grab distinct rows, and the mark helpers transition status correctly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.command_queue import (
    claim_next_pending,
    enqueue_batch,
    enqueue_command,
    mark_applied,
    mark_failed,
)
from app.models.command_queue import CommandQueueEntry, CommandStatus
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def _clean_command_queue(
    async_engine: AsyncEngine,
) -> AsyncIterator[None]:
    """Start each test from an empty command queue.

    ``claim_next_pending`` scans ``command_queue`` globally and the
    enqueue helpers commit, so committed rows would otherwise leak
    between tests and break the oldest-first / SKIP-LOCKED assertions.
    This autouse fixture clears the queue + batch tables beforehand.
    """
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with maker() as cleanup:
        await cleanup.execute(text("DELETE FROM command_queue"))
        await cleanup.execute(text("DELETE FROM command_batch"))
        await cleanup.commit()
    yield


def _setpoint(
    entity: str = "input_number.f1_setpoint_temp_day", value: float = 28.0
) -> dict[str, object]:
    """Build a stock ``input_number.set_value`` command spec."""
    return {
        "domain": "input_number",
        "service": "set_value",
        "target_entity": entity,
        "service_data": {"entity_id": entity, "value": value},
        "expected_value": str(value),
    }


async def test_enqueue_command_inserts_pending(session: AsyncSession) -> None:
    """A fresh enqueue creates one pending row."""
    cmd = await enqueue_command(
        session,
        domain="input_number",
        service="set_value",
        target_entity="input_number.cq1_temp",
        service_data={"entity_id": "input_number.cq1_temp", "value": 27.0},
        expected_value="27.0",
        idempotency_key="cq-1-key",
        enqueued_by="tester",
        reason="unit test",
    )
    await session.commit()

    assert cmd.id is not None
    assert cmd.status == CommandStatus.pending
    assert cmd.attempts == 0
    assert cmd.enqueued_by == "tester"


async def test_enqueue_command_is_idempotent(session: AsyncSession) -> None:
    """Enqueuing twice with the same key yields one row, not two."""
    key = "cq-idem-key"
    first = await enqueue_command(
        session,
        domain="input_number",
        service="set_value",
        target_entity="input_number.cq2_temp",
        service_data={"entity_id": "input_number.cq2_temp", "value": 26.0},
        expected_value="26.0",
        idempotency_key=key,
        enqueued_by="tester",
    )
    await session.commit()

    second = await enqueue_command(
        session,
        domain="input_number",
        service="set_value",
        target_entity="input_number.cq2_temp",
        service_data={"entity_id": "input_number.cq2_temp", "value": 26.0},
        expected_value="26.0",
        idempotency_key=key,
        enqueued_by="tester",
    )
    await session.commit()

    assert first.id == second.id

    count = await session.execute(
        select(CommandQueueEntry).where(
            CommandQueueEntry.idempotency_key == key
        )
    )
    assert len(count.scalars().all()) == 1


async def test_enqueue_batch_creates_batch_and_entries(
    session: AsyncSession,
) -> None:
    """A batch enqueue creates the header plus one row per command."""
    batch = await enqueue_batch(
        session,
        room_id="cq-room",
        purpose="lights_on",
        idempotency_key="cq-batch-key",
        commands=[
            _setpoint("input_number.cq_room_setpoint_temp_day", 28.0),
            _setpoint("input_number.cq_room_setpoint_rh_day", 60.0),
        ],
        enqueued_by="tester",
    )
    await session.commit()

    assert batch.id is not None
    assert batch.room_id == "cq-room"

    rows = await session.execute(
        select(CommandQueueEntry).where(
            CommandQueueEntry.batch_id == batch.id
        )
    )
    entries = rows.scalars().all()
    assert len(entries) == 2
    # Each command's key derives from the batch key + its target entity.
    keys = {e.idempotency_key for e in entries}
    assert keys == {
        "cq-batch-key:input_number.cq_room_setpoint_temp_day",
        "cq-batch-key:input_number.cq_room_setpoint_rh_day",
    }
    assert all(e.status == CommandStatus.pending for e in entries)


async def test_enqueue_batch_is_idempotent(session: AsyncSession) -> None:
    """A duplicated batch enqueue does not re-create the batch or rows."""
    key = "cq-batch-idem"
    first = await enqueue_batch(
        session,
        room_id="cq-room-2",
        purpose="lights_on",
        idempotency_key=key,
        commands=[_setpoint("input_number.cq_room2_temp", 25.0)],
        enqueued_by="tester",
    )
    await session.commit()

    second = await enqueue_batch(
        session,
        room_id="cq-room-2",
        purpose="lights_on",
        idempotency_key=key,
        commands=[_setpoint("input_number.cq_room2_temp", 25.0)],
        enqueued_by="tester",
    )
    await session.commit()

    assert first.id == second.id
    rows = await session.execute(
        select(CommandQueueEntry).where(
            CommandQueueEntry.batch_id == first.id
        )
    )
    assert len(rows.scalars().all()) == 1


async def test_claim_next_pending_takes_oldest(session: AsyncSession) -> None:
    """Claiming returns the oldest pending row and flips it to applying."""
    for i in range(3):
        await enqueue_command(
            session,
            domain="input_number",
            service="set_value",
            target_entity=f"input_number.cq_claim_{i}",
            service_data={"entity_id": f"input_number.cq_claim_{i}", "value": i},
            expected_value=str(i),
            idempotency_key=f"cq-claim-{i}",
            enqueued_by="tester",
        )
    await session.commit()

    claimed = await claim_next_pending(session)
    await session.commit()

    assert claimed is not None
    assert claimed.idempotency_key == "cq-claim-0"
    assert claimed.status == CommandStatus.applying
    assert claimed.started_at is not None


async def test_claim_next_pending_none_when_empty(
    session: AsyncSession,
) -> None:
    """An empty queue yields None."""
    assert await claim_next_pending(session) is None


async def test_claim_skip_locked_gives_distinct_rows(
    async_engine: AsyncEngine,
) -> None:
    """Two concurrent claimers under SKIP LOCKED grab different rows."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    # Seed two pending commands on a throwaway session.
    async with factory() as seed:
        for i in range(2):
            await enqueue_command(
                seed,
                domain="input_number",
                service="set_value",
                target_entity=f"input_number.cq_skip_{i}",
                service_data={"value": i},
                expected_value=str(i),
                idempotency_key=f"cq-skip-{i}",
                enqueued_by="tester",
            )
        await seed.commit()

    # Two sessions claim concurrently; each holds its row lock open.
    async with factory() as s1, factory() as s2:
        first = await claim_next_pending(s1)
        second = await claim_next_pending(s2)

        assert first is not None
        assert second is not None
        # SKIP LOCKED means the second claimer skips the first's locked
        # row and takes the other one.
        assert first.id != second.id
        assert {first.idempotency_key, second.idempotency_key} == {
            "cq-skip-0",
            "cq-skip-1",
        }
        await s1.rollback()
        await s2.rollback()


async def test_mark_applied_transitions_row(session: AsyncSession) -> None:
    """mark_applied sets applied status + readback + completed_at."""
    await enqueue_command(
        session,
        domain="input_number",
        service="set_value",
        target_entity="input_number.cq_applied",
        service_data={"value": 22.0},
        expected_value="22.0",
        idempotency_key="cq-applied-key",
        enqueued_by="tester",
    )
    await session.commit()
    command = await claim_next_pending(session)
    assert command is not None

    await mark_applied(
        session, command, {"expected": "22.0", "actual": "22.0", "attempts": 1}
    )
    await session.commit()

    assert command.status == CommandStatus.applied
    assert command.completed_at is not None
    assert command.attempts == 1
    assert command.readback == {
        "expected": "22.0",
        "actual": "22.0",
        "attempts": 1,
    }
    assert command.last_error is None


async def test_mark_failed_records_attempt(session: AsyncSession) -> None:
    """mark_failed increments attempts + records the error, status held."""
    await enqueue_command(
        session,
        domain="input_number",
        service="set_value",
        target_entity="input_number.cq_failed",
        service_data={"value": 20.0},
        expected_value="20.0",
        idempotency_key="cq-failed-key",
        enqueued_by="tester",
    )
    await session.commit()
    command = await claim_next_pending(session)
    assert command is not None

    await mark_failed(session, command, "HA timeout")
    await session.commit()

    assert command.attempts == 1
    assert command.last_error == "HA timeout"
    # mark_failed itself does not terminally fail — that is the executor's
    # escalation decision — so the row is still in ``applying``.
    assert command.status == CommandStatus.applying

    await mark_failed(session, command, "HA timeout again")
    await session.commit()
    assert command.attempts == 2
    assert command.last_error == "HA timeout again"
