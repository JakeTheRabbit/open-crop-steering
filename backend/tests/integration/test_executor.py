"""Integration tests for :mod:`app.workers.executor`.

The Home Assistant client is replaced with :class:`FakeHAClient`, a small
double whose ``call_and_readback`` returns a scripted success or failure.
That keeps the tests fast + deterministic while still exercising the real
command-queue tables, advisory lock, audit chain, and event log against
Postgres.

Covered: a pending command applies + audit-rows; a failing command
increments attempts; escalation emits ``system_warning`` at 3 cumulative
failures and ``formal_deviation`` (terminal) at 5; ``on_lights_on``
enqueues a batch and a duplicate lights-on trigger does not double-enqueue.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import pytest_asyncio
from app.ha_client import ReadbackResult
from app.models.audit_event import AuditEvent, AuditEventType
from app.models.command_queue import CommandQueueEntry, CommandStatus
from app.models.event_log import EventLogEntry
from app.models.recipe_revision import (
    RecipeRevision,
    RecipeRevisionParam,
    RecipeStatus,
)
from app.models.user import User
from app.workers.executor import Executor
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def _clean_command_queue(
    async_engine: AsyncEngine,
) -> AsyncIterator[None]:
    """Start each test from an empty command queue.

    The conftest ``session`` fixture isolates tests by rollback, but the
    executor commits through its own sessions and scans ``command_queue``
    *globally* (``claim_next_pending`` / ``run_once`` are not room-scoped).
    Committed rows therefore leak across tests, so this autouse fixture
    clears the queue + batch tables on a throwaway session beforehand.
    """
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with maker() as cleanup:
        await cleanup.execute(text("DELETE FROM command_queue"))
        await cleanup.execute(text("DELETE FROM command_batch"))
        await cleanup.commit()
    yield


class FakeHAClient:
    """Test double for :class:`app.ha_client.HAClient`.

    ``call_and_readback`` echoes a scripted outcome. ``mode`` is one of:

    * ``"ok"`` — readback applied, ``actual_value`` equals the expected.
    * ``"mismatch"`` — readback returns ``applied=False`` (state never
      settled).
    * ``"raise"`` — raises a :class:`RuntimeError`, simulating a transport
      failure.

    Every call is recorded in :attr:`calls` for assertions.
    """

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.calls: list[dict[str, Any]] = []

    async def call_and_readback(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None,
        expected_entity: str,
        expected_value: Any,
        **_kwargs: Any,
    ) -> ReadbackResult:
        """Return the scripted readback outcome for this fake."""
        self.calls.append(
            {
                "domain": domain,
                "service": service,
                "data": data,
                "expected_entity": expected_entity,
                "expected_value": expected_value,
            }
        )
        if self.mode == "raise":
            raise RuntimeError("simulated HA transport failure")
        if self.mode == "mismatch":
            return ReadbackResult(
                applied=False,
                actual_value="stale",
                attempts=3,
                elapsed_s=0.5,
            )
        return ReadbackResult(
            applied=True,
            actual_value=expected_value,
            attempts=1,
            elapsed_s=0.1,
        )


def _session_factory(engine: AsyncEngine):  # type: ignore[no-untyped-def]
    """Build a zero-arg factory yielding an AsyncSession context manager.

    Each yielded session has the ``audit.hmac_key_1`` GUC pre-set, exactly
    as :func:`app.db.get_session` does in production — the executor writes
    audit rows, so its sessions must carry the HMAC key the chain trigger
    needs. (See the WIRING NEEDED note in the Phase 4 report: the
    orchestrator must hand the production ``Executor`` a GUC-setting
    factory rather than the bare ``async_sessionmaker``.)
    """
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def _factory():  # type: ignore[no-untyped-def]
        async with maker() as session:
            await session.execute(
                text("SELECT set_config('audit.hmac_key_1', :v, false)"),
                {"v": os.environ["HMAC_KEY_1"]},
            )
            yield session

    return _factory


async def _enqueue_pending(
    session: AsyncSession,
    *,
    entity: str,
    value: float,
    key: str,
) -> CommandQueueEntry:
    """Insert one pending input_number.set_value command."""
    command = CommandQueueEntry(
        domain="input_number",
        service="set_value",
        target_entity=entity,
        service_data={"entity_id": entity, "value": value},
        expected_value=str(value),
        idempotency_key=key,
        status=CommandStatus.pending,
        enqueued_by="tester",
    )
    session.add(command)
    await session.flush()
    return command


async def _reload(
    session: AsyncSession, command_id: int
) -> CommandQueueEntry:
    """Re-read a command, bypassing the session's stale identity map.

    The executor mutates + commits commands on its *own* sessions; the
    test's ``session`` still holds the pre-mutation instance in its
    identity map, so a plain ``session.get`` would return stale state.
    ``expire_all`` forces a fresh SELECT.
    """
    session.expire_all()
    refreshed = await session.get(CommandQueueEntry, command_id)
    assert refreshed is not None
    return refreshed


async def _seed_effective_targets(
    session: AsyncSession,
    *,
    uid: str,
    room_id: str,
    day_index: int,
    params: dict[str, float],
) -> None:
    """Seed an approved recipe so the effective_target matview is populated."""
    from app.core.effective import refresh_effective_targets  # noqa: PLC0415

    session.add(User(id=uid, display_name=f"User {uid}"))
    await session.flush()
    rev = RecipeRevision(
        room_id=room_id,
        version=1,
        name=f"Recipe {room_id}",
        status=RecipeStatus.approved,
        created_by=uid,
        approved_by=uid,
    )
    session.add(rev)
    await session.flush()
    for param_name, value in params.items():
        session.add(
            RecipeRevisionParam(
                recipe_revision_id=rev.id,
                room_id=room_id,
                day_index=day_index,
                param_name=param_name,
                value=value,
            )
        )
    await session.commit()
    await refresh_effective_targets(session)
    await session.commit()


async def test_run_once_applies_pending_command(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """run_once applies a pending command and audit-rows it."""
    cmd = await _enqueue_pending(
        session, entity="input_number.ex1_temp", value=27.0, key="ex-1"
    )
    await session.commit()
    cmd_id = cmd.id

    executor = Executor(_session_factory(async_engine), FakeHAClient("ok"))
    processed = await executor.run_once()
    assert processed == 1

    refreshed = await _reload(session, cmd_id)
    assert refreshed.status == CommandStatus.applied
    assert refreshed.attempts == 1
    assert refreshed.readback is not None
    assert refreshed.readback["actual"] == "27.0"

    audit = await session.execute(
        select(AuditEvent).where(
            AuditEvent.command_id == cmd_id,
            AuditEvent.event_type == AuditEventType.controlled_adjustment,
        )
    )
    assert audit.scalar_one().event_type == AuditEventType.controlled_adjustment


async def test_run_once_empty_queue_returns_zero(
    async_engine: AsyncEngine,
) -> None:
    """run_once on an empty queue processes nothing."""
    executor = Executor(_session_factory(async_engine), FakeHAClient("ok"))
    assert await executor.run_once() == 0


async def test_failing_command_increments_attempts(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A command whose HA call raises records a failed attempt."""
    cmd = await _enqueue_pending(
        session, entity="input_number.ex_fail", value=20.0, key="ex-fail-1"
    )
    await session.commit()
    cmd_id = cmd.id

    executor = Executor(_session_factory(async_engine), FakeHAClient("raise"))
    await executor.run_once()

    refreshed = await _reload(session, cmd_id)
    assert refreshed.attempts == 1
    assert refreshed.last_error is not None
    # Below the terminal threshold the command returns to pending to retry.
    assert refreshed.status == CommandStatus.pending


async def test_readback_mismatch_is_a_failure(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A readback that never settles counts as a failed attempt."""
    cmd = await _enqueue_pending(
        session, entity="input_number.ex_mismatch", value=21.0, key="ex-mm-1"
    )
    await session.commit()
    cmd_id = cmd.id

    executor = Executor(_session_factory(async_engine), FakeHAClient("mismatch"))
    await executor.run_once()

    refreshed = await _reload(session, cmd_id)
    assert refreshed.attempts == 1
    assert refreshed.status == CommandStatus.pending
    assert "readback mismatch" in (refreshed.last_error or "")


async def test_three_failures_emit_system_warning(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """Three cumulative failed attempts emit a system_warning event."""
    cmd = await _enqueue_pending(
        session, entity="input_number.ex_warn", value=19.0, key="ex-warn-1"
    )
    await session.commit()
    cmd_id = cmd.id

    executor = Executor(_session_factory(async_engine), FakeHAClient("raise"))
    for _ in range(3):
        await executor.run_once()

    refreshed = await _reload(session, cmd_id)
    assert refreshed.attempts == 3

    warnings = await session.execute(
        select(func.count(EventLogEntry.id)).where(
            EventLogEntry.event_type == AuditEventType.system_warning,
            EventLogEntry.payload["command_id"].astext == str(cmd_id),
        )
    )
    assert warnings.scalar_one() >= 1


async def test_five_failures_emit_formal_deviation(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """Five cumulative failed attempts terminally fail + emit formal_deviation."""
    cmd = await _enqueue_pending(
        session, entity="input_number.ex_dev", value=18.0, key="ex-dev-1"
    )
    await session.commit()
    cmd_id = cmd.id

    executor = Executor(_session_factory(async_engine), FakeHAClient("raise"))
    for _ in range(5):
        await executor.run_once()

    refreshed = await _reload(session, cmd_id)
    assert refreshed.attempts == 5
    # Terminal: the command is failed and no longer retried.
    assert refreshed.status == CommandStatus.failed
    assert refreshed.completed_at is not None

    # A 6th drain must not touch the terminally-failed command.
    assert await executor.run_once() == 0

    deviations = await session.execute(
        select(func.count(EventLogEntry.id)).where(
            EventLogEntry.event_type == AuditEventType.formal_deviation,
            EventLogEntry.payload["command_id"].astext == str(cmd_id),
        )
    )
    assert deviations.scalar_one() == 1

    audit = await session.execute(
        select(func.count(AuditEvent.id)).where(
            AuditEvent.command_id == cmd_id,
            AuditEvent.event_type == AuditEventType.formal_deviation,
        )
    )
    assert audit.scalar_one() == 1


async def test_on_lights_on_enqueues_batch(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """on_lights_on enqueues a batch with one command per effective target."""
    await _seed_effective_targets(
        session,
        uid="ex-lights-1",
        room_id="ex-lroom-1",
        day_index=10,
        params={"temp_day": 28.0, "rh_day": 60.0},
    )
    executor = Executor(_session_factory(async_engine), FakeHAClient("ok"))

    batch = await executor.on_lights_on(session, "ex-lroom-1", 10)
    await session.commit()

    assert batch is not None
    rows = await session.execute(
        select(CommandQueueEntry).where(
            CommandQueueEntry.batch_id == batch.id
        )
    )
    entries = rows.scalars().all()
    assert len(entries) == 2
    entities = {e.target_entity for e in entries}
    assert entities == {
        "input_number.ex-lroom-1_setpoint_temp_day",
        "input_number.ex-lroom-1_setpoint_rh_day",
    }


async def test_on_lights_on_is_idempotent(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A duplicated lights-on trigger does not double-enqueue the batch."""
    await _seed_effective_targets(
        session,
        uid="ex-lights-2",
        room_id="ex-lroom-2",
        day_index=12,
        params={"temp_day": 27.0},
    )
    executor = Executor(_session_factory(async_engine), FakeHAClient("ok"))

    first = await executor.on_lights_on(session, "ex-lroom-2", 12)
    await session.commit()
    second = await executor.on_lights_on(session, "ex-lroom-2", 12)
    await session.commit()

    assert first is not None
    assert second is not None
    assert first.id == second.id

    rows = await session.execute(
        select(func.count(CommandQueueEntry.id)).where(
            CommandQueueEntry.batch_id == first.id
        )
    )
    # Exactly one command — not doubled by the second trigger.
    assert rows.scalar_one() == 1


async def test_on_lights_on_no_targets_returns_none(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A room with no effective targets yields no batch."""
    executor = Executor(_session_factory(async_engine), FakeHAClient("ok"))
    assert await executor.on_lights_on(session, "ex-no-room", 1) is None


async def test_handle_day_overflow_clamps_and_warns(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A cycle day past the recipe length clamps + emits a system_warning."""
    executor = Executor(_session_factory(async_engine), FakeHAClient("ok"))

    in_range = await executor.handle_day_overflow(session, "ex-of-room", 40, 84)
    assert in_range == 40

    clamped = await executor.handle_day_overflow(session, "ex-of-room", 99, 84)
    await session.commit()
    assert clamped == 84

    warnings = await session.execute(
        select(func.count(EventLogEntry.id)).where(
            EventLogEntry.event_type == AuditEventType.system_warning,
            EventLogEntry.room_id == "ex-of-room",
        )
    )
    assert warnings.scalar_one() == 1


async def test_lights_on_batch_drains_via_run_once(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """An on_lights_on batch is then applied end-to-end by run_once."""
    await _seed_effective_targets(
        session,
        uid="ex-e2e",
        room_id="ex-e2e-room",
        day_index=5,
        params={"temp_day": 26.0, "vwc_target": 50.0},
    )
    executor = Executor(_session_factory(async_engine), FakeHAClient("ok"))

    batch = await executor.on_lights_on(session, "ex-e2e-room", 5)
    await session.commit()
    assert batch is not None

    processed = await executor.run_once()
    assert processed == 2

    # Commands were enqueued on this session but mutated by the executor's
    # own sessions. Read the status column directly (not via the ORM
    # identity map) so the test sees the executor's committed state.
    statuses = await session.execute(
        select(CommandQueueEntry.status).where(
            CommandQueueEntry.batch_id == batch.id
        )
    )
    assert all(s == CommandStatus.applied for s in statuses.scalars())
