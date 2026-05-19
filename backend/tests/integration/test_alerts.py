"""Integration tests for :class:`app.workers.alerts.AlertsWorker`.

Exercises severity routing against real Postgres + ``event_log`` rows:
critical events send immediately, warnings send (or are suppressed by a
mute), info events never send, and the daily digest bundles warnings.

The :class:`Notifier` is a :class:`FakeNotifier` recording every send,
so the routing decisions are asserted without a real Telegram bot. The
worker scans not-yet-notified ``event_log`` rows and stamps
``notified_at`` (never the acknowledged_* columns), so the tests seed +
commit on a throwaway session and clear the table between tests.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from app.models.audit_event import AuditEventType
from app.models.event_log import EventLogEntry, EventSeverity
from app.workers.alerts import AlertsWorker, NullNotifier
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def _clean_event_log(
    async_engine: AsyncEngine,
) -> AsyncIterator[None]:
    """Start each test from an empty event_log.

    The worker commits through its own sessions and scans ``event_log``
    globally, so committed rows leak across tests; clear the table on a
    throwaway session beforehand.
    """
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with maker() as cleanup:
        await cleanup.execute(text("DELETE FROM event_log"))
        await cleanup.commit()
    yield


class FakeNotifier:
    """A :class:`app.workers.alerts.Notifier` recording every send.

    Set ``fail=True`` to make :meth:`send` raise, simulating a transport
    failure so the worker's "leave notified_at NULL for retry" path can
    be asserted.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[EventSeverity, str]] = []

    async def send(self, severity: EventSeverity, text: str) -> None:
        if self.fail:
            raise RuntimeError("simulated notifier transport failure")
        self.sent.append((severity, text))


def _session_factory(engine: AsyncEngine):  # type: ignore[no-untyped-def]
    """Zero-arg factory yielding a GUC-set AsyncSession context manager.

    Mirrors :func:`app.db.open_session`: the alerts worker may not write
    audit rows itself, but keeping the factory shape identical to the
    executor's keeps the wiring uniform.
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


async def _seed_event(
    engine: AsyncEngine,
    *,
    severity: EventSeverity,
    room_id: str | None,
    summary: str,
    event_type: AuditEventType,
    occurred_at: dt.datetime | None = None,
) -> int:
    """Insert one committed event_log row; return its id."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        entry = EventLogEntry(
            event_type=event_type,
            severity=severity,
            room_id=room_id,
            summary=summary,
            payload={},
            reason_codes=[],
        )
        if occurred_at is not None:
            entry.occurred_at = occurred_at
        session.add(entry)
        await session.flush()
        event_id = entry.id
        await session.commit()
    return event_id


async def _reload(
    session: AsyncSession, event_id: int
) -> EventLogEntry:
    """Re-read an event row, bypassing the identity map."""
    session.expire_all()
    row = await session.get(EventLogEntry, event_id)
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# severity routing
# ---------------------------------------------------------------------------


async def test_critical_event_sent_immediately(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A critical event is sent on the first run_once and marked notified."""
    event_id = await _seed_event(
        async_engine,
        severity=EventSeverity.critical,
        room_id="al-room-1",
        summary="condensation risk",
        event_type=AuditEventType.critical_incident,
    )
    notifier = FakeNotifier()
    worker = AlertsWorker(_session_factory(async_engine), notifier)

    handled = await worker.run_once()
    assert handled == 1
    assert len(notifier.sent) == 1
    severity, text = notifier.sent[0]
    assert severity is EventSeverity.critical
    assert "condensation risk" in text

    row = await _reload(session, event_id)
    assert row.notified_at is not None
    # Regression guard: the worker must NOT touch the human-ack columns.
    # Those are the QAP deviation gate — auto-acking them would silently
    # disable rollout-advance blocking.
    assert row.acknowledged_at is None
    assert row.acknowledged_by is None


async def test_warning_event_sent_when_not_muted(
    async_engine: AsyncEngine,
) -> None:
    """A warning event sends immediately when its room is not muted."""
    await _seed_event(
        async_engine,
        severity=EventSeverity.warning,
        room_id="al-room-2",
        summary="AC saturated",
        event_type=AuditEventType.system_warning,
    )
    notifier = FakeNotifier()
    worker = AlertsWorker(_session_factory(async_engine), notifier)

    await worker.run_once()
    assert len(notifier.sent) == 1
    assert notifier.sent[0][0] is EventSeverity.warning


async def test_info_event_not_sent(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """An info event is dashboard-only — handled but never sent."""
    event_id = await _seed_event(
        async_engine,
        severity=EventSeverity.info,
        room_id="al-room-3",
        summary="room recovered",
        event_type=AuditEventType.info_event,
    )
    notifier = FakeNotifier()
    worker = AlertsWorker(_session_factory(async_engine), notifier)

    handled = await worker.run_once()
    assert handled == 1
    assert notifier.sent == []

    # Still marked notified so it does not re-process every tick.
    row = await _reload(session, event_id)
    assert row.notified_at is not None


async def test_already_notified_event_is_skipped(
    async_engine: AsyncEngine,
) -> None:
    """A run_once over already-notified rows handles nothing."""
    await _seed_event(
        async_engine,
        severity=EventSeverity.critical,
        room_id="al-room-4",
        summary="first",
        event_type=AuditEventType.critical_incident,
    )
    notifier = FakeNotifier()
    worker = AlertsWorker(_session_factory(async_engine), notifier)

    assert await worker.run_once() == 1
    # Second drain: the row is already notified, nothing left to do.
    assert await worker.run_once() == 0
    assert len(notifier.sent) == 1


# ---------------------------------------------------------------------------
# mute support
# ---------------------------------------------------------------------------


async def test_mute_suppresses_warning(
    async_engine: AsyncEngine,
) -> None:
    """A muted room's warning event is suppressed (handled, not sent)."""
    await _seed_event(
        async_engine,
        severity=EventSeverity.warning,
        room_id="al-muted",
        summary="dehu saturated",
        event_type=AuditEventType.system_warning,
    )

    async def _mute_checker(room_id: str | None) -> bool:
        return room_id == "al-muted"

    notifier = FakeNotifier()
    worker = AlertsWorker(
        _session_factory(async_engine), notifier, mute_checker=_mute_checker
    )

    handled = await worker.run_once()
    assert handled == 1
    assert notifier.sent == []  # suppressed


async def test_mute_does_not_suppress_critical(
    async_engine: AsyncEngine,
) -> None:
    """A muted room's critical event is still sent — mute is bypassed."""
    await _seed_event(
        async_engine,
        severity=EventSeverity.critical,
        room_id="al-muted-crit",
        summary="irrigation failed",
        event_type=AuditEventType.critical_incident,
    )

    async def _mute_checker(_room_id: str | None) -> bool:
        return True  # everything muted

    notifier = FakeNotifier()
    worker = AlertsWorker(
        _session_factory(async_engine), notifier, mute_checker=_mute_checker
    )

    await worker.run_once()
    assert len(notifier.sent) == 1
    assert notifier.sent[0][0] is EventSeverity.critical


# ---------------------------------------------------------------------------
# digest
# ---------------------------------------------------------------------------


async def test_digest_bundles_warnings(
    async_engine: AsyncEngine,
) -> None:
    """send_digest bundles every recent warning into one message."""
    for i in range(3):
        await _seed_event(
            async_engine,
            severity=EventSeverity.warning,
            room_id=f"al-dg-{i}",
            summary=f"warning {i}",
            event_type=AuditEventType.system_warning,
        )
    notifier = FakeNotifier()
    worker = AlertsWorker(_session_factory(async_engine), notifier)

    count = await worker.send_digest()
    assert count == 3
    # One single bundled message, not three.
    assert len(notifier.sent) == 1
    severity, text = notifier.sent[0]
    assert severity is EventSeverity.warning
    assert "3 event(s)" in text
    for i in range(3):
        assert f"warning {i}" in text


async def test_digest_excludes_critical_and_info(
    async_engine: AsyncEngine,
) -> None:
    """The digest covers warnings only — not critical or info events."""
    await _seed_event(
        async_engine,
        severity=EventSeverity.critical,
        room_id="al-dg-crit",
        summary="critical event",
        event_type=AuditEventType.critical_incident,
    )
    await _seed_event(
        async_engine,
        severity=EventSeverity.info,
        room_id="al-dg-info",
        summary="info event",
        event_type=AuditEventType.info_event,
    )
    await _seed_event(
        async_engine,
        severity=EventSeverity.warning,
        room_id="al-dg-warn",
        summary="warning event",
        event_type=AuditEventType.system_warning,
    )
    notifier = FakeNotifier()
    worker = AlertsWorker(_session_factory(async_engine), notifier)

    count = await worker.send_digest()
    assert count == 1
    assert "warning event" in notifier.sent[0][1]
    assert "critical event" not in notifier.sent[0][1]
    assert "info event" not in notifier.sent[0][1]


async def test_digest_empty_sends_nothing(
    async_engine: AsyncEngine,
) -> None:
    """An empty warning window sends no digest message."""
    notifier = FakeNotifier()
    worker = AlertsWorker(_session_factory(async_engine), notifier)
    assert await worker.send_digest() == 0
    assert notifier.sent == []


async def test_digest_respects_since_window(
    async_engine: AsyncEngine,
) -> None:
    """A warning older than the ``since`` window is excluded from the digest."""
    now = dt.datetime.now(dt.UTC)
    await _seed_event(
        async_engine,
        severity=EventSeverity.warning,
        room_id="al-dg-old",
        summary="stale warning",
        event_type=AuditEventType.system_warning,
        occurred_at=now - dt.timedelta(days=3),
    )
    await _seed_event(
        async_engine,
        severity=EventSeverity.warning,
        room_id="al-dg-fresh",
        summary="fresh warning",
        event_type=AuditEventType.system_warning,
        occurred_at=now - dt.timedelta(hours=1),
    )
    notifier = FakeNotifier()
    worker = AlertsWorker(_session_factory(async_engine), notifier)

    count = await worker.send_digest(since=now - dt.timedelta(days=1))
    assert count == 1
    assert "fresh warning" in notifier.sent[0][1]
    assert "stale warning" not in notifier.sent[0][1]


# ---------------------------------------------------------------------------
# transport-failure handling
# ---------------------------------------------------------------------------


async def test_send_failure_leaves_event_not_notified(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A notifier transport failure leaves notified_at NULL for retry."""
    event_id = await _seed_event(
        async_engine,
        severity=EventSeverity.critical,
        room_id="al-fail",
        summary="will fail to send",
        event_type=AuditEventType.critical_incident,
    )
    worker = AlertsWorker(
        _session_factory(async_engine), FakeNotifier(fail=True)
    )

    handled = await worker.run_once()
    assert handled == 0  # nothing successfully handled

    row = await _reload(session, event_id)
    assert row.notified_at is None  # left for a later retry

    # A later run with a working notifier picks it back up.
    notifier = FakeNotifier()
    worker_ok = AlertsWorker(_session_factory(async_engine), notifier)
    assert await worker_ok.run_once() == 1
    assert len(notifier.sent) == 1


# ---------------------------------------------------------------------------
# notifier implementations
# ---------------------------------------------------------------------------


async def test_null_notifier_drops_silently(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """NullNotifier handles events without sending (and without raising)."""
    event_id = await _seed_event(
        async_engine,
        severity=EventSeverity.critical,
        room_id="al-null",
        summary="dropped",
        event_type=AuditEventType.critical_incident,
    )
    worker = AlertsWorker(_session_factory(async_engine), NullNotifier())

    handled = await worker.run_once()
    assert handled == 1  # handled — the NullNotifier "send" succeeds

    row = await _reload(session, event_id)
    assert row.notified_at is not None
