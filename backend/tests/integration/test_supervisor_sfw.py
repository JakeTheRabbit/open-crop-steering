"""Integration tests for the supervisor's SFW path (Phase 8).

Drives a full supervisor tick against real Postgres with a
:class:`FakeLLMClient` returning a non-empty proposal, against a room
whose rollout stage puts its parameter class in ``supervised_approval``
mode.

Covered:

* A supervised_approval room + a proposal → a ``pending_approval`` row is
  created and **no** ``runtime_adjustment`` is applied until a human
  approves (the supervisor never auto-applies in SFW mode).
* A report-only room + the same proposal → **no** pending row (Report
  mode is unchanged).
* The tick sweeps expired pending approvals.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from app.core.action_set import ParamCandidate
from app.core.equipment import RoomContext
from app.core.room_runtime import get_or_create
from app.influx_client import ThresholdOp
from app.llm_client import FakeLLMClient
from app.models.command_queue import CommandQueueEntry
from app.models.pending_approval import PendingApproval, PendingStatus
from app.models.recipe_revision import (
    RecipeRevision,
    RecipeRevisionParam,
    RecipeStatus,
)
from app.models.runtime_adjustment import AdjustmentMode, RuntimeAdjustment
from app.models.user import User
from app.schemas.llm_decision import LLM_DECISION_SCHEMA_VERSION
from app.workers.supervisor import RoomTickInput, Supervisor
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------- doubles


class FakeInflux:
    """Scripted InfluxClient double."""

    def __init__(
        self,
        *,
        current: dict[str, float] | None = None,
        slope: dict[str, float] | None = None,
        threshold: dict[str, bool] | None = None,
        delta: dict[str, float] | None = None,
    ) -> None:
        self._current = current or {}
        self._slope = slope or {}
        self._threshold = threshold or {}
        self._delta = delta or {}

    def current_value(self, entity: str) -> float | None:
        return self._current.get(entity)

    def trend_slope(self, entity: str, window: timedelta | str) -> float | None:
        return self._slope.get(entity)

    def at_threshold_for(
        self,
        entity: str,
        op: ThresholdOp,
        threshold: float,
        duration: timedelta | str,
    ) -> bool:
        return self._threshold.get(entity, False)

    def delta_post_event(
        self, entity: str, event_ts: str, window: timedelta | str
    ) -> float | None:
        return self._delta.get(entity)


# --------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(async_engine: AsyncEngine) -> AsyncIterator[None]:
    """Clear globally-scanned tables — the supervisor commits its own sessions."""
    from app.core.effective import refresh_effective_targets  # noqa: PLC0415

    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with maker() as cleanup:
        for table in (
            "command_queue",
            "command_batch",
            "runtime_adjustment",
            "llm_call_log",
            "event_log",
            "sensor_snapshot",
            "pending_approval",
            "room_runtime",
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
        await cleanup.execute(text("DELETE FROM users WHERE id LIKE 'sv-%'"))
        await cleanup.commit()
        await refresh_effective_targets(cleanup)
        await cleanup.commit()
    yield


def _session_factory(engine: AsyncEngine):  # type: ignore[no-untyped-def]
    """Zero-arg factory yielding a GUC-set AsyncSession context manager."""
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


def _room_context(room_id: str = "sv-f1") -> RoomContext:
    """Room context with temp + RH sensors mapped.

    The sensor ``entity_id`` tags follow the ``{room_id}_{measurement}``
    convention :func:`app.core.tolerance` derives, so the tolerance
    check and the snapshot read the same tags the fake Influx serves.
    """
    return RoomContext(
        room_id=room_id,
        temp_actual_entity=f"{room_id}_temp",
        temp_setpoint=26.0,
        rh_actual_entity=f"{room_id}_rh",
        rh_setpoint=60.0,
    )


async def _seed_recipe(
    session: AsyncSession, *, uid: str, room_id: str, day_index: int = 1
) -> int:
    """Seed an approved recipe + refresh the matview."""
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


#: Action-set candidates naming ``temp_day`` (a Class-B param) so the
#: strict parser's allowed-param check accepts a ``temp_day`` proposal.
_TEMP_DAY_CANDIDATES = [
    ParamCandidate(param_name="temp_day", current_value=26.0, tolerance=0.5)
]


def _proposal_payload(snapshot_id: int) -> dict[str, Any]:
    """A valid ocs.llm_decision.v1 payload proposing one temp_day cut.

    ``temp_day`` is a Class-B parameter; at a rollout stage where Class B
    is in ``supervised_approval`` this proposal must become a pending
    approval rather than auto-apply.
    """
    return {
        "schema_version": LLM_DECISION_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "assessment": "Room is running warm; a small day-setpoint cut helps.",
        "recommended_action_id": None,
        "proposed_changes": [
            {
                "param_name": "temp_day",
                "direction": "decrease",
                "delta": -0.4,
                "unit": "C",
                "rationale": "Recentre the temp band.",
            }
        ],
        "confidence": 0.82,
        "reason_codes": ["EC-001"],
        "human_summary": "Lower temp_day by 0.4 C to recentre the band.",
        "requires_human": True,
    }


async def _count(session: AsyncSession, model: Any, **filters: Any) -> int:
    """Count rows of ``model`` matching ``filters``."""
    stmt = select(func.count()).select_from(model)
    for col, value in filters.items():
        stmt = stmt.where(getattr(model, col) == value)
    return int((await session.execute(stmt)).scalar_one())


# --------------------------------------------------------------------- tests


async def test_supervised_approval_room_creates_pending_no_overlay(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """An SFW-mode room + a proposal -> a pending row, NO overlay applied.

    The room is put at rollout stage ``stage_1`` — at which a Class-B
    parameter (``temp_day``) resolves to ``supervised_approval``. The
    out-of-band tick calls the LLM, which proposes a temp_day cut. The
    supervisor must park that as a ``pending_approval`` and apply
    nothing — no overlay, no command — until a human approves.
    """
    rev_id = await _seed_recipe(session, uid="sv-sfw", room_id="sv-f1")

    # Put the room in a stage where Class B is supervised_approval.
    async with _session_factory(async_engine)() as setup:
        runtime = await get_or_create(setup, "sv-f1")
        runtime.rollout_stage = "stage_1"
        await setup.commit()

    # temp far above the 26.0 +- 0.5 band -> out of band -> LLM consulted.
    influx = FakeInflux(current={"sv-f1_temp": 30.0, "sv-f1_rh": 60.0})
    llm = FakeLLMClient(
        response_payload=_proposal_payload(snapshot_id=0),
        echo_snapshot_id=True,
    )
    supervisor = Supervisor(
        _session_factory(async_engine),
        influx,
        None,
        llm,
        rooms_provider=lambda: [
            RoomTickInput(
                room_id="sv-f1",
                room_context=_room_context(),
                recipe_revision_id=rev_id,
                action_candidates=_TEMP_DAY_CANDIDATES,
            )
        ],
        now_provider=lambda: dt.datetime(2026, 5, 16, 12, 0, tzinfo=dt.UTC),
    )
    results = await supervisor.tick()

    assert len(results) == 1
    result = results[0]
    assert result.llm_called is True
    assert result.mode is AdjustmentMode.supervised_approval
    assert result.pending_id is not None

    session.expire_all()
    # A pending approval row was created, still open.
    pending = await session.get(PendingApproval, result.pending_id)
    assert pending is not None
    assert pending.room_id == "sv-f1"
    assert pending.status is PendingStatus.open
    # It carries the proposal + the snapshot's cycle day.
    assert pending.proposal["proposed_changes"][0]["param_name"] == "temp_day"
    assert pending.proposal["day_index"] == 1
    assert pending.snapshot_id == result.snapshot_id

    # CRITICAL: nothing applied until approval — no overlay, no command.
    assert await _count(session, RuntimeAdjustment, room_id="sv-f1") == 0
    assert await _count(session, CommandQueueEntry) == 0


async def test_report_only_room_creates_no_pending(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A report-only room + the same proposal -> NO pending row.

    The default rollout stage (``report_only``) keeps every class in
    Report mode; the proposal is logged but never parked for approval.
    """
    rev_id = await _seed_recipe(session, uid="sv-rep", room_id="sv-f2")

    influx = FakeInflux(current={"sv-f2_temp": 30.0, "sv-f2_rh": 60.0})
    llm = FakeLLMClient(
        response_payload=_proposal_payload(snapshot_id=0),
        echo_snapshot_id=True,
    )
    supervisor = Supervisor(
        _session_factory(async_engine),
        influx,
        None,
        llm,
        rooms_provider=lambda: [
            RoomTickInput(
                room_id="sv-f2",
                room_context=_room_context("sv-f2"),
                recipe_revision_id=rev_id,
                action_candidates=_TEMP_DAY_CANDIDATES,
            )
        ],
        now_provider=lambda: dt.datetime(2026, 5, 16, 12, 0, tzinfo=dt.UTC),
    )
    results = await supervisor.tick()

    assert results[0].llm_called is True
    assert results[0].mode is AdjustmentMode.report_only
    assert results[0].pending_id is None

    session.expire_all()
    assert await _count(session, PendingApproval, room_id="sv-f2") == 0
    assert await _count(session, RuntimeAdjustment, room_id="sv-f2") == 0


async def test_supervised_approval_empty_proposal_stays_report(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """An SFW-stage room with an empty proposal creates no pending row.

    Effective mode is resolved per *proposed change*; a decision that
    proposes nothing has nothing to approve, so it resolves to
    ``report_only`` and no pending row is created.
    """
    rev_id = await _seed_recipe(session, uid="sv-empty", room_id="sv-f3")
    async with _session_factory(async_engine)() as setup:
        runtime = await get_or_create(setup, "sv-f3")
        runtime.rollout_stage = "stage_1"
        await setup.commit()

    influx = FakeInflux(current={"sv-f3_temp": 30.0, "sv-f3_rh": 60.0})
    empty = _proposal_payload(snapshot_id=0)
    empty["proposed_changes"] = []
    llm = FakeLLMClient(response_payload=empty, echo_snapshot_id=True)
    supervisor = Supervisor(
        _session_factory(async_engine),
        influx,
        None,
        llm,
        rooms_provider=lambda: [
            RoomTickInput(
                room_id="sv-f3",
                room_context=_room_context("sv-f3"),
                recipe_revision_id=rev_id,
            )
        ],
        now_provider=lambda: dt.datetime(2026, 5, 16, 12, 0, tzinfo=dt.UTC),
    )
    results = await supervisor.tick()

    assert results[0].llm_called is True
    assert results[0].mode is AdjustmentMode.report_only
    assert results[0].pending_id is None

    session.expire_all()
    assert await _count(session, PendingApproval, room_id="sv-f3") == 0


async def test_tick_expires_stale_pending(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """The tick sweeps SFW pending approvals past their TTL.

    A pending row with an ``expires_at`` in the past is flipped to
    ``expired`` by the leading sweep of the next tick.
    """
    rev_id = await _seed_recipe(session, uid="sv-stale", room_id="sv-f4")

    # Seed a snapshot + a past-TTL open pending row directly.
    from app.models.sensor_snapshot import SensorSnapshot  # noqa: PLC0415

    async with _session_factory(async_engine)() as setup:
        snapshot = SensorSnapshot(
            room_id="sv-f4",
            payload={"room_id": "sv-f4"},
            recipe_revision_id=rev_id,
            cycle_day=1,
            rollout_stage="report_only",
        )
        setup.add(snapshot)
        await setup.flush()
        stale = PendingApproval(
            room_id="sv-f4",
            proposal=_proposal_payload(snapshot.id),
            snapshot_id=snapshot.id,
            llm_call_id=None,
            summary="stale pending",
            status=PendingStatus.open,
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5),
        )
        setup.add(stale)
        await setup.commit()
        stale_id = stale.id

    # An in-band room means the tick does no LLM work — but the sweep
    # still runs.
    influx = FakeInflux(current={"sv-f4_temp": 26.0, "sv-f4_rh": 60.0})
    supervisor = Supervisor(
        _session_factory(async_engine),
        influx,
        None,
        FakeLLMClient(response_payload=_proposal_payload(1)),
        rooms_provider=lambda: [
            RoomTickInput(
                room_id="sv-f4",
                room_context=_room_context("sv-f4"),
                recipe_revision_id=rev_id,
            )
        ],
        now_provider=lambda: dt.datetime(2026, 5, 16, 12, 0, tzinfo=dt.UTC),
    )
    await supervisor.tick()

    session.expire_all()
    refreshed = await session.get(PendingApproval, stale_id)
    assert refreshed is not None
    assert refreshed.status is PendingStatus.expired
