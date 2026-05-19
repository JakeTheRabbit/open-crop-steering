"""``worker-alerts`` — severity-routed notifications + daily digest.

Phase 6's notification surface. The deterministic monitors
(:mod:`app.core.state_machine`, :mod:`app.core.equipment`) write rows to
``event_log``; this worker turns the *not-yet-notified* ones into
notifications, routed by severity per plan locked decision #18:

* **critical** — sent immediately, every time, **bypassing** any mute or
  do-not-disturb. A critical alert that gets swallowed is a safety
  problem.
* **warning** — accumulated and bundled into one **daily digest**
  message (:meth:`AlertsWorker.send_digest`). One digest beats N pings.
* **info** — dashboard-only. Never sent anywhere.

Coordination: the dispatch scan runs under the ``alerts_dispatch``
Postgres advisory lock so a duplicated worker process does not
double-send (plan locked decision #3).

Muting
------
A room can be muted (UI action — Phase 8/10 owns the persistence; this
worker takes a :class:`MuteChecker` so the policy is injected). A muted
room suppresses **warning** and **info** alerts; **critical** alerts are
*never* suppressed.

Notifier abstraction
--------------------
:class:`Notifier` is the send abstraction. Three implementations:

* :class:`TelegramNotifier` — thin ``python-telegram-bot`` wrapper.
* :class:`NullNotifier` — drops everything (no Telegram configured).
* tests use a ``FakeNotifier`` that records calls.

Dispatch marks a row ``notified_at`` only after the notifier returns
without raising — a transport failure leaves ``notified_at`` NULL so a
later run retries it. ``notified_at`` is the worker's own marker and is
deliberately separate from ``acknowledged_at`` (a human QAP ack, which
the rollout gate reads): the worker never writes the acknowledged_*
columns.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog
from sqlalchemy import select

from app.core.locks import LockBusyError, advisory_lock
from app.models.event_log import EventLogEntry, EventSeverity

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

#: Advisory-lock name — only one alerts worker dispatches at a time.
_LOCK_NAME = "alerts_dispatch"


#: Per-severity Telegram message prefix. ASCII-only apart from the
#: critical siren (an escape sequence) so the file stays free of
#: ambiguous-unicode lint findings; critical alerts get the emoji
#: because the visual punch matters in a busy chat.
_TELEGRAM_PREFIX: dict[EventSeverity, str] = {
    EventSeverity.critical: "\U0001f6a8 CRITICAL",
    EventSeverity.warning: "WARNING",
    EventSeverity.info: "INFO",
}

#: Zero-arg callable yielding an ``AsyncSession`` async context manager
#: (e.g. ``app.db.open_session``).
SessionFactory = Callable[[], "AbstractAsyncContextManager[AsyncSession]"]

#: ``room_id -> bool`` — ``True`` when the room is muted. ``room_id`` is
#: ``None`` for room-agnostic events (those are never considered muted).
MuteChecker = Callable[[str | None], Awaitable[bool]]


@runtime_checkable
class Notifier(Protocol):
    """Send abstraction for outbound alerts.

    Implementations must not raise for an ordinary "message delivered"
    outcome; a raise signals a transport failure and leaves the
    triggering event with ``notified_at`` NULL for retry.
    """

    async def send(self, severity: EventSeverity, text: str) -> None:
        """Deliver one message at the given severity."""
        ...


class NullNotifier:
    """A :class:`Notifier` that silently drops every message.

    The default when no Telegram bot is configured — the dashboard still
    shows events; nothing is pushed.
    """

    async def send(self, severity: EventSeverity, text: str) -> None:
        """Log at debug and discard the message."""
        log.debug("null_notifier_drop", severity=severity.value, text=text)


class TelegramNotifier:
    """Thin ``python-telegram-bot`` wrapper implementing :class:`Notifier`.

    Construct with a bot token + chat id (from
    :class:`~app.config.Settings`). The send is a single
    ``Bot.send_message`` await — retry / batching policy lives in
    :class:`AlertsWorker`, not here.

    Args:
        bot_token: Telegram bot token.
        chat_id: Destination chat id. May be an ``int`` or a ``@channel``
            string.
        bot: Pre-built bot instance (injection point for tests); when
            omitted a :class:`telegram.Bot` is created from ``bot_token``.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: int | str,
        *,
        bot: object | None = None,
    ) -> None:
        self._chat_id = chat_id
        if bot is not None:
            self._bot = bot
        else:
            from telegram import Bot  # noqa: PLC0415 — optional import path

            self._bot = Bot(token=bot_token)

    async def send(self, severity: EventSeverity, text: str) -> None:
        """Send ``text`` to the configured chat.

        ``severity`` is prefixed onto the message so the tier is visible
        in the chat itself; ``python-telegram-bot`` does the transport.
        """
        prefix = _TELEGRAM_PREFIX[severity]
        await self._bot.send_message(  # type: ignore[attr-defined]
            chat_id=self._chat_id, text=f"{prefix}\n{text}"
        )


async def _never_muted(_room_id: str | None) -> bool:
    """Default :class:`MuteChecker` — nothing is ever muted."""
    return False


def _format_event(entry: EventLogEntry) -> str:
    """Render one event-log row as a single notification line."""
    room = entry.room_id or "system"
    when = entry.occurred_at.strftime("%Y-%m-%d %H:%M UTC")
    line = f"[{room}] {entry.summary} ({when})"
    if entry.reason_codes:
        line += f" — {', '.join(entry.reason_codes)}"
    return line


class AlertsWorker:
    """Scans ``event_log`` and dispatches alerts by severity.

    Args:
        session_factory: Zero-arg callable yielding an ``AsyncSession``
            async context manager (typically :func:`app.db.open_session`).
        notifier: The :class:`Notifier` outbound messages go through.
        mute_checker: Async predicate ``room_id -> muted?``; defaults to
            "never muted". A muted room suppresses warning + info alerts;
            critical alerts ignore the mute.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        notifier: Notifier,
        *,
        mute_checker: MuteChecker | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._notifier = notifier
        self._is_muted: MuteChecker = mute_checker or _never_muted

    # -- dispatch ---------------------------------------------------------

    async def run_once(self) -> int:
        """Dispatch every not-yet-notified event once.

        Held under the ``alerts_dispatch`` advisory lock; if another
        worker holds it this returns ``0``. For each not-yet-notified row:

        * **critical** — sent immediately (mute ignored), then marked
          ``notified_at``.
        * **warning** — sent immediately *unless* its room is muted;
          marked ``notified_at`` once handled (a muted warning is marked
          notified without sending, so it does not pile up — it still
          shows on the dashboard).
        * **info** — marked ``notified_at`` without sending (dashboard-only).

        A row whose send raises is left with ``notified_at`` NULL for a
        later retry.

        Returns:
            The number of event rows handled (sent or suppressed).
        """
        handled = 0
        async with self._session_factory() as session:
            try:
                async with advisory_lock(session, _LOCK_NAME):
                    handled = await self._dispatch_pending(session)
                    await session.commit()
            except LockBusyError:
                log.debug("alerts_lock_busy_skip")
                return 0

        if handled:
            log.info("alerts_run_once_complete", handled=handled)
        return handled

    async def _dispatch_pending(self, session: AsyncSession) -> int:
        """Send / suppress every not-yet-notified row; return the count."""
        rows = await self._pending_dispatch(session)
        handled = 0
        for entry in rows:
            try:
                sent = await self._handle_one(entry)
            except Exception:  # transport failure — leave for a later retry
                log.exception(
                    "alert_dispatch_failed", event_id=entry.id
                )
                continue
            self._mark_dispatched(entry)
            handled += 1
            log.debug(
                "alert_handled",
                event_id=entry.id,
                severity=entry.severity.value,
                sent=sent,
            )
        return handled

    async def _handle_one(self, entry: EventLogEntry) -> bool:
        """Route a single event by severity. Returns whether it was sent.

        Raises whatever the notifier raises, so the caller can leave
        ``notified_at`` NULL on a transport failure.
        """
        if entry.severity is EventSeverity.info:
            return False  # dashboard-only

        if entry.severity is EventSeverity.critical:
            await self._notifier.send(entry.severity, _format_event(entry))
            return True

        # warning — suppressed when the room is muted.
        if await self._is_muted(entry.room_id):
            log.info("alert_suppressed_muted", event_id=entry.id)
            return False
        await self._notifier.send(entry.severity, _format_event(entry))
        return True

    # -- digest -----------------------------------------------------------

    async def send_digest(self, *, since: dt.datetime | None = None) -> int:
        """Bundle the day's warning events into one digest message.

        Critical events are *not* part of the digest (they were sent
        immediately by :meth:`run_once`); info events are dashboard-only.
        The digest gathers every ``warning`` row in the window — including
        ones suppressed by a mute at dispatch time, so a muted room's
        operator still gets a once-a-day summary — and sends a single
        message.

        Args:
            since: Window start; defaults to 24 hours before now (UTC).

        Returns:
            The number of warning events bundled (``0`` sends nothing).
        """
        window_start = since or (
            dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
        )
        async with self._session_factory() as session:
            result = await session.execute(
                select(EventLogEntry)
                .where(
                    EventLogEntry.severity == EventSeverity.warning,
                    EventLogEntry.occurred_at >= window_start,
                )
                .order_by(EventLogEntry.occurred_at)
            )
            warnings = list(result.scalars().all())

        if not warnings:
            log.info("alerts_digest_empty")
            return 0

        lines = [f"Daily warning digest — {len(warnings)} event(s):"]
        lines.extend(f"  - {_format_event(w)}" for w in warnings)
        await self._notifier.send(EventSeverity.warning, "\n".join(lines))
        log.info("alerts_digest_sent", count=len(warnings))
        return len(warnings)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    async def _pending_dispatch(
        session: AsyncSession,
    ) -> Sequence[EventLogEntry]:
        """Return rows not yet notified by this worker, oldest first."""
        result = await session.execute(
            select(EventLogEntry)
            .where(EventLogEntry.notified_at.is_(None))
            .order_by(EventLogEntry.occurred_at)
        )
        return list(result.scalars().all())

    @staticmethod
    def _mark_dispatched(entry: EventLogEntry) -> None:
        """Stamp a row as notified by this worker.

        Writes ONLY ``notified_at`` — never the acknowledged_* columns,
        which are reserved for a human QAP acknowledgement (the
        rollout-advance gate reads ``acknowledged_at``).
        """
        entry.notified_at = dt.datetime.now(dt.UTC)


async def iter_pending_dispatch(
    session: AsyncSession,
) -> AsyncIterator[EventLogEntry]:  # pragma: no cover - convenience helper
    """Yield not-yet-notified event rows oldest-first (ad-hoc inspection)."""
    result = await session.execute(
        select(EventLogEntry)
        .where(EventLogEntry.notified_at.is_(None))
        .order_by(EventLogEntry.occurred_at)
    )
    for row in result.scalars().all():
        yield row
