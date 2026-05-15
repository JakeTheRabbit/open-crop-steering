"""Integration tests for the supervisor's bounded-auto-adjust path (Phase 9).

Drives a full supervisor tick against real Postgres with a
:class:`FakeLLMClient` returning a non-empty proposal, against a room
whose rollout stage puts its parameter class in ``bounded_auto_adjust``
mode.

Covered:

* A clean proposal in a bounded-auto-adjust room auto-applies — a
  ``runtime_adjustment`` overlay and a ``command_queue`` row are written
  (no human in the loop).
* An anti-pattern-violating proposal (lowering temp while the AC is
  saturated — ``SAT-AC`` + ``AP-02``) is rejected — **no** overlay is
  added, and a ``guardrail_rejection`` audit row is written citing both
  ids.
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
from app.models.audit_event import AuditEvent, AuditEventType
from app.models.command_queue import CommandQueueEntry
from app.models.cumulative_delta import CumulativeDelta
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
            "cumulative_delta",
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
        await cleanup.execute(text("DELETE FROM users WHERE id LIKE 'ba-%'"))
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


def _room_context(room_id: str) -> RoomContext:
    """Room context with temp + RH sensors mapped (no AC saturation)."""
    return RoomContext(
        room_id=room_id,
        temp_actual_entity=f"{room_id}_temp",
        temp_setpoint=26.0,
        rh_actual_entity=f"{room_id}_rh",
        rh_setpoint=60.0,
    )


def _saturated_ac_room_context(room_id: str) -> RoomContext:
    """Room context whose AC saturation predicate (SAT-AC) will fire.

    SAT-AC needs: the climate fan high for >= 15 min, temp above
    setpoint, and a positive temperature slope. The fake Influx is
    scripted to satisfy all three for the ``{room}_ac_fan`` and
    ``{room}_temp`` tags.
    """
    return RoomContext(
        room_id=room_id,
        temp_actual_entity=f"{room_id}_temp",
        temp_setpoint=26.0,
        ac_fan_entity=f"{room_id}_ac_fan",
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


#: Action-set candidate naming ``temp_day`` (a Class-B param).
_TEMP_DAY_CANDIDATES = [
    ParamCandidate(param_name="temp_day", current_value=26.0, tolerance=0.5)
]


def _proposal_payload(
    snapshot_id: int, *, delta: float = -0.4
) -> dict[str, Any]:
    """A valid ocs.llm_decision.v1 payload proposing one temp_day change."""
    direction = "decrease" if delta < 0 else "increase"
    return {
        "schema_version": LLM_DECISION_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "assessment": "Room is running warm; a small day-setpoint change.",
        "recommended_action_id": None,
        "proposed_changes": [
            {
                "param_name": "temp_day",
                "direction": direction,
                "delta": delta,
                "unit": "C",
                "rationale": "Recentre the temp band.",
            }
        ],
        "confidence": 0.82,
        "reason_codes": [],
        "human_summary": f"Change temp_day by {delta} C.",
        "requires_human": False,
    }


async def _count(session: AsyncSession, model: Any, **filters: Any) -> int:
    """Count rows of ``model`` matching ``filters``."""
    stmt = select(func.count()).select_from(model)
    for col, value in filters.items():
        stmt = stmt.where(getattr(model, col) == value)
    return int((await session.execute(stmt)).scalar_one())


async def _set_stage(
    engine: AsyncEngine, room_id: str, stage: str
) -> None:
    """Put a room at a given rollout stage (committed)."""
    async with _session_factory(engine)() as setup:
        runtime = await get_or_create(setup, room_id)
        runtime.rollout_stage = stage
        await setup.commit()


# --------------------------------------------------------------------- tests


async def test_bounded_clean_proposal_auto_applies(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A clean proposal in a bounded-auto-adjust room auto-applies.

    The room is at ``stage_2`` — at which a Class-B parameter
    (``temp_day``) resolves to ``bounded_auto_adjust``. The out-of-band
    tick calls the LLM, which proposes a small in-bounds temp_day cut.
    The guardrail validator approves it; the supervisor must auto-apply
    — a ``runtime_adjustment`` overlay and a ``command_queue`` row — with
    no human in the loop.
    """
    rev_id = await _seed_recipe(session, uid="ba-clean", room_id="ba-f1")
    await _set_stage(async_engine, "ba-f1", "stage_2")

    # temp far above the 26.0 +- 0.5 band -> out of band -> LLM consulted.
    influx = FakeInflux(current={"ba-f1_temp": 30.0, "ba-f1_rh": 60.0})
    # The model proposes the in-set -0.5 step (matches the candidate).
    llm = FakeLLMClient(
        response_payload=_proposal_payload(snapshot_id=0, delta=-0.5),
        echo_snapshot_id=True,
    )
    supervisor = Supervisor(
        _session_factory(async_engine),
        influx,
        None,
        llm,
        rooms_provider=lambda: [
            RoomTickInput(
                room_id="ba-f1",
                room_context=_room_context("ba-f1"),
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
    assert result.mode is AdjustmentMode.bounded_auto_adjust
    # The guardrail validator applied the change.
    assert len(result.guardrail_decisions) == 1
    assert result.guardrail_decisions[0].applied is True

    session.expire_all()
    # An overlay was auto-applied (source ai_auto) — NO pending approval.
    assert await _count(session, RuntimeAdjustment, room_id="ba-f1") == 1
    overlay = (
        await session.execute(
            select(RuntimeAdjustment).where(
                RuntimeAdjustment.room_id == "ba-f1"
            )
        )
    ).scalar_one()
    assert overlay.param_name == "temp_day"
    assert overlay.delta == pytest.approx(-0.5)
    assert overlay.mode is AdjustmentMode.bounded_auto_adjust

    # A command was enqueued for the resulting effective setpoint.
    assert await _count(session, CommandQueueEntry) == 1
    command = (
        await session.execute(select(CommandQueueEntry))
    ).scalar_one()
    # recipe 26.0 + overlay -0.5 = 25.5 effective setpoint.
    assert command.service_data["value"] == pytest.approx(25.5)

    # The cumulative-delta row recorded the applied delta.
    cumulative = (
        await session.execute(
            select(CumulativeDelta).where(
                CumulativeDelta.room_id == "ba-f1",
                CumulativeDelta.param_name == "temp_day",
            )
        )
    ).scalar_one()
    assert cumulative.sum_delta_24h == pytest.approx(-0.5)

    # A controlled_adjustment audit row records the bounded apply.
    audit_count = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.event_type == AuditEventType.controlled_adjustment,
            AuditEvent.room_id == "ba-f1",
        )
    )
    assert int(audit_count or 0) == 1


async def test_bounded_anti_pattern_proposal_rejects_no_overlay(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """An AP-violating proposal is rejected — no overlay, citing SAT-AC + AP-02.

    The room's AC is saturated (SAT-AC fires from the scripted Influx).
    The model proposes lowering ``temp_day`` — the canonical AP-02
    anti-pattern. The guardrail validator must reject it: NO overlay,
    NO command, and a ``guardrail_rejection`` audit row citing both
    ``SAT-AC`` and ``AP-02``.
    """
    rev_id = await _seed_recipe(session, uid="ba-ap", room_id="ba-f2")
    await _set_stage(async_engine, "ba-f2", "stage_2")

    # Script the fake Influx so SAT-AC fires:
    #  - the AC fan reads high (>= level 3) continuously for 15 min;
    #  - measured temp (30) is above the 26.0 setpoint;
    #  - the temperature slope is positive (still climbing).
    influx = FakeInflux(
        current={"ba-f2_temp": 30.0, "ba-f2_rh": 60.0},
        slope={"ba-f2_temp": 0.01},
        threshold={"ba-f2_ac_fan": True},
    )
    llm = FakeLLMClient(
        response_payload=_proposal_payload(snapshot_id=0, delta=-0.4),
        echo_snapshot_id=True,
    )
    supervisor = Supervisor(
        _session_factory(async_engine),
        influx,
        None,
        llm,
        rooms_provider=lambda: [
            RoomTickInput(
                room_id="ba-f2",
                room_context=_saturated_ac_room_context("ba-f2"),
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
    assert result.mode is AdjustmentMode.bounded_auto_adjust
    assert len(result.guardrail_decisions) == 1
    verdict = result.guardrail_decisions[0]
    assert verdict.rejected is True
    # The rejection cites BOTH the saturation id and the anti-pattern id.
    assert "SAT-AC" in verdict.reason_codes
    assert "AP-02" in verdict.reason_codes

    session.expire_all()
    # CRITICAL: nothing applied — no overlay, no command.
    assert await _count(session, RuntimeAdjustment, room_id="ba-f2") == 0
    assert await _count(session, CommandQueueEntry) == 0

    # A guardrail_rejection audit row citing SAT-AC + AP-02 was written.
    rejection = (
        await session.execute(
            select(AuditEvent).where(
                AuditEvent.event_type
                == AuditEventType.guardrail_rejection,
                AuditEvent.room_id == "ba-f2",
            )
        )
    ).scalar_one()
    assert "SAT-AC" in rejection.reason_codes
    assert "AP-02" in rejection.reason_codes
