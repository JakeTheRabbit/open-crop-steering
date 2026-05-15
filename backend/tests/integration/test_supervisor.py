"""Integration tests for :mod:`app.workers.supervisor` — Report mode.

Drives a full supervisor tick against real Postgres with a
:class:`FakeLLMClient` plus scripted Influx / HA doubles. Covered:

* A HEALTHY, in-band room records a nominal assessment and **does not**
  call the LLM.
* An out-of-band room calls the LLM, writes an ``llm_call_log`` row, and
  — Report mode — writes **no** ``runtime_adjustment`` overlay and **no**
  ``command_queue`` row.
* Skip rules: a muted / paused / lights-off / not-HEALTHY room and a
  room inside a no-touch window are all skipped before any snapshot.
* The key behavioural test: a snapshot whose ``equipment_status.ac`` is
  ``SAT-AC`` paired with an LLM decision that returns
  ``proposed_changes=[]`` and ``reason_codes=["SAT-AC","AP-02"]`` — the
  supervisor records that decision intact and still writes no control
  change.
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
from app.core.equipment import RoomContext
from app.core.no_touch import NoTouchWindow
from app.core.room_runtime import get_or_create
from app.influx_client import ThresholdOp
from app.llm_client import FakeLLMClient, LLMCompletion
from app.models.command_queue import CommandQueueEntry
from app.models.event_log import EventLogEntry
from app.models.llm_call_log import LLMCallLog, LLMCallOutcome
from app.models.recipe_revision import (
    RecipeRevision,
    RecipeRevisionParam,
    RecipeStatus,
)
from app.models.room_runtime import RoomRuntime
from app.models.runtime_adjustment import RuntimeAdjustment
from app.models.sensor_snapshot import SensorSnapshot
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
    """Clear globally-scanned tables — the supervisor commits its own sessions.

    The conftest ``session`` fixture isolates by rollback, but the
    supervisor commits through its *own* sessions and the seed helper
    here commits an approved recipe. Those committed rows leak across
    tests, so this autouse fixture truncates everything the supervisor
    tests touch (including the recipe / user / effective-target chain)
    before each test and refreshes the now-empty matview.
    """
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
            # table name comes from a hardcoded literal tuple, not input
            await cleanup.execute(text(f"DELETE FROM {table}"))  # noqa: S608
        # recipe_revision_param has an immutability trigger that blocks
        # DELETE while the parent revision is 'approved'. Move parents to
        # 'superseded' first (the one transition the trigger permits),
        # then the params + revisions delete cleanly.
        await cleanup.execute(
            text(
                "UPDATE recipe_revision SET status = 'superseded' "
                "WHERE status = 'approved'"
            )
        )
        await cleanup.execute(text("DELETE FROM recipe_revision_param"))
        await cleanup.execute(text("DELETE FROM recipe_revision"))
        # Users this module seeds (FK-referenced by recipe_revision).
        await cleanup.execute(
            text("DELETE FROM users WHERE id LIKE 'u-%'")
        )
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


def _room_context(room_id: str = "f1") -> RoomContext:
    """Room context with AC + RH sensors mapped (so SAT-AC can be driven)."""
    return RoomContext(
        room_id=room_id,
        temp_actual_entity="f1_temp",
        temp_setpoint=26.0,
        ac_fan_entity="f1_ac_fan",
        rh_actual_entity="f1_rh",
        rh_setpoint=60.0,
        dehu_switch_entity="f1_dehu",
    )


async def _seed_recipe(
    session: AsyncSession,
    *,
    uid: str,
    room_id: str,
    day_index: int,
    params: dict[str, tuple[float, float]],
) -> int:
    """Seed an approved recipe + refresh the matview. params: name->(value,tol)."""
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
    for param_name, (value, tol) in params.items():
        session.add(
            RecipeRevisionParam(
                recipe_revision_id=rev.id,
                room_id=room_id,
                day_index=day_index,
                param_name=param_name,
                value=value,
                tolerance=tol,
            )
        )
    await session.commit()
    await refresh_effective_targets(session)
    await session.commit()
    return rev.id


def _decision_payload(snapshot_id: int, **over: Any) -> dict[str, Any]:
    """Build a valid ocs.llm_decision.v1 payload echoing ``snapshot_id``."""
    base: dict[str, Any] = {
        "schema_version": LLM_DECISION_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "assessment": "Room is drifting warm.",
        "recommended_action_id": None,
        "proposed_changes": [],
        "confidence": 0.7,
        "reason_codes": [],
        "human_summary": "Temperature is above target; monitoring.",
        "requires_human": False,
    }
    base.update(over)
    return base


async def _count(session: AsyncSession, model: Any, **filters: Any) -> int:
    """Count rows of ``model`` matching ``filters``."""
    stmt = select(func.count()).select_from(model)
    for col, value in filters.items():
        stmt = stmt.where(getattr(model, col) == value)
    return int((await session.execute(stmt)).scalar_one())


# --------------------------------------------------------------------- skip tests


async def test_muted_room_is_skipped(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    async with _session_factory(async_engine)() as setup:
        runtime = await get_or_create(setup, "f1")
        runtime.muted = True
        await setup.commit()

    llm = FakeLLMClient(response_payload=_decision_payload(1))
    supervisor = Supervisor(
        _session_factory(async_engine),
        FakeInflux(),
        None,
        llm,
        rooms_provider=lambda: [
            RoomTickInput(room_id="f1", room_context=_room_context(), recipe_revision_id=1)
        ],
    )
    results = await supervisor.tick()
    assert len(results) == 1
    assert results[0].skipped is True
    assert results[0].skip_reason == "muted"
    assert llm.calls == []


async def test_paused_room_is_skipped(async_engine: AsyncEngine) -> None:
    async with _session_factory(async_engine)() as setup:
        runtime = await get_or_create(setup, "f1")
        runtime.paused = True
        await setup.commit()

    llm = FakeLLMClient(response_payload=_decision_payload(1))
    supervisor = Supervisor(
        _session_factory(async_engine),
        FakeInflux(),
        None,
        llm,
        rooms_provider=lambda: [
            RoomTickInput(room_id="f1", room_context=_room_context(), recipe_revision_id=1)
        ],
    )
    results = await supervisor.tick()
    assert results[0].skip_reason == "paused"
    assert llm.calls == []


async def test_lights_off_room_is_skipped(async_engine: AsyncEngine) -> None:
    llm = FakeLLMClient(response_payload=_decision_payload(1))
    supervisor = Supervisor(
        _session_factory(async_engine),
        FakeInflux(),
        None,
        llm,
        rooms_provider=lambda: [
            RoomTickInput(
                room_id="f1",
                room_context=_room_context(),
                recipe_revision_id=1,
                lights_on=False,
            )
        ],
    )
    results = await supervisor.tick()
    assert results[0].skip_reason == "lights_off"
    assert llm.calls == []


async def test_not_healthy_room_is_skipped(async_engine: AsyncEngine) -> None:
    async with _session_factory(async_engine)() as setup:
        runtime = await get_or_create(setup, "f1")
        runtime.current_state = "impaired"
        await setup.commit()

    llm = FakeLLMClient(response_payload=_decision_payload(1))
    supervisor = Supervisor(
        _session_factory(async_engine),
        FakeInflux(),
        None,
        llm,
        rooms_provider=lambda: [
            RoomTickInput(room_id="f1", room_context=_room_context(), recipe_revision_id=1)
        ],
    )
    results = await supervisor.tick()
    assert results[0].skip_reason.startswith("not_healthy")
    assert llm.calls == []


async def test_no_touch_window_skips_room(async_engine: AsyncEngine) -> None:
    llm = FakeLLMClient(response_payload=_decision_payload(1))
    # A window that covers the fixed tick time below.
    supervisor = Supervisor(
        _session_factory(async_engine),
        FakeInflux(),
        None,
        llm,
        rooms_provider=lambda: [
            RoomTickInput(room_id="f1", room_context=_room_context(), recipe_revision_id=1)
        ],
        no_touch_windows=[NoTouchWindow(start="00:00", end="23:59")],
        now_provider=lambda: dt.datetime(2026, 5, 16, 12, 0, tzinfo=dt.UTC),
    )
    results = await supervisor.tick()
    assert results[0].skip_reason == "no_touch_window"
    assert llm.calls == []


# --------------------------------------------------------------------- in-band


async def test_in_band_room_skips_the_llm(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A HEALTHY room with every parameter in band records nominal, no LLM."""
    rev_id = await _seed_recipe(
        session,
        uid="u-inband",
        room_id="f1",
        day_index=1,
        params={"temp_day": (26.0, 0.5)},
    )
    # f1_temp == 26.0 == target -> in band.
    influx = FakeInflux(current={"f1_temp": 26.0, "f1_rh": 60.0})
    llm = FakeLLMClient(response_payload=_decision_payload(1))
    supervisor = Supervisor(
        _session_factory(async_engine),
        influx,
        None,
        llm,
        rooms_provider=lambda: [
            RoomTickInput(
                room_id="f1",
                room_context=_room_context(),
                recipe_revision_id=rev_id,
            )
        ],
        now_provider=lambda: dt.datetime(2026, 5, 16, 12, 0, tzinfo=dt.UTC),
    )
    results = await supervisor.tick()
    assert results[0].in_band is True
    assert results[0].llm_called is False
    assert llm.calls == []

    # A nominal info_event was logged; no llm_call_log row.
    session.expire_all()
    assert await _count(session, EventLogEntry, room_id="f1") == 1
    assert await _count(session, LLMCallLog, room_id="f1") == 0


# --------------------------------------------------------------------- out-of-band


async def test_out_of_band_room_calls_llm_and_writes_no_control(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """An out-of-band room calls the LLM + logs it, writes no overlay/command."""
    rev_id = await _seed_recipe(
        session,
        uid="u-oob",
        room_id="f1",
        day_index=1,
        params={"temp_day": (26.0, 0.5)},
    )
    # f1_temp far above the 26.0 +- 0.5 band -> major drift.
    influx = FakeInflux(current={"f1_temp": 30.0, "f1_rh": 60.0})

    # echo_snapshot_id=True -> the fake echoes the real snapshot id the
    # supervisor built, so the strict parser's echo check passes.
    llm = FakeLLMClient(
        response_payload=_decision_payload(snapshot_id=0),
        echo_snapshot_id=True,
    )
    supervisor = Supervisor(
        _session_factory(async_engine),
        influx,
        None,
        llm,
        rooms_provider=lambda: [
            RoomTickInput(
                room_id="f1",
                room_context=_room_context(),
                recipe_revision_id=rev_id,
            )
        ],
        now_provider=lambda: dt.datetime(2026, 5, 16, 12, 0, tzinfo=dt.UTC),
    )
    results = await supervisor.tick()
    assert results[0].in_band is False
    assert results[0].llm_called is True
    assert results[0].outcome is LLMCallOutcome.parsed
    assert len(llm.calls) == 1

    session.expire_all()
    # The llm_call_log row exists and parsed cleanly.
    call = (
        await session.execute(select(LLMCallLog).where(LLMCallLog.room_id == "f1"))
    ).scalar_one()
    assert call.outcome is LLMCallOutcome.parsed
    assert call.response_parsed is not None
    assert call.snapshot_id is not None

    # Report mode: NO overlay, NO command queued.
    assert await _count(session, RuntimeAdjustment, room_id="f1") == 0
    assert await _count(session, CommandQueueEntry) == 0
    # A snapshot was persisted.
    assert await _count(session, SensorSnapshot, room_id="f1") == 1


async def test_supervisor_records_sat_ac_decision_intact_no_control(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """Key behavioural test — SAT-AC + AP-02 report, no control change.

    The snapshot is built so ``equipment_status.ac == "SAT-AC"`` (AC fan
    high, temp above setpoint, climbing). The FakeLLMClient returns a
    decision with ``proposed_changes=[]`` and
    ``reason_codes=["SAT-AC","AP-02"]`` — exactly the report the system
    prompt instructs for a saturated AC. The supervisor must store that
    decision intact and still write no overlay / no command.
    """
    rev_id = await _seed_recipe(
        session,
        uid="u-satac",
        room_id="f1",
        day_index=1,
        params={"temp_day": (26.0, 0.5)},
    )
    # SAT-AC: fan high (threshold), temp above setpoint + above band,
    # and climbing (positive slope).
    influx = FakeInflux(
        current={"f1_temp": 29.5, "f1_rh": 60.0},
        slope={"f1_temp": 0.03},
        threshold={"f1_ac_fan": True},
    )

    sat_decision = _decision_payload(
        snapshot_id=1,
        assessment="Room is above temp target but the AC is saturated.",
        recommended_action_id=None,
        proposed_changes=[],
        confidence=0.85,
        reason_codes=["SAT-AC", "AP-02"],
        human_summary=(
            "The AC is running flat-out and still losing ground (SAT-AC). "
            "Lowering the temperature setpoint would be AP-02 — useless on "
            "a saturated unit. This is a hardware-capacity constraint, not "
            "a setpoint issue."
        ),
        requires_human=True,
    )
    llm = FakeLLMClient(response_payload=sat_decision, echo_snapshot_id=True)
    supervisor = Supervisor(
        _session_factory(async_engine),
        influx,
        None,
        llm,
        rooms_provider=lambda: [
            RoomTickInput(
                room_id="f1",
                room_context=_room_context(),
                recipe_revision_id=rev_id,
            )
        ],
        now_provider=lambda: dt.datetime(2026, 5, 16, 12, 0, tzinfo=dt.UTC),
    )
    results = await supervisor.tick()
    assert results[0].llm_called is True
    assert results[0].outcome is LLMCallOutcome.parsed

    session.expire_all()

    # The snapshot itself flagged the AC as saturated.
    snapshot = (
        await session.execute(
            select(SensorSnapshot).where(SensorSnapshot.room_id == "f1")
        )
    ).scalar_one()
    assert snapshot.payload["equipment_status"]["ac"] == "SAT-AC"

    # The decision was stored intact — empty proposed_changes, both codes.
    call = (
        await session.execute(select(LLMCallLog).where(LLMCallLog.room_id == "f1"))
    ).scalar_one()
    assert call.outcome is LLMCallOutcome.parsed
    parsed = call.response_parsed
    assert parsed is not None
    assert parsed["proposed_changes"] == []
    assert parsed["reason_codes"] == ["SAT-AC", "AP-02"]

    # The event_log row carries the reason codes for the dashboard / digest.
    report_event = (
        await session.execute(
            select(EventLogEntry).where(EventLogEntry.room_id == "f1")
        )
    ).scalar_one()
    assert set(report_event.reason_codes) == {"SAT-AC", "AP-02"}

    # Still report-only — absolutely no control change.
    assert await _count(session, RuntimeAdjustment, room_id="f1") == 0
    assert await _count(session, CommandQueueEntry) == 0


async def test_malformed_llm_response_logged_as_degraded_no_control(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """A malformed LLM reply is logged schema_invalid; still no control change."""
    rev_id = await _seed_recipe(
        session,
        uid="u-bad",
        room_id="f1",
        day_index=1,
        params={"temp_day": (26.0, 0.5)},
    )
    influx = FakeInflux(current={"f1_temp": 30.0, "f1_rh": 60.0})
    # FakeLLMClient returns raw non-JSON content.
    llm = FakeLLMClient(
        completion=LLMCompletion(
            content="sorry, I cannot help with that",
            model="fake-model",
            latency_ms=3,
        )
    )
    supervisor = Supervisor(
        _session_factory(async_engine),
        influx,
        None,
        llm,
        rooms_provider=lambda: [
            RoomTickInput(
                room_id="f1",
                room_context=_room_context(),
                recipe_revision_id=rev_id,
            )
        ],
        now_provider=lambda: dt.datetime(2026, 5, 16, 12, 0, tzinfo=dt.UTC),
    )
    results = await supervisor.tick()
    assert results[0].outcome is LLMCallOutcome.schema_invalid

    session.expire_all()
    call = (
        await session.execute(select(LLMCallLog).where(LLMCallLog.room_id == "f1"))
    ).scalar_one()
    assert call.outcome is LLMCallOutcome.schema_invalid
    assert call.validator_errors  # non-empty
    assert await _count(session, RuntimeAdjustment, room_id="f1") == 0
    assert await _count(session, CommandQueueEntry) == 0


async def test_tick_marks_room_runtime_ticked(
    async_engine: AsyncEngine, session: AsyncSession
) -> None:
    """Every processed room gets its ``last_tick_at`` stamped."""
    rev_id = await _seed_recipe(
        session,
        uid="u-tick",
        room_id="f1",
        day_index=1,
        params={"temp_day": (26.0, 0.5)},
    )
    influx = FakeInflux(current={"f1_temp": 26.0, "f1_rh": 60.0})
    tick_time = dt.datetime(2026, 5, 16, 12, 0, tzinfo=dt.UTC)
    supervisor = Supervisor(
        _session_factory(async_engine),
        influx,
        None,
        FakeLLMClient(response_payload=_decision_payload(1)),
        rooms_provider=lambda: [
            RoomTickInput(
                room_id="f1",
                room_context=_room_context(),
                recipe_revision_id=rev_id,
            )
        ],
        now_provider=lambda: tick_time,
    )
    await supervisor.tick()

    session.expire_all()
    runtime = await session.get(RoomRuntime, "f1")
    assert runtime is not None
    assert runtime.last_tick_at == tick_time
