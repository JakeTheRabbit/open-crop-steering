"""Command-queue helpers — enqueue + claim for the outbox pattern.

Every Home Assistant control write lands first as a ``command_queue`` row
(locked decision #5: durable outbox). Producers — the executor's
lights-on handler, SFW approvals, manual actions — call :func:`enqueue_command`
(or :func:`enqueue_batch`); the executor worker claims rows with
:func:`claim_next_pending`, applies them via the HA client, and records
the outcome with :func:`mark_applied` / :func:`mark_failed`.

Idempotency is the load-bearing property: every enqueue carries a stable
``idempotency_key`` and the ``command_queue.idempotency_key`` column is
``UNIQUE``. A duplicated trigger (e.g. a lights switch that bounces) must
not re-enqueue, so :func:`enqueue_command` returns the existing row
instead of inserting a second one.

Claiming uses ``FOR UPDATE SKIP LOCKED`` so two executor instances — or
an executor racing an API restart — never claim the same row; the loser
simply skips to the next pending row.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.command_queue import (
    CommandBatch,
    CommandQueueEntry,
    CommandStatus,
)

log = structlog.get_logger(__name__)


async def enqueue_command(
    session: AsyncSession,
    *,
    domain: str,
    service: str,
    target_entity: str,
    service_data: dict[str, Any],
    expected_value: str | None,
    idempotency_key: str,
    enqueued_by: str,
    reason: str | None = None,
    batch_id: int | None = None,
) -> CommandQueueEntry:
    """Insert one ``pending`` command, idempotently.

    If a ``command_queue`` row already exists with ``idempotency_key``,
    the existing row is returned unchanged — no duplicate is inserted.
    This makes the enqueue safe to call from a trigger that can fire
    more than once.

    Args:
        session: Active async session.
        domain: HA service domain, e.g. ``"input_number"``.
        service: HA service name, e.g. ``"set_value"``.
        target_entity: Entity whose state the readback verifies.
        service_data: ``call_service`` payload (entity_id + parameters).
        expected_value: Value the entity's ``state`` should reach after
            the call; ``None`` skips readback verification.
        idempotency_key: Stable key; the column is ``UNIQUE``.
        enqueued_by: Identifier of the producer (``users.id`` or a worker
            name such as ``"executor"``).
        reason: Optional free-text reason recorded on the row.
        batch_id: Optional :class:`CommandBatch` id this command belongs to.

    Returns:
        The persisted (or pre-existing) :class:`CommandQueueEntry`.
    """
    existing = await _find_by_key(session, idempotency_key)
    if existing is not None:
        log.debug(
            "command_enqueue_idempotent_hit",
            idempotency_key=idempotency_key,
            command_id=existing.id,
        )
        return existing

    command = CommandQueueEntry(
        batch_id=batch_id,
        domain=domain,
        service=service,
        target_entity=target_entity,
        service_data=service_data,
        expected_value=expected_value,
        idempotency_key=idempotency_key,
        status=CommandStatus.pending,
        enqueued_by=enqueued_by,
        reason=reason,
    )
    session.add(command)
    try:
        await session.flush()
    except IntegrityError:
        # A concurrent producer won the race on the UNIQUE key. Roll back
        # to a clean state and return the row the other writer inserted.
        await session.rollback()
        winner = await _find_by_key(session, idempotency_key)
        if winner is None:  # pragma: no cover — IntegrityError implies a row
            raise
        log.debug(
            "command_enqueue_idempotent_race",
            idempotency_key=idempotency_key,
            command_id=winner.id,
        )
        return winner

    log.info(
        "command_enqueued",
        command_id=command.id,
        domain=domain,
        service=service,
        target_entity=target_entity,
        batch_id=batch_id,
    )
    return command


async def enqueue_batch(
    session: AsyncSession,
    *,
    room_id: str,
    purpose: str,
    idempotency_key: str,
    commands: list[dict[str, Any]],
    enqueued_by: str,
) -> CommandBatch:
    """Create a :class:`CommandBatch` and its commands under one key.

    Grouping a multi-setpoint apply under a single batch idempotency key
    means a duplicated trigger does not re-fire half the batch: the batch
    key is checked first, and each command's own key derives from the
    batch key plus its target entity so individual commands are
    independently idempotent too.

    Args:
        session: Active async session.
        room_id: Room the batch applies to.
        purpose: Short batch purpose, e.g. ``"lights_on"``.
        idempotency_key: Stable batch key; the column is ``UNIQUE``.
        commands: One dict per command. Required keys: ``domain``,
            ``service``, ``target_entity``, ``service_data``. Optional:
            ``expected_value``, ``reason``.
        enqueued_by: Identifier of the producer.

    Returns:
        The persisted (or pre-existing) :class:`CommandBatch`.
    """
    existing = await _find_batch_by_key(session, idempotency_key)
    if existing is not None:
        log.debug(
            "command_batch_idempotent_hit",
            idempotency_key=idempotency_key,
            batch_id=existing.id,
        )
        return existing

    batch = CommandBatch(
        room_id=room_id,
        purpose=purpose,
        idempotency_key=idempotency_key,
    )
    session.add(batch)
    try:
        await session.flush()  # assigns batch.id
    except IntegrityError:
        await session.rollback()
        winner = await _find_batch_by_key(session, idempotency_key)
        if winner is None:  # pragma: no cover — IntegrityError implies a row
            raise
        log.debug(
            "command_batch_idempotent_race",
            idempotency_key=idempotency_key,
            batch_id=winner.id,
        )
        return winner

    for spec in commands:
        target_entity = str(spec["target_entity"])
        await enqueue_command(
            session,
            domain=str(spec["domain"]),
            service=str(spec["service"]),
            target_entity=target_entity,
            service_data=dict(spec["service_data"]),
            expected_value=spec.get("expected_value"),
            idempotency_key=f"{idempotency_key}:{target_entity}",
            enqueued_by=enqueued_by,
            reason=spec.get("reason"),
            batch_id=batch.id,
        )

    log.info(
        "command_batch_enqueued",
        batch_id=batch.id,
        room_id=room_id,
        purpose=purpose,
        command_count=len(commands),
    )
    return batch


async def claim_next_pending(session: AsyncSession) -> CommandQueueEntry | None:
    """Claim the oldest ``pending`` command and flip it to ``applying``.

    Uses ``FOR UPDATE SKIP LOCKED``: a concurrent claimer locking the same
    row is skipped over rather than blocked, so two executor instances
    process distinct rows. The claimed row's status moves to ``applying``
    and ``started_at`` is stamped before the function returns, so the
    command is no longer eligible for a second claim.

    Args:
        session: Active async session. The row stays locked until this
            session commits or rolls back.

    Returns:
        The claimed :class:`CommandQueueEntry`, or ``None`` if the queue
        holds no pending commands.
    """
    result = await session.execute(
        select(CommandQueueEntry)
        .where(CommandQueueEntry.status == CommandStatus.pending)
        .order_by(CommandQueueEntry.enqueued_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    command = result.scalar_one_or_none()
    if command is None:
        return None

    command.status = CommandStatus.applying
    command.started_at = dt.datetime.now(dt.UTC)
    await session.flush()

    log.debug("command_claimed", command_id=command.id)
    return command


async def mark_applied(
    session: AsyncSession,
    command: CommandQueueEntry,
    readback: dict[str, Any],
) -> None:
    """Mark a command ``applied`` with its readback evidence.

    Args:
        session: Active async session.
        command: The command being completed.
        readback: Readback evidence (e.g. expected / actual / attempts)
            stored verbatim on the row.
    """
    command.status = CommandStatus.applied
    command.completed_at = dt.datetime.now(dt.UTC)
    command.attempts += 1
    command.readback = readback
    command.last_error = None
    await session.flush()

    log.info(
        "command_applied",
        command_id=command.id,
        attempts=command.attempts,
        target_entity=command.target_entity,
    )


async def mark_failed(
    session: AsyncSession,
    command: CommandQueueEntry,
    error: str,
) -> None:
    """Record one failed attempt against a command.

    This increments ``attempts`` and stores ``last_error`` but leaves the
    status at ``applying`` so the executor's retry/escalation logic owns
    the decision of when to terminally fail the row.

    Args:
        session: Active async session.
        command: The command that failed.
        error: Human-readable failure description.
    """
    command.attempts += 1
    command.last_error = error[:2048]
    await session.flush()

    log.warning(
        "command_attempt_failed",
        command_id=command.id,
        attempts=command.attempts,
        error=error,
    )


async def _find_by_key(
    session: AsyncSession, idempotency_key: str
) -> CommandQueueEntry | None:
    """Look up a command row by its idempotency key."""
    result = await session.execute(
        select(CommandQueueEntry).where(
            CommandQueueEntry.idempotency_key == idempotency_key
        )
    )
    return result.scalar_one_or_none()


async def _find_batch_by_key(
    session: AsyncSession, idempotency_key: str
) -> CommandBatch | None:
    """Look up a batch row by its idempotency key."""
    result = await session.execute(
        select(CommandBatch).where(
            CommandBatch.idempotency_key == idempotency_key
        )
    )
    return result.scalar_one_or_none()
