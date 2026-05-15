"""Integration tests for :func:`app.workers.telegram_bot.handle_callback`.

The testable core of the Telegram approval bot, exercised against real
Postgres with minimal fake callback objects — no live bot, no network.

Covered (plan locked decision #9 — Telegram approvals authoritative only
when the chat_id maps to an HA user with the right role):

* A mapped + privileged (cultivator) chat_id approves a pending row.
* An **unmapped** chat_id is refused, and the refusal writes an audit
  row capturing the chat_id.
* A mapped-but-**operator-only** chat_id is refused (operator is below
  the cultivator decision floor).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from app.core.sfw import create_pending
from app.models.audit_event import AuditEvent, AuditEventType
from app.models.pending_approval import PendingApproval, PendingStatus
from app.models.recipe_revision import (
    RecipeRevision,
    RecipeRevisionParam,
    RecipeStatus,
)
from app.models.runtime_adjustment import RuntimeAdjustment
from app.models.sensor_snapshot import SensorSnapshot
from app.models.telegram_user_map import TelegramUserMap
from app.models.user import User
from app.schemas.llm_decision import LLM_DECISION_SCHEMA_VERSION
from app.workers.telegram_bot import (
    ACTION_APPROVE,
    ACTION_CUSTOM,
    ACTION_REJECT,
    callback_data,
    handle_callback,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


# ----------------------------------------------------------- fake callback objs


@dataclass
class _FakeChat:
    """A Telegram ``Chat``-shaped object."""

    id: int


@dataclass
class _FakeMessage:
    """A Telegram ``Message``-shaped object carrying the chat."""

    chat: _FakeChat


@dataclass
class _FakeCallbackQuery:
    """A Telegram ``CallbackQuery``-shaped object — data + nested chat."""

    data: str
    message: _FakeMessage | None


def _callback(action: str, pending_id: int, chat_id: int) -> _FakeCallbackQuery:
    """Build a fake callback query for an action on a pending from a chat."""
    return _FakeCallbackQuery(
        data=callback_data(action, pending_id),
        message=_FakeMessage(chat=_FakeChat(id=chat_id)),
    )


# --------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(async_engine: AsyncEngine) -> AsyncIterator[None]:
    """Truncate committed tables these tests touch, before each test."""
    from app.core.effective import refresh_effective_targets  # noqa: PLC0415

    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with maker() as cleanup:
        for table in (
            "command_queue",
            "command_batch",
            "runtime_adjustment",
            "event_log",
            "sensor_snapshot",
            "pending_approval",
            "telegram_user_map",
        ):
            await cleanup.execute(text(f"DELETE FROM {table}"))  # noqa: S608
        await cleanup.execute(
            text(
                "UPDATE recipe_revision SET status = 'superseded' "
                "WHERE status = 'approved'"
            )
        )
        await cleanup.execute(text("DELETE FROM recipe_revision_param"))
        await cleanup.execute(text("DELETE FROM recipe_revision"))
        await cleanup.execute(text("DELETE FROM users WHERE id LIKE 'tg-%'"))
        await cleanup.commit()
        await refresh_effective_targets(cleanup)
        await cleanup.commit()
    yield


def _proposal(snapshot_id: int) -> dict:
    """A valid ocs.llm_decision.v1 proposal with one concrete change."""
    return {
        "schema_version": LLM_DECISION_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "assessment": "Room drifting warm.",
        "recommended_action_id": None,
        "proposed_changes": [
            {
                "param_name": "temp_day",
                "direction": "decrease",
                "delta": -0.3,
                "unit": "C",
                "rationale": "Trim toward band centre.",
            }
        ],
        "confidence": 0.8,
        "reason_codes": [],
        "human_summary": "Lower temp_day by 0.3 C.",
        "requires_human": True,
        "day_index": 1,
    }


async def _seed_user(
    session: AsyncSession, uid: str, *, role: str | None
) -> None:
    """Seed a user and (optionally) one role row."""
    session.add(User(id=uid, display_name=f"User {uid}"))
    await session.flush()
    if role is not None:
        await session.execute(
            text(
                "INSERT INTO user_roles (user_id, role_name) "
                "VALUES (:u, :r)"
            ),
            {"u": uid, "r": role},
        )
        await session.flush()


async def _map_chat(
    session: AsyncSession, chat_id: str, user_id: str
) -> None:
    """Map a Telegram chat_id to an HA user."""
    session.add(
        TelegramUserMap(
            chat_id=chat_id, user_id=user_id, created_by="tg-admin"
        )
    )
    await session.flush()


async def _seed_pending(
    session: AsyncSession, *, uid: str, room_id: str
) -> PendingApproval:
    """Seed + commit a recipe + snapshot + an open pending approval."""
    from app.core.effective import refresh_effective_targets  # noqa: PLC0415

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
    session.add(
        RecipeRevisionParam(
            recipe_revision_id=rev.id,
            room_id=room_id,
            day_index=1,
            param_name="temp_day",
            value=26.0,
            tolerance=0.5,
        )
    )
    await session.commit()
    await refresh_effective_targets(session)
    await session.commit()

    snapshot = SensorSnapshot(
        room_id=room_id,
        payload={"room_id": room_id},
        recipe_revision_id=rev.id,
        cycle_day=1,
        rollout_stage="stage_3",
    )
    session.add(snapshot)
    await session.commit()

    pending = await create_pending(
        session,
        room_id=room_id,
        proposal=_proposal(snapshot.id),
        snapshot_id=snapshot.id,
        llm_call_id=None,
        summary="Lower temp_day",
    )
    await session.commit()
    return pending


# --------------------------------------------------------------------- tests


async def test_mapped_privileged_chat_approves(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A cultivator-mapped chat_id approves the pending row."""
    await _seed_user(session, "tg-cult", role="cultivator")
    await _map_chat(session, "555001", "tg-cult")
    pending = await _seed_pending(session, uid="tg-cult", room_id="tg-r1")

    result = await handle_callback(
        session, _callback(ACTION_APPROVE, pending.id, 555001)
    )
    await session.commit()

    assert result.ok is True
    assert result.refused is False
    assert result.user_id == "tg-cult"
    assert result.pending_id == pending.id

    refreshed = await session.get(PendingApproval, pending.id)
    assert refreshed is not None
    assert refreshed.status is PendingStatus.approved
    # Telegram is the recorded channel + the chat_id is stamped on the row.
    assert refreshed.decision_channel == "telegram"
    assert refreshed.decision_chat_id == "555001"
    assert refreshed.decided_by == "tg-cult"
    # The approval applied the overlay.
    overlays = await session.scalar(
        select(func.count())
        .select_from(RuntimeAdjustment)
        .where(RuntimeAdjustment.room_id == "tg-r1")
    )
    assert int(overlays or 0) == 1


async def test_mapped_privileged_chat_rejects(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A cultivator-mapped chat_id can also reject the pending row."""
    await _seed_user(session, "tg-cult2", role="qap")
    await _map_chat(session, "555009", "tg-cult2")
    pending = await _seed_pending(session, uid="tg-cult2", room_id="tg-r9")

    result = await handle_callback(
        session, _callback(ACTION_REJECT, pending.id, 555009)
    )
    await session.commit()

    assert result.ok is True
    refreshed = await session.get(PendingApproval, pending.id)
    assert refreshed is not None
    assert refreshed.status is PendingStatus.rejected


async def test_unmapped_chat_is_refused_and_audited(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """An unmapped chat_id is refused; an audit row captures the chat_id."""
    await _seed_user(session, "tg-owner", role="cultivator")
    pending = await _seed_pending(session, uid="tg-owner", room_id="tg-r2")
    # No telegram_user_map row for chat 999777.

    result = await handle_callback(
        session, _callback(ACTION_APPROVE, pending.id, 999777)
    )
    await session.commit()

    assert result.ok is False
    assert result.refused is True
    assert result.user_id is None

    # The pending row is untouched — still open.
    refreshed = await session.get(PendingApproval, pending.id)
    assert refreshed is not None
    assert refreshed.status is PendingStatus.open

    # A system_warning audit row records the refused chat_id.
    refusal = (
        await session.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.system_warning,
                AuditEvent.params["refusal_reason"].astext
                == "unmapped_chat_id",
            )
        )
    ).scalars().all()
    assert len(refusal) == 1
    assert refusal[0].params["chat_id"] == "999777"
    assert refusal[0].params["pending_id"] == pending.id


async def test_mapped_operator_only_chat_is_refused(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A mapped chat whose user is only an operator is refused."""
    await _seed_user(session, "tg-op", role="operator")
    await _map_chat(session, "555222", "tg-op")
    pending = await _seed_pending(session, uid="tg-op", room_id="tg-r3")

    result = await handle_callback(
        session, _callback(ACTION_APPROVE, pending.id, 555222)
    )
    await session.commit()

    assert result.ok is False
    assert result.refused is True
    # The chat resolved to a user, so the result carries the user_id.
    assert result.user_id == "tg-op"

    refreshed = await session.get(PendingApproval, pending.id)
    assert refreshed is not None
    assert refreshed.status is PendingStatus.open

    # The refusal audit row records the under-privileged user + chat.
    refusal = (
        await session.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.system_warning,
                AuditEvent.params["refusal_reason"].astext
                == "insufficient_role",
            )
        )
    ).scalars().all()
    assert len(refusal) == 1
    assert refusal[0].params["chat_id"] == "555222"
    assert refusal[0].params["resolved_user_id"] == "tg-op"


async def test_custom_callback_is_a_noop(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """ocs_custom is a no-op stub — no decision, pending stays open."""
    await _seed_user(session, "tg-c", role="cultivator")
    await _map_chat(session, "555333", "tg-c")
    pending = await _seed_pending(session, uid="tg-c", room_id="tg-r4")

    result = await handle_callback(
        session, _callback(ACTION_CUSTOM, pending.id, 555333)
    )
    await session.commit()

    assert result.ok is False
    assert result.refused is False
    assert result.action == ACTION_CUSTOM

    refreshed = await session.get(PendingApproval, pending.id)
    assert refreshed is not None
    assert refreshed.status is PendingStatus.open


async def test_unparseable_callback_data_is_ignored(
    session: AsyncSession,
) -> None:
    """Garbage callback data yields a no-op result, not a crash."""
    bad = _FakeCallbackQuery(data="not-a-real-callback", message=None)
    result = await handle_callback(session, bad)
    assert result.ok is False
    assert result.action == ""
    assert result.pending_id is None
