"""Integration tests for :mod:`app.core.sfw` — the SFW pending lifecycle.

Runs against real Postgres. Covered:

* :func:`create_pending` writes a pending row + an audit row.
* :func:`approve_pending` on a clean proposal adds runtime overlay(s),
  enqueues a command batch, and marks the row ``approved``.
* :func:`reject_pending` marks the row ``rejected`` with no overlay /
  command.
* :func:`expire_stale_pending` expires an open row past its TTL.
* :func:`recheck_proposal` fails when the room's active recipe revision
  changed after the snapshot was taken.

The SFW apply path calls :func:`app.core.overlays.add_adjustment`, which
refreshes the ``effective_target`` materialized view — and a matview
refresh only sees *committed* data. So these tests seed + commit the
recipe (like the supervisor tests do) and an autouse fixture truncates
the leaked rows between tests.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.sfw import (
    approve_pending,
    create_pending,
    expire_stale_pending,
    recheck_proposal,
    reject_pending,
)
from app.models.audit_event import AuditEvent, AuditEventType
from app.models.command_queue import CommandBatch, CommandQueueEntry
from app.models.pending_approval import PendingApproval, PendingStatus
from app.models.recipe_revision import (
    RecipeRevision,
    RecipeRevisionParam,
    RecipeStatus,
)
from app.models.runtime_adjustment import RuntimeAdjustment
from app.models.sensor_snapshot import SensorSnapshot
from app.models.user import User
from app.schemas.llm_decision import LLM_DECISION_SCHEMA_VERSION
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(async_engine: AsyncEngine) -> AsyncIterator[None]:
    """Truncate the tables these tests commit into, before each test."""
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
        await cleanup.execute(text("DELETE FROM users WHERE id LIKE 'sfw-%'"))
        await cleanup.commit()
        await refresh_effective_targets(cleanup)
        await cleanup.commit()
    yield


def _proposal(snapshot_id: int, *, day_index: int = 1) -> dict:
    """A valid ocs.llm_decision.v1 proposal with one concrete change."""
    return {
        "schema_version": LLM_DECISION_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "assessment": "Room is drifting warm; a small setpoint cut is warranted.",
        "recommended_action_id": None,
        "proposed_changes": [
            {
                "param_name": "temp_day",
                "direction": "decrease",
                "delta": -0.4,
                "unit": "C",
                "rationale": "Trim the day setpoint toward the band centre.",
            }
        ],
        "confidence": 0.8,
        "reason_codes": ["EC-001"],
        "human_summary": "Lower temp_day by 0.4 C to recentre the band.",
        "requires_human": True,
        "day_index": day_index,
    }


async def _seed_recipe(
    session: AsyncSession,
    *,
    uid: str,
    room_id: str,
    day_index: int = 1,
) -> int:
    """Seed + commit an approved recipe; return its revision id."""
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
    session.add(
        RecipeRevisionParam(
            recipe_revision_id=rev.id,
            room_id=room_id,
            day_index=day_index,
            param_name="temp_day",
            value=26.0,
            tolerance=0.5,
        )
    )
    await session.commit()
    await refresh_effective_targets(session)
    await session.commit()
    return rev.id


async def _seed_snapshot(
    session: AsyncSession, *, room_id: str, recipe_revision_id: int
) -> SensorSnapshot:
    """Seed + commit a minimal snapshot row tied to a recipe revision."""
    snapshot = SensorSnapshot(
        room_id=room_id,
        payload={"room_id": room_id, "schema_version": "ocs.snapshot.v1"},
        recipe_revision_id=recipe_revision_id,
        cycle_day=1,
        rollout_stage="stage_3",
    )
    session.add(snapshot)
    await session.commit()
    return snapshot


async def _count(session: AsyncSession, model: type, **filters: object) -> int:
    """Count rows of ``model`` matching ``filters``."""
    stmt = select(func.count()).select_from(model)
    for col, value in filters.items():
        stmt = stmt.where(getattr(model, col) == value)
    return int((await session.execute(stmt)).scalar_one())


# --------------------------------------------------------------------- create


async def test_create_pending_writes_row_and_audit(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """create_pending inserts an open pending row and an audit event."""
    rev_id = await _seed_recipe(session, uid="sfw-create", room_id="sfw-r1")
    snapshot = await _seed_snapshot(
        session, room_id="sfw-r1", recipe_revision_id=rev_id
    )

    pending = await create_pending(
        session,
        room_id="sfw-r1",
        proposal=_proposal(snapshot.id),
        snapshot_id=snapshot.id,
        llm_call_id=None,
        summary="Lower temp_day by 0.4 C",
    )
    await session.commit()

    assert pending.id is not None
    assert pending.status is PendingStatus.open
    assert pending.expires_at > dt.datetime.now(dt.UTC)

    # An audit row records the pending opening.
    audit_count = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.params["pending_id"].astext == str(pending.id))
    )
    assert int(audit_count or 0) >= 1


# --------------------------------------------------------------------- approve


async def test_approve_pending_applies_overlay_and_command_batch(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A clean approve adds an overlay, enqueues a batch, marks approved."""
    rev_id = await _seed_recipe(session, uid="sfw-appr", room_id="sfw-r2")
    snapshot = await _seed_snapshot(
        session, room_id="sfw-r2", recipe_revision_id=rev_id
    )
    pending = await create_pending(
        session,
        room_id="sfw-r2",
        proposal=_proposal(snapshot.id),
        snapshot_id=snapshot.id,
        llm_call_id=None,
        summary="Lower temp_day by 0.4 C",
    )
    await session.commit()

    decided = await approve_pending(
        session,
        pending.id,
        decided_by="sfw-appr",
        channel="ui",
    )
    await session.commit()

    assert decided.status is PendingStatus.approved
    assert decided.decided_by == "sfw-appr"
    assert decided.decision_channel == "ui"
    assert decided.decided_at is not None

    # One overlay was added for the proposed change.
    assert await _count(session, RuntimeAdjustment, room_id="sfw-r2") == 1
    overlay = (
        await session.execute(
            select(RuntimeAdjustment).where(
                RuntimeAdjustment.room_id == "sfw-r2"
            )
        )
    ).scalar_one()
    assert overlay.param_name == "temp_day"
    assert overlay.delta == pytest.approx(-0.4)

    # A command batch + command were enqueued to write the new setpoint.
    assert await _count(session, CommandBatch, room_id="sfw-r2") == 1
    assert await _count(session, CommandQueueEntry) == 1
    command = (
        await session.execute(select(CommandQueueEntry))
    ).scalar_one()
    # recipe 26.0 + overlay -0.4 = 25.6 effective setpoint.
    assert command.service_data["value"] == pytest.approx(25.6)

    # Exactly one controlled_adjustment audit row for the decision.
    decision_audits = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.event_type == AuditEventType.controlled_adjustment,
            AuditEvent.params["pending_id"].astext == str(pending.id),
        )
    )
    assert int(decision_audits or 0) == 1


async def test_approve_pending_rejects_when_already_decided(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A second decision on a decided pending row raises ValueError."""
    rev_id = await _seed_recipe(session, uid="sfw-twice", room_id="sfw-r3")
    snapshot = await _seed_snapshot(
        session, room_id="sfw-r3", recipe_revision_id=rev_id
    )
    pending = await create_pending(
        session,
        room_id="sfw-r3",
        proposal=_proposal(snapshot.id),
        snapshot_id=snapshot.id,
        llm_call_id=None,
        summary="x",
    )
    await session.commit()
    await approve_pending(
        session, pending.id, decided_by="sfw-twice", channel="ui"
    )
    await session.commit()

    with pytest.raises(ValueError, match="already been decided"):
        await approve_pending(
            session, pending.id, decided_by="sfw-twice", channel="ui"
        )


# --------------------------------------------------------------------- reject


async def test_reject_pending_marks_rejected_no_overlay(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """reject_pending marks the row rejected and applies nothing."""
    rev_id = await _seed_recipe(session, uid="sfw-rej", room_id="sfw-r4")
    snapshot = await _seed_snapshot(
        session, room_id="sfw-r4", recipe_revision_id=rev_id
    )
    pending = await create_pending(
        session,
        room_id="sfw-r4",
        proposal=_proposal(snapshot.id),
        snapshot_id=snapshot.id,
        llm_call_id=None,
        summary="x",
    )
    await session.commit()

    decided = await reject_pending(
        session,
        pending.id,
        decided_by="sfw-rej",
        channel="ui",
        notes="not warranted right now",
    )
    await session.commit()

    assert decided.status is PendingStatus.rejected
    assert decided.decision_notes == "not warranted right now"
    # No control side effects.
    assert await _count(session, RuntimeAdjustment, room_id="sfw-r4") == 0
    assert await _count(session, CommandQueueEntry) == 0


# --------------------------------------------------------------------- expire


async def test_expire_stale_pending_expires_past_ttl_row(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """expire_stale_pending flips an open, past-TTL row to expired."""
    rev_id = await _seed_recipe(session, uid="sfw-exp", room_id="sfw-r5")
    snapshot = await _seed_snapshot(
        session, room_id="sfw-r5", recipe_revision_id=rev_id
    )
    pending = await create_pending(
        session,
        room_id="sfw-r5",
        proposal=_proposal(snapshot.id),
        snapshot_id=snapshot.id,
        llm_call_id=None,
        summary="x",
    )
    # Force the row's TTL into the past.
    pending.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
    await session.commit()

    expired = await expire_stale_pending(session)
    await session.commit()

    assert expired == 1
    refreshed = await session.get(PendingApproval, pending.id)
    assert refreshed is not None
    assert refreshed.status is PendingStatus.expired

    # A still-open, in-TTL row is left alone.
    assert await expire_stale_pending(session) == 0


# --------------------------------------------------------------------- recheck


async def test_recheck_fails_when_recipe_revision_changed(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """recheck_proposal fails if the active recipe revision moved on.

    The snapshot is tied to revision N; a newer approved revision N+1 is
    then created. The re-check must reject the now-stale proposal.
    """
    rev1 = await _seed_recipe(session, uid="sfw-rc", room_id="sfw-r6")
    snapshot = await _seed_snapshot(
        session, room_id="sfw-r6", recipe_revision_id=rev1
    )
    pending = await create_pending(
        session,
        room_id="sfw-r6",
        proposal=_proposal(snapshot.id),
        snapshot_id=snapshot.id,
        llm_call_id=None,
        summary="x",
    )
    await session.commit()

    # A newer approved revision supersedes the one the snapshot used.
    rev2 = RecipeRevision(
        room_id="sfw-r6",
        version=2,
        name="Recipe sfw-r6 v2",
        status=RecipeStatus.approved,
        created_by="sfw-rc",
        approved_by="sfw-rc",
    )
    session.add(rev2)
    await session.flush()

    active = (
        await session.execute(
            select(RecipeRevision)
            .where(
                RecipeRevision.room_id == "sfw-r6",
                RecipeRevision.status == RecipeStatus.approved,
            )
            .order_by(RecipeRevision.version.desc())
            .limit(1)
        )
    ).scalar_one()
    result = await recheck_proposal(session, snapshot, pending, active)
    assert result.ok is False
    assert result.reason == "recipe_revision_changed"

    # And the re-check holds when the active revision still matches.
    same = await session.get(RecipeRevision, rev1)
    ok_result = await recheck_proposal(session, snapshot, pending, same)
    assert ok_result.ok is True


async def test_approve_rejects_on_failed_recheck(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """approve_pending rejects (not applies) when the re-check fails.

    A missing snapshot is one re-check failure mode; the pending row is
    kept but marked rejected, with a guardrail_rejection audit row and
    no overlay.
    """
    rev_id = await _seed_recipe(session, uid="sfw-rcf", room_id="sfw-r7")
    snapshot = await _seed_snapshot(
        session, room_id="sfw-r7", recipe_revision_id=rev_id
    )
    pending = await create_pending(
        session,
        room_id="sfw-r7",
        proposal=_proposal(snapshot.id),
        snapshot_id=snapshot.id,
        llm_call_id=None,
        summary="x",
    )
    await session.commit()

    # Delete the snapshot so the re-check's snapshot-exists check fails.
    # ORM delete keeps the identity map consistent, so approve_pending's
    # ``session.get`` correctly returns ``None`` for the gone row.
    await session.delete(snapshot)
    await session.commit()

    decided = await approve_pending(
        session, pending.id, decided_by="sfw-rcf", channel="ui"
    )
    await session.commit()

    assert decided.status is PendingStatus.rejected
    assert "re-check failed" in (decided.decision_notes or "")
    assert await _count(session, RuntimeAdjustment, room_id="sfw-r7") == 0

    rejection_audits = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.event_type == AuditEventType.guardrail_rejection,
            AuditEvent.params["pending_id"].astext == str(pending.id),
        )
    )
    assert int(rejection_audits or 0) == 1
