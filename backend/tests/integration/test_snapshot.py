"""Integration tests for :mod:`app.core.snapshot`.

Exercises :func:`build_snapshot` against real Postgres with a scripted
InfluxDB double and an optional HA double: a :class:`SensorSnapshot` row
is persisted, its payload carries equipment-saturation flags by id,
stale sensors are listed, the HA WS value is preferred over Influx, and
the recent-audit / open-pending counts are folded in.
"""

from __future__ import annotations

import datetime as dt
from datetime import timedelta
from typing import Any

import pytest
from app.core.equipment import RoomContext
from app.core.snapshot import SnapshotRequest, build_snapshot
from app.influx_client import ThresholdOp
from app.models.pending_approval import PendingApproval, PendingStatus
from app.models.sensor_snapshot import SensorSnapshot
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


class FakeInflux:
    """Scripted InfluxClient double (mirrors unit/test_equipment.py)."""

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


class FakeHA:
    """HA client double — returns scripted state objects by entity id."""

    def __init__(self, states: dict[str, Any]) -> None:
        self._states = states
        self.calls: list[str] = []

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        self.calls.append(entity_id)
        if entity_id not in self._states:
            raise RuntimeError(f"entity not found: {entity_id}")
        return {"state": self._states[entity_id]}


def _room_context(room_id: str = "f1") -> RoomContext:
    """A room context with the AC + RH sensors mapped."""
    return RoomContext(
        room_id=room_id,
        temp_actual_entity="f1_temp",
        temp_setpoint=26.0,
        ac_fan_entity="f1_ac_fan",
        rh_actual_entity="f1_rh",
        rh_setpoint=60.0,
        dehu_switch_entity="f1_dehu",
    )


async def test_build_snapshot_persists_a_row(session: AsyncSession) -> None:
    influx = FakeInflux(current={"f1_temp": 26.5, "f1_rh": 61.0})
    snapshot = await build_snapshot(
        session,
        influx,
        None,
        SnapshotRequest(
            room_id="f1",
            room_context=_room_context(),
            recipe_revision_id=7,
            cycle_day=30,
            rollout_stage="report_only",
        ),
    )
    assert snapshot.id is not None

    reloaded = await session.get(SensorSnapshot, snapshot.id)
    assert reloaded is not None
    assert reloaded.room_id == "f1"
    assert reloaded.recipe_revision_id == 7
    assert reloaded.cycle_day == 30
    assert reloaded.rollout_stage == "report_only"


async def test_snapshot_payload_carries_equipment_flags_by_id(
    session: AsyncSession,
) -> None:
    # Drive SAT-AC: fan high, temp above setpoint, climbing.
    influx = FakeInflux(
        current={"f1_temp": 28.5, "f1_rh": 61.0},
        slope={"f1_temp": 0.02},
        threshold={"f1_ac_fan": True},
    )
    snapshot = await build_snapshot(
        session,
        influx,
        None,
        SnapshotRequest(
            room_id="f1",
            room_context=_room_context(),
            recipe_revision_id=1,
            cycle_day=1,
            rollout_stage="report_only",
        ),
    )
    equipment = snapshot.payload["equipment_status"]
    # AC is saturated; flagged by its SAT id keyed under "ac".
    assert equipment["ac"] == "SAT-AC"
    # Dehu is mapped + evaluated but not saturated.
    assert equipment["dehu"] == "ok"
    # CO2 / fans / pressure / condensation not mapped -> unmonitored.
    assert equipment["co2"] == "unmonitored"
    assert equipment["pressure"] == "unmonitored"
    assert "SAT-AC" in snapshot.payload["saturated_indicators"]


async def test_snapshot_lists_stale_sensors(session: AsyncSession) -> None:
    # f1_temp has a value; f1_rh / f1_ac_fan / f1_dehu do not.
    influx = FakeInflux(current={"f1_temp": 26.5})
    snapshot = await build_snapshot(
        session,
        influx,
        None,
        SnapshotRequest(
            room_id="f1",
            room_context=_room_context(),
            recipe_revision_id=1,
            cycle_day=1,
            rollout_stage="report_only",
        ),
    )
    stale = set(snapshot.payload["stale_sensors"])
    assert "f1_rh" in stale
    assert "f1_temp" not in stale


async def test_snapshot_prefers_ha_ws_value_over_influx(
    session: AsyncSession,
) -> None:
    # HA reports 27.0; Influx reports 26.5 — HA WS must win.
    influx = FakeInflux(current={"f1_temp": 26.5, "f1_rh": 61.0})
    ha = FakeHA({"sensor.f1_temp": "27.0"})
    snapshot = await build_snapshot(
        session,
        influx,
        ha,
        SnapshotRequest(
            room_id="f1",
            room_context=_room_context(),
            recipe_revision_id=1,
            cycle_day=1,
            rollout_stage="report_only",
        ),
    )
    temp_reading = snapshot.payload["sensors"]["f1_temp"]
    assert temp_reading["value"] == 27.0
    assert temp_reading["source"] == "ha_ws"
    # f1_rh not in HA -> Influx fallback.
    rh_reading = snapshot.payload["sensors"]["f1_rh"]
    assert rh_reading["value"] == 61.0
    assert rh_reading["source"] == "influx"


async def test_snapshot_includes_open_pending_count(
    session: AsyncSession,
) -> None:
    session.add(
        PendingApproval(
            room_id="f1",
            proposal={"x": 1},
            snapshot_id=1,
            summary="pending one",
            status=PendingStatus.open,
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        )
    )
    await session.flush()

    influx = FakeInflux(current={"f1_temp": 26.5, "f1_rh": 61.0})
    snapshot = await build_snapshot(
        session,
        influx,
        None,
        SnapshotRequest(
            room_id="f1",
            room_context=_room_context(),
            recipe_revision_id=1,
            cycle_day=1,
            rollout_stage="report_only",
        ),
    )
    assert snapshot.payload["open_pending_count"] == 1


async def test_snapshot_recent_audit_summary_shape(
    session: AsyncSession,
) -> None:
    influx = FakeInflux(current={"f1_temp": 26.5, "f1_rh": 61.0})
    snapshot = await build_snapshot(
        session,
        influx,
        None,
        SnapshotRequest(
            room_id="f1",
            room_context=_room_context(),
            recipe_revision_id=1,
            cycle_day=1,
            rollout_stage="report_only",
        ),
    )
    audit = snapshot.payload["recent_audit"]
    assert set(audit) == {"window_hours", "count", "by_type", "recent"}
    assert audit["count"] == 0
