"""Executor worker — drains the command queue into Home Assistant.

This worker is the only thing that turns ``command_queue`` rows into real
HA service calls (locked decision #5). One instance is active at a time,
guarded by the ``executor_consume`` Postgres advisory lock.

For each pending command the worker:

1. Claims it (``FOR UPDATE SKIP LOCKED``) — see
   :func:`app.core.command_queue.claim_next_pending`.
2. Calls :meth:`HAClient.call_and_readback`, which issues the service
   call then polls the target entity until it reports the expected value.
3. On a verified readback → marks the command ``applied`` and writes a
   ``controlled_adjustment`` audit row carrying the readback evidence.
4. On failure → records the attempt and applies the retry / escalation
   policy below.

**Retry + escalation.** Transient failures are retried; a per-command
exponential backoff (``run_forever`` paces drains) bounds the rate.
Escalation is driven by the *cumulative* ``attempts`` counter on the row:

* after **3** failed attempts → emit a ``system_warning`` event.
* after **5** failed attempts → emit a ``formal_deviation`` event + audit
  row and set the command ``failed`` terminally (it is no longer retried).

The ``HAClient`` is injected so tests can pass a fake double instead of
talking to a real Home Assistant.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_audit
from app.core.command_queue import (
    claim_next_pending,
    enqueue_batch,
    mark_applied,
    mark_failed,
)
from app.core.effective import effective_targets_for_day
from app.core.locks import LockBusyError, advisory_lock
from app.ha_client import HAClient
from app.models.audit_event import AuditEventType
from app.models.command_queue import CommandBatch, CommandQueueEntry, CommandStatus
from app.models.effective_target import EffectiveTarget
from app.models.event_log import EventLogEntry, EventSeverity

log = structlog.get_logger(__name__)

# Advisory-lock name — only one executor drains the queue at a time.
_LOCK_NAME = "executor_consume"

# Escalation thresholds, keyed off the cumulative ``attempts`` counter.
_WARN_AFTER_ATTEMPTS = 3
_FAIL_AFTER_ATTEMPTS = 5

# Exponential-backoff bounds for transient retries (seconds).
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 60.0

# The worker is its own actor in the audit / enqueue trail.
_ACTOR = "executor"

# Type alias: a zero-arg callable returning an AsyncSession context manager.
SessionFactory = Callable[[], Any]


def backoff_delay(attempts: int) -> float:
    """Return the exponential-backoff delay for a given attempt count.

    Args:
        attempts: Cumulative failed-attempt count on the command.

    Returns:
        Seconds to wait before the next retry, ``2 ** (attempts - 1)``
        base seconds, capped at :data:`_BACKOFF_CAP`.
    """
    if attempts < 1:
        return 0.0
    return min(_BACKOFF_BASE * (2 ** (attempts - 1)), _BACKOFF_CAP)


class Executor:
    """Command-queue consumer that applies commands to Home Assistant.

    Args:
        session_factory: Zero-arg callable yielding an ``AsyncSession``
            async context manager (e.g. ``app.db.get_session_factory()``).
        ha_client: Home Assistant client used for service calls +
            readback. Injected so tests can substitute a fake.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        ha_client: HAClient,
    ) -> None:
        self._session_factory = session_factory
        self._ha = ha_client
        self._stopped = asyncio.Event()

    # -- queue draining ---------------------------------------------------

    async def run_once(self) -> int:
        """Drain every currently-pending command once.

        Acquires the ``executor_consume`` advisory lock for the whole
        drain; if another executor holds it this returns ``0`` without
        doing work. Each command is claimed, applied, and committed in
        turn so a crash mid-drain leaves completed commands durable.

        Each command gets at most one HA attempt per drain: a command
        that fails non-terminally returns to ``pending`` and is retried
        on the *next* :meth:`run_once`, so the ``poll_interval`` of
        :meth:`run_forever` paces the exponential backoff between retries.

        Returns:
            The number of commands processed (applied or failed).
        """
        processed = 0
        attempted: set[int] = set()
        async with self._session_factory() as lock_session:
            try:
                async with advisory_lock(lock_session, _LOCK_NAME):
                    while True:
                        command_id = await self._process_one(attempted)
                        if command_id is None:
                            break
                        attempted.add(command_id)
                        processed += 1
            except LockBusyError:
                log.debug("executor_lock_busy_skip")
                return 0

        if processed:
            log.info("executor_run_once_complete", processed=processed)
        return processed

    async def _process_one(self, attempted: set[int]) -> int | None:
        """Claim and apply a single command in its own transaction.

        Args:
            attempted: Command ids already handled in the current drain;
                a command in this set is skipped so a failed-then-retried
                row is not re-attempted within the same drain.

        Returns:
            The processed command id, or ``None`` if the queue holds no
            pending command not already attempted this drain.
        """
        async with self._session_factory() as session:
            command = await claim_next_pending(session)
            if command is None:
                return None
            if command.id in attempted:
                # Already attempted this drain — release the claim so the
                # next run_once re-claims it, and stop draining.
                command.status = CommandStatus.pending
                command.started_at = None
                await session.commit()
                return None
            command_id = command.id
            await self._apply(session, command)
            await session.commit()
        return command_id

    async def _apply(
        self, session: AsyncSession, command: CommandQueueEntry
    ) -> None:
        """Apply one claimed command and record the outcome.

        On a verified readback the command is marked ``applied`` and a
        ``controlled_adjustment`` audit row is written. On any failure the
        attempt is recorded and :meth:`_handle_failure` decides whether to
        retry, warn, or terminally fail.
        """
        try:
            readback = await self._ha.call_and_readback(
                command.domain,
                command.service,
                command.service_data,
                command.target_entity,
                command.expected_value,
            )
        except Exception as exc:  # any HA client error is a failed attempt
            await self._handle_failure(
                session, command, f"{type(exc).__name__}: {exc}"
            )
            return

        if not readback.applied:
            await self._handle_failure(
                session,
                command,
                (
                    f"readback mismatch: expected {command.expected_value!r}, "
                    f"got {readback.actual_value!r} after {readback.attempts} polls"
                ),
            )
            return

        readback_evidence: dict[str, Any] = {
            "expected": command.expected_value,
            "actual": readback.actual_value,
            "attempts": readback.attempts,
            "elapsed_s": round(readback.elapsed_s, 3),
        }
        await mark_applied(session, command, readback_evidence)
        await self._maybe_complete_batch(session, command)

        await log_audit(
            session,
            event_type=AuditEventType.controlled_adjustment,
            actor_id=_ACTOR,
            summary=(
                f"Applied {command.domain}.{command.service} -> "
                f"{command.target_entity} = {readback.actual_value}"
            ),
            command_id=command.id,
            params={
                "domain": command.domain,
                "service": command.service,
                "target_entity": command.target_entity,
                "service_data": command.service_data,
                "readback": readback_evidence,
            },
        )

    async def _handle_failure(
        self, session: AsyncSession, command: CommandQueueEntry, error: str
    ) -> None:
        """Record a failed attempt and apply the escalation policy.

        After :data:`_WARN_AFTER_ATTEMPTS` cumulative attempts a
        ``system_warning`` event is logged; after
        :data:`_FAIL_AFTER_ATTEMPTS` a ``formal_deviation`` event + audit
        row is written and the command is set ``failed`` terminally.
        """
        await mark_failed(session, command, error)
        attempts = command.attempts

        if attempts >= _FAIL_AFTER_ATTEMPTS:
            command.status = CommandStatus.failed
            command.completed_at = dt.datetime.now(dt.UTC)
            await session.flush()

            audit = await log_audit(
                session,
                event_type=AuditEventType.formal_deviation,
                actor_id=_ACTOR,
                summary=(
                    f"Command {command.id} failed terminally after "
                    f"{attempts} attempts: {error}"
                ),
                command_id=command.id,
                reason_codes=["command_apply_failed"],
                params={
                    "target_entity": command.target_entity,
                    "attempts": attempts,
                    "last_error": error,
                },
            )
            session.add(
                EventLogEntry(
                    event_type=AuditEventType.formal_deviation,
                    severity=EventSeverity.critical,
                    summary=(
                        f"Command to {command.target_entity} failed "
                        f"terminally after {attempts} attempts"
                    ),
                    payload={
                        "command_id": command.id,
                        "attempts": attempts,
                        "last_error": error,
                    },
                    reason_codes=["command_apply_failed"],
                    audit_event_id=audit.id,
                )
            )
            await session.flush()
            log.error(
                "executor_command_failed_terminal",
                command_id=command.id,
                attempts=attempts,
            )
        elif attempts >= _WARN_AFTER_ATTEMPTS:
            session.add(
                EventLogEntry(
                    event_type=AuditEventType.system_warning,
                    severity=EventSeverity.warning,
                    summary=(
                        f"Command to {command.target_entity} has failed "
                        f"{attempts} times; still retrying"
                    ),
                    payload={
                        "command_id": command.id,
                        "attempts": attempts,
                        "last_error": error,
                    },
                    reason_codes=["command_apply_failed"],
                )
            )
            await session.flush()
            log.warning(
                "executor_command_failing",
                command_id=command.id,
                attempts=attempts,
            )

        # Below the terminal threshold the row is left ``applying`` so a
        # later drain re-claims it (claim_next_pending only takes pending).
        if command.status == CommandStatus.applying:
            command.status = CommandStatus.pending
            await session.flush()

    async def _maybe_complete_batch(
        self, session: AsyncSession, command: CommandQueueEntry
    ) -> None:
        """Stamp ``completed_at`` on the batch once all its commands settle."""
        if command.batch_id is None:
            return
        batch = await session.get(CommandBatch, command.batch_id)
        if batch is None or batch.completed_at is not None:
            return
        # Reload sibling commands to test for any still-open work.
        await session.refresh(batch, attribute_names=["commands"])
        open_states = {CommandStatus.pending, CommandStatus.applying}
        if any(c.status in open_states for c in batch.commands):
            return
        batch.completed_at = dt.datetime.now(dt.UTC)
        await session.flush()

    # -- run loop ---------------------------------------------------------

    async def run_forever(self, poll_interval: float) -> None:
        """Loop :meth:`run_once`, sleeping ``poll_interval`` between drains.

        Cancellation-safe: an ``asyncio.CancelledError`` (or a
        :meth:`stop` call) breaks the loop cleanly.

        Args:
            poll_interval: Seconds to sleep between drains.
        """
        log.info("executor_started", poll_interval=poll_interval)
        try:
            while not self._stopped.is_set():
                try:
                    await self.run_once()
                except Exception:  # never let one drain kill the loop
                    log.exception("executor_run_once_error")
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(), timeout=poll_interval
                    )
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            log.info("executor_cancelled")
            raise
        finally:
            log.info("executor_stopped")

    def stop(self) -> None:
        """Signal :meth:`run_forever` to exit after the current drain."""
        self._stopped.set()

    # -- lights-on apply --------------------------------------------------

    async def on_lights_on(
        self,
        session: AsyncSession,
        room_id: str,
        cycle_day: int,
    ) -> CommandBatch | None:
        """Enqueue a setpoint-apply batch for a room's lights-on event.

        Fetches today's effective targets and enqueues one command per
        setpoint, writing each value to its HA helper entity. The batch
        idempotency key is ``"{room_id}-lights-on-{date}"`` so a lights
        switch that bounces — firing the trigger twice — does NOT
        re-enqueue the batch.

        Args:
            session: Active async session.
            room_id: Room whose lights just turned on.
            cycle_day: Cultivation cycle day to apply.

        Returns:
            The enqueued (or pre-existing) :class:`CommandBatch`, or
            ``None`` if the room has no effective targets for that day.
        """
        targets = await effective_targets_for_day(session, room_id, cycle_day)
        if not targets:
            log.warning(
                "lights_on_no_targets", room_id=room_id, cycle_day=cycle_day
            )
            return None

        today = dt.datetime.now(dt.UTC).date().isoformat()
        idempotency_key = f"{room_id}-lights-on-{today}"

        commands = [
            self._setpoint_command(room_id, cycle_day, target)
            for target in targets.values()
        ]
        batch = await enqueue_batch(
            session,
            room_id=room_id,
            purpose="lights_on",
            idempotency_key=idempotency_key,
            commands=commands,
            enqueued_by=_ACTOR,
        )
        log.info(
            "lights_on_batch_enqueued",
            room_id=room_id,
            cycle_day=cycle_day,
            batch_id=batch.id,
            setpoint_count=len(commands),
        )
        return batch

    @staticmethod
    def _setpoint_command(
        room_id: str, cycle_day: int, target: EffectiveTarget
    ) -> dict[str, Any]:
        """Build one ``enqueue_batch`` command dict for an effective target.

        The HA helper entity is derived by convention as
        ``input_number.{room_id}_setpoint_{param_name}``.
        """
        entity = f"input_number.{room_id}_setpoint_{target.param_name}"
        value = float(target.value)
        return {
            "domain": "input_number",
            "service": "set_value",
            "target_entity": entity,
            "service_data": {"entity_id": entity, "value": value},
            "expected_value": str(value),
            "reason": f"lights-on apply day {cycle_day} {target.param_name}",
        }

    async def handle_day_overflow(
        self,
        session: AsyncSession,
        room_id: str,
        cycle_day: int,
        cycle_day_count: int,
    ) -> int:
        """Clamp a cycle day that runs past the end of the recipe.

        When a cultivation cycle has not been restarted, ``cycle_day`` can
        exceed the recipe's ``cycle_day_count``. Rather than fail, the day
        is clamped to the last recipe day and a ``system_warning`` event
        is logged so an operator can start a fresh cycle.

        Args:
            session: Active async session.
            room_id: Room being evaluated.
            cycle_day: The (possibly out-of-range) cycle day.
            cycle_day_count: The recipe's day count.

        Returns:
            The clamped cycle day — ``cycle_day`` itself when in range,
            otherwise ``cycle_day_count``.
        """
        if cycle_day <= cycle_day_count:
            return cycle_day

        session.add(
            EventLogEntry(
                event_type=AuditEventType.system_warning,
                severity=EventSeverity.warning,
                room_id=room_id,
                summary=(
                    f"Cycle day {cycle_day} exceeds recipe length "
                    f"{cycle_day_count}; clamped to last day. Start a new cycle."
                ),
                payload={
                    "cycle_day": cycle_day,
                    "cycle_day_count": cycle_day_count,
                    "clamped_to": cycle_day_count,
                },
                reason_codes=["cycle_day_overflow"],
            )
        )
        await session.flush()
        log.warning(
            "cycle_day_overflow",
            room_id=room_id,
            cycle_day=cycle_day,
            cycle_day_count=cycle_day_count,
        )
        return cycle_day_count

    # -- WS wiring (thin; logic lives in on_lights_on) --------------------

    async def run_lights_watcher(
        self,
        room_switch_map: dict[str, str],
        cycle_day_for: Callable[[str], int],
    ) -> None:
        """Subscribe to HA state changes and fire :meth:`on_lights_on`.

        This is deliberately thin — all testable logic lives in
        :meth:`on_lights_on`. It watches ``state_changed`` events; when a
        mapped lights switch transitions to ``on`` it enqueues that room's
        lights-on batch.

        Args:
            room_switch_map: Maps ``room_id`` to the lights switch
                ``entity_id`` to watch.
            cycle_day_for: Callable returning the current cycle day for a
                room id.
        """
        switch_to_room = {v: k for k, v in room_switch_map.items()}
        log.info("lights_watcher_started", switches=list(switch_to_room))

        async for event in self._ha.subscribe_events("state_changed"):
            if self._stopped.is_set():
                break
            data = event.get("data", {})
            entity_id = data.get("entity_id")
            room_id = switch_to_room.get(entity_id)
            if room_id is None:
                continue
            new_state = (data.get("new_state") or {}).get("state")
            old_state = (data.get("old_state") or {}).get("state")
            if new_state == "on" and old_state != "on":
                async with self._session_factory() as session:
                    await self.on_lights_on(
                        session, room_id, cycle_day_for(room_id)
                    )
                    await session.commit()
