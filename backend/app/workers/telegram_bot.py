"""Telegram approval bot — SFW decisions over an inline keyboard.

A pending SFW approval (see :mod:`app.core.sfw`) is pushed to Telegram as
a message carrying an inline keyboard: **Approve** / **Reject** (and a
**Customise** stub, a no-op until a later phase). Tapping a button sends
a ``callback_query`` whose ``data`` field encodes the action and the
pending id.

Authoritative-only (plan locked decision #9)
--------------------------------------------
A Telegram tap is authoritative **only** when the originating chat_id is
mapped — via ``telegram_user_map`` — to a known HA user who holds the
``cultivator`` role or higher. An unmapped chat_id, or a mapped user
without the role, is refused; the refusal itself is audit-rowed with the
chat_id so an attempt from an unknown chat is visible in the tamper-
evident chain. An authorised decision audits **both** the Telegram
chat_id and the resolved HA user_id (via :mod:`app.core.sfw`, which
records the channel + chat_id, plus the explicit refusal/authorise audit
rows here).

Testability
-----------
All decision logic lives in :func:`handle_callback`, which takes a plain
session + a minimal callback object — no live bot, no network. The
:class:`TelegramApprovalBot` class is only the thin
python-telegram-bot wiring (build the keyboard, send the message, run
the poller) and delegates every decision to :func:`handle_callback`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from app.core.acl import get_user_roles, has_min_role
from app.core.audit import log_audit
from app.core.auth import resolve_telegram_user
from app.core.sfw import approve_pending, reject_pending
from app.models.audit_event import AuditEventType
from app.models.user import RoleName

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

#: Decision channel recorded on the pending row + audit trail.
CHANNEL = "telegram"

#: Minimum role a Telegram user must hold to decide an SFW approval.
MIN_DECISION_ROLE = RoleName.cultivator

#: callback_data action prefixes (``<prefix>:<pending_id>``).
ACTION_APPROVE = "ocs_approve"
ACTION_REJECT = "ocs_reject"
ACTION_CUSTOM = "ocs_custom"
_ACTIONS = frozenset({ACTION_APPROVE, ACTION_REJECT, ACTION_CUSTOM})


def callback_data(action: str, pending_id: int) -> str:
    """Build the inline-keyboard ``callback_data`` for an action + pending.

    Args:
        action: One of :data:`ACTION_APPROVE` / :data:`ACTION_REJECT` /
            :data:`ACTION_CUSTOM`.
        pending_id: The :class:`~app.models.pending_approval.PendingApproval`
            id the button decides.

    Returns:
        The ``"<action>:<pending_id>"`` string.

    Raises:
        ValueError: If ``action`` is not a recognised action.
    """
    if action not in _ACTIONS:
        raise ValueError(f"unknown callback action {action!r}")
    return f"{action}:{pending_id}"


@dataclass(slots=True)
class CallbackResult:
    """The outcome of handling one Telegram callback query.

    Attributes:
        ok: ``True`` if the callback produced a real decision (an
            approve or reject that ran). A refused or no-op callback is
            ``ok=False``.
        action: The parsed action (``ocs_approve`` / ``ocs_reject`` /
            ``ocs_custom``), or ``""`` if the callback data was
            unparseable.
        pending_id: The parsed pending id, or ``None``.
        refused: ``True`` if the callback was refused on authorisation
            (unmapped chat_id or under-privileged user).
        message: A short human-readable result — surfaced to the user as
            the Telegram callback-answer toast.
        user_id: The resolved HA user_id, when the chat_id mapped.
    """

    ok: bool
    action: str = ""
    pending_id: int | None = None
    refused: bool = False
    message: str = ""
    user_id: str | None = None


@dataclass(slots=True)
class _ParsedCallback:
    """A parsed ``callback_data`` payload."""

    action: str
    pending_id: int


def _parse_callback_data(data: str | None) -> _ParsedCallback | None:
    """Parse ``"<action>:<pending_id>"`` callback data.

    Returns ``None`` for anything that is not a recognised action with an
    integer pending id — a malformed callback is ignored, not actioned.
    """
    if not data or ":" not in data:
        return None
    action, _, raw_id = data.partition(":")
    if action not in _ACTIONS:
        return None
    try:
        pending_id = int(raw_id)
    except ValueError:
        return None
    return _ParsedCallback(action=action, pending_id=pending_id)


def _extract_chat_id(callback_query: Any) -> str | None:
    """Best-effort pull of the originating chat_id from a callback query.

    Telegram nests the chat under ``callback_query.message.chat.id``. The
    fake callback objects the tests use mirror that shape; ``from_user``
    is the fallback for a chat-less inline query.
    """
    message = getattr(callback_query, "message", None)
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        from_user = getattr(callback_query, "from_user", None)
        chat_id = getattr(from_user, "id", None)
    return None if chat_id is None else str(chat_id)


async def _refuse(
    session: AsyncSession,
    *,
    chat_id: str | None,
    parsed: _ParsedCallback,
    reason: str,
    detail: str,
    user_id: str | None = None,
) -> CallbackResult:
    """Audit + return a refusal for an unauthorised Telegram callback.

    The refusal writes a ``system_warning`` audit row capturing the
    chat_id (and resolved user_id, when there was one) so an approval
    attempt from an unknown / under-privileged chat is itself in the
    tamper-evident chain.
    """
    await log_audit(
        session,
        event_type=AuditEventType.system_warning,
        actor_id=user_id,
        summary=(
            f"Telegram SFW callback refused ({reason}) for pending "
            f"#{parsed.pending_id} from chat {chat_id}"
        ),
        params={
            "pending_id": parsed.pending_id,
            "action": parsed.action,
            "channel": CHANNEL,
            "chat_id": chat_id,
            "resolved_user_id": user_id,
            "refusal_reason": reason,
        },
        reason_codes=["telegram_unauthorized"],
    )
    log.warning(
        "telegram_callback_refused",
        chat_id=chat_id,
        pending_id=parsed.pending_id,
        action=parsed.action,
        reason=reason,
    )
    return CallbackResult(
        ok=False,
        action=parsed.action,
        pending_id=parsed.pending_id,
        refused=True,
        message=detail,
        user_id=user_id,
    )


async def handle_callback(
    session: AsyncSession, callback_query: Any
) -> CallbackResult:
    """Handle one Telegram callback query — the testable decision core.

    Steps:

    1. Parse ``callback_query.data`` (``"<action>:<pending_id>"``); an
       unparseable payload is a no-op.
    2. ``ocs_custom`` is a no-op stub for now (a later phase wires the
       custom-value flow); it returns ``ok=False`` without a decision.
    3. Resolve the originating chat_id to an HA user via
       ``telegram_user_map``. An **unmapped** chat_id is refused +
       audited.
    4. Check the resolved user holds :data:`MIN_DECISION_ROLE` or higher.
       An **under-privileged** user is refused + audited.
    5. Dispatch an authorised ``ocs_approve`` / ``ocs_reject`` to
       :func:`app.core.sfw.approve_pending` / :func:`~app.core.sfw.reject_pending`
       with ``channel="telegram"``, recording the chat_id; the resolved
       HA user_id is the decision actor (so the audit trail carries
       both).

    The caller is responsible for committing the session.

    Args:
        session: Active async session.
        callback_query: A Telegram ``CallbackQuery``-shaped object —
            anything exposing ``.data`` and a nested
            ``.message.chat.id`` (or ``.from_user.id``).

    Returns:
        A :class:`CallbackResult` describing what happened.
    """
    parsed = _parse_callback_data(getattr(callback_query, "data", None))
    if parsed is None:
        log.debug(
            "telegram_callback_unparseable",
            data=getattr(callback_query, "data", None),
        )
        return CallbackResult(ok=False, message="unrecognised callback")

    # ocs_custom is a no-op stub for Phase 8.
    if parsed.action == ACTION_CUSTOM:
        log.info("telegram_callback_custom_noop", pending_id=parsed.pending_id)
        return CallbackResult(
            ok=False,
            action=parsed.action,
            pending_id=parsed.pending_id,
            message="Custom adjustments are not available yet.",
        )

    chat_id = _extract_chat_id(callback_query)

    # --- chat_id -> HA user (plan locked decision #9) --------------------
    user = await resolve_telegram_user(session, chat_id) if chat_id else None
    if user is None:
        return await _refuse(
            session,
            chat_id=chat_id,
            parsed=parsed,
            reason="unmapped_chat_id",
            detail=(
                "This Telegram chat is not authorised to approve "
                "changes. Ask an admin to map it."
            ),
        )

    # --- role gate -------------------------------------------------------
    roles = await get_user_roles(session, user.id)
    if not has_min_role(roles, MIN_DECISION_ROLE):
        return await _refuse(
            session,
            chat_id=chat_id,
            parsed=parsed,
            reason="insufficient_role",
            detail=(
                f"Your account lacks the '{MIN_DECISION_ROLE.value}' role "
                "required to decide approvals."
            ),
            user_id=user.id,
        )

    # --- authorised: dispatch to the SFW lifecycle -----------------------
    try:
        if parsed.action == ACTION_APPROVE:
            pending = await approve_pending(
                session,
                parsed.pending_id,
                decided_by=user.id,
                channel=CHANNEL,
                chat_id=chat_id,
            )
            decided = pending.status.value
            message = f"Approval #{pending.id}: {decided}."
        else:  # ACTION_REJECT
            pending = await reject_pending(
                session,
                parsed.pending_id,
                decided_by=user.id,
                channel=CHANNEL,
                chat_id=chat_id,
            )
            decided = pending.status.value
            message = f"Approval #{pending.id} rejected."
    except ValueError as exc:
        # Already decided / expired / unknown — surface, don't crash.
        log.warning(
            "telegram_callback_decision_unavailable",
            pending_id=parsed.pending_id,
            action=parsed.action,
            error=str(exc),
        )
        return CallbackResult(
            ok=False,
            action=parsed.action,
            pending_id=parsed.pending_id,
            message="This approval is no longer open.",
            user_id=user.id,
        )

    log.info(
        "telegram_callback_decided",
        pending_id=parsed.pending_id,
        action=parsed.action,
        user_id=user.id,
        chat_id=chat_id,
        status=decided,
    )
    return CallbackResult(
        ok=True,
        action=parsed.action,
        pending_id=parsed.pending_id,
        message=message,
        user_id=user.id,
    )


# --------------------------------------------------------------------------
# Live-wiring layer — thin python-telegram-bot wrapper. All decision logic
# stays in handle_callback above so this needs no test coverage.
# --------------------------------------------------------------------------


class _PendingLike(Protocol):
    """The minimal pending shape :meth:`TelegramApprovalBot.send_approval` needs."""

    id: int
    room_id: str
    summary: str


class TelegramApprovalBot:  # pragma: no cover - live integration wiring
    """python-telegram-bot wrapper for sending + deciding SFW approvals.

    Construction does not touch the network. :meth:`send_approval` pushes
    a pending approval to the configured chat with an inline keyboard;
    :meth:`run_polling` runs the long-poll loop and routes callbacks to
    :func:`handle_callback`.

    Args:
        token: Telegram bot token.
        chat_id: Default chat the approval messages are sent to.
        session_factory: Zero-arg callable yielding an ``AsyncSession``
            context manager (with HMAC GUCs set — i.e.
            :func:`app.db.open_session`).
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        session_factory: Any,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._session_factory = session_factory
        self._app: Any = None

    @staticmethod
    def _keyboard(pending_id: int) -> Any:
        """Build the Approve / Reject / Customise inline keyboard."""
        from telegram import (  # noqa: PLC0415 - optional dep, imported lazily
            InlineKeyboardButton,
            InlineKeyboardMarkup,
        )

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Approve",
                        callback_data=callback_data(ACTION_APPROVE, pending_id),
                    ),
                    InlineKeyboardButton(
                        "❌ Reject",
                        callback_data=callback_data(ACTION_REJECT, pending_id),
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "✏️ Customise",
                        callback_data=callback_data(ACTION_CUSTOM, pending_id),
                    ),
                ],
            ]
        )

    def _application(self) -> Any:
        """Return the lazily-built python-telegram-bot ``Application``."""
        if self._app is None:
            from telegram.ext import (  # noqa: PLC0415 - optional dep
                Application,
                CallbackQueryHandler,
            )

            self._app = Application.builder().token(self._token).build()
            self._app.add_handler(CallbackQueryHandler(self._on_callback))
        return self._app

    async def send_approval(self, pending: _PendingLike) -> None:
        """Send a pending approval to the configured chat.

        Args:
            pending: A pending approval (anything with ``id`` /
                ``room_id`` / ``summary``).
        """
        app = self._application()
        text = (
            f"\U0001f33f *SFW approval #{pending.id}* — room "
            f"`{pending.room_id}`\n\n{pending.summary}"
        )
        await app.bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=self._keyboard(pending.id),
        )
        log.info(
            "telegram_approval_sent",
            pending_id=pending.id,
            room_id=pending.room_id,
        )

    async def _on_callback(self, update: Any, _context: Any) -> None:
        """python-telegram-bot handler — route a callback to the core."""
        query = update.callback_query
        async with self._session_factory() as session:
            result = await handle_callback(session, query)
            await session.commit()
        # Answer the callback so Telegram clears the button's spinner.
        if query is not None:
            await query.answer(text=result.message or "Done", show_alert=False)

    def run_polling(self) -> None:
        """Run the long-poll loop (blocking) — for the ``alerts`` worker."""
        log.info("telegram_bot_polling_start")
        self._application().run_polling()
