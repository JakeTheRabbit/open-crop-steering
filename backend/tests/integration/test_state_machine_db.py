"""Integration tests for :func:`app.core.state_machine.evaluate_room`.

Exercises the event-taxonomy writes against real Postgres: a HEALTHY
room going IMPAIRED writes a ``system_warning`` event; going CRITICAL
writes a ``critical_incident`` event *and* a chained audit row;
recovering to HEALTHY writes an ``info_event``; an unchanged state
writes nothing.

Equipment + tolerance inputs are driven through :class:`FakeInflux` so
the test controls exactly which indicators fire; the DB side
(``event_log`` / ``audit_event`` rows, the HMAC chain) is real.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from app.core.equipment import FiredShot, RoomContext
from app.core.state_machine import RoomState, evaluate_room
from app.influx_client import ThresholdOp
from app.models.audit_event import AuditEvent, AuditEventType
from app.models.event_log import EventLogEntry, EventSeverity
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


class FakeInflux:
    """Scripted InfluxClient double (see ``unit/test_equipment.py``)."""

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

    def trend_slope(
        self, entity: str, window: timedelta | str
    ) -> float | None:
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


def _ac_room(room_id: str) -> RoomContext:
    """Room context wired only for the SAT-AC predicate."""
    return RoomContext(
        room_id=room_id,
        temp_actual_entity=f"{room_id}_temp",
        temp_setpoint=28.0,
        ac_fan_entity=f"{room_id}_ac_fan",
    )


def _ac_saturated_influx(room_id: str) -> FakeInflux:
    """Influx data that makes SAT-AC fire (warning) for ``room_id``."""
    return FakeInflux(
        threshold={f"{room_id}_ac_fan": True},
        current={f"{room_id}_temp": 30.0},
        slope={f"{room_id}_temp": 0.05},
    )


async def _count_events(
    session: AsyncSession,
    room_id: str,
    event_type: AuditEventType,
) -> int:
    """Count ``event_log`` rows of a type for a room."""
    result = await session.execute(
        select(func.count(EventLogEntry.id)).where(
            EventLogEntry.room_id == room_id,
            EventLogEntry.event_type == event_type,
        )
    )
    return int(result.scalar_one())


async def test_healthy_room_writes_no_event(
    session: AsyncSession,
) -> None:
    """A HEALTHY->HEALTHY evaluation writes nothing to the event log."""
    room_id = "sm-healthy-1"
    evaluation = await evaluate_room(
        session,
        FakeInflux(),
        _ac_room(room_id),
        day_index=1,
        current_state=RoomState.healthy,
    )
    await session.flush()

    assert evaluation.state is RoomState.healthy
    assert evaluation.changed is False

    total = await session.execute(
        select(func.count(EventLogEntry.id)).where(
            EventLogEntry.room_id == room_id
        )
    )
    assert total.scalar_one() == 0


async def test_transition_to_impaired_writes_system_warning(
    session: AsyncSession,
) -> None:
    """HEALTHY->IMPAIRED on a warning saturation writes a system_warning."""
    room_id = "sm-impaired-1"
    evaluation = await evaluate_room(
        session,
        _ac_saturated_influx(room_id),
        _ac_room(room_id),
        day_index=1,
        current_state=RoomState.healthy,
    )
    await session.flush()

    assert evaluation.state is RoomState.impaired
    assert evaluation.changed is True
    assert "SAT-AC" in evaluation.reason_codes

    warnings = await _count_events(
        session, room_id, AuditEventType.system_warning
    )
    assert warnings == 1

    # IMPAIRED is operational — no audit-chain row is written for it.
    audit = await session.execute(
        select(func.count(AuditEvent.id)).where(
            AuditEvent.room_id == room_id
        )
    )
    assert audit.scalar_one() == 0


async def test_impaired_event_carries_warning_severity_and_codes(
    session: AsyncSession,
) -> None:
    """The system_warning row carries warning severity + the SAT code."""
    room_id = "sm-impaired-2"
    await evaluate_room(
        session,
        _ac_saturated_influx(room_id),
        _ac_room(room_id),
        day_index=1,
        current_state=RoomState.healthy,
    )
    await session.flush()

    row = await session.execute(
        select(EventLogEntry).where(EventLogEntry.room_id == room_id)
    )
    entry = row.scalar_one()
    assert entry.severity is EventSeverity.warning
    assert entry.event_type is AuditEventType.system_warning
    assert "SAT-AC" in entry.reason_codes
    assert entry.payload["state"] == "impaired"
    assert "SAT-AC" in entry.payload["saturated_indicators"]


def _dew_room(room_id: str) -> RoomContext:
    """Room context wired for the SAT-DEW (critical) predicate."""
    return RoomContext(
        room_id=room_id,
        coldest_surface_entity=f"{room_id}_surface",
        dew_point_entity=f"{room_id}_dew",
    )


def _dew_saturated_influx(room_id: str) -> FakeInflux:
    """Influx data that makes SAT-DEW fire (critical) for ``room_id``."""
    return FakeInflux(
        current={f"{room_id}_surface": 16.0, f"{room_id}_dew": 18.0},
        threshold={f"{room_id}_surface": True},
    )


async def test_transition_to_critical_writes_event_and_audit(
    session: AsyncSession,
) -> None:
    """HEALTHY->CRITICAL writes a critical_incident event + audit row."""
    room_id = "sm-critical-1"
    evaluation = await evaluate_room(
        session,
        _dew_saturated_influx(room_id),
        _dew_room(room_id),
        day_index=1,
        current_state=RoomState.healthy,
    )
    await session.flush()

    assert evaluation.state is RoomState.critical
    assert "SAT-DEW" in evaluation.reason_codes

    events = await _count_events(
        session, room_id, AuditEventType.critical_incident
    )
    assert events == 1

    audit = await session.execute(
        select(AuditEvent).where(
            AuditEvent.room_id == room_id,
            AuditEvent.event_type == AuditEventType.critical_incident,
        )
    )
    audit_row = audit.scalar_one()
    assert "SAT-DEW" in audit_row.reason_codes
    # The chain trigger populated the HMAC.
    assert audit_row.hmac != b"\x00" * 32

    # The event row links back to the audit row.
    event = await session.execute(
        select(EventLogEntry).where(EventLogEntry.room_id == room_id)
    )
    event_row = event.scalar_one()
    assert event_row.audit_event_id == audit_row.id
    assert event_row.severity is EventSeverity.critical


async def test_recovery_to_healthy_writes_info_event(
    session: AsyncSession,
) -> None:
    """IMPAIRED->HEALTHY writes an info_event recovery row."""
    room_id = "sm-recover-1"
    # Influx is clean -> next_state is HEALTHY; previous was IMPAIRED.
    evaluation = await evaluate_room(
        session,
        FakeInflux(),
        _ac_room(room_id),
        day_index=1,
        current_state=RoomState.impaired,
    )
    await session.flush()

    assert evaluation.state is RoomState.healthy
    assert evaluation.changed is True

    info = await _count_events(session, room_id, AuditEventType.info_event)
    assert info == 1

    row = await session.execute(
        select(EventLogEntry).where(EventLogEntry.room_id == room_id)
    )
    entry = row.scalar_one()
    assert entry.severity is EventSeverity.info
    assert "recovered to HEALTHY" in entry.summary


async def test_critical_to_critical_writes_nothing(
    session: AsyncSession,
) -> None:
    """A room already CRITICAL that stays CRITICAL does not re-log."""
    room_id = "sm-critical-stay"
    evaluation = await evaluate_room(
        session,
        _dew_saturated_influx(room_id),
        _dew_room(room_id),
        day_index=1,
        current_state=RoomState.critical,
    )
    await session.flush()

    assert evaluation.state is RoomState.critical
    assert evaluation.changed is False

    total = await session.execute(
        select(func.count(EventLogEntry.id)).where(
            EventLogEntry.room_id == room_id
        )
    )
    assert total.scalar_one() == 0


async def test_full_healthy_impaired_critical_progression(
    session: AsyncSession,
) -> None:
    """Walk a room HEALTHY -> IMPAIRED -> CRITICAL, one event per edge."""
    room_id = "sm-progression"
    ctx_kwargs: dict[str, Any] = {
        "room_id": room_id,
        "temp_actual_entity": f"{room_id}_temp",
        "temp_setpoint": 28.0,
        "ac_fan_entity": f"{room_id}_ac_fan",
        "coldest_surface_entity": f"{room_id}_surface",
        "dew_point_entity": f"{room_id}_dew",
    }
    ctx = RoomContext(**ctx_kwargs)

    # 1. HEALTHY -> HEALTHY: nothing.
    first = await evaluate_room(
        session, FakeInflux(), ctx, day_index=1,
        current_state=RoomState.healthy,
    )
    assert first.state is RoomState.healthy

    # 2. HEALTHY -> IMPAIRED: SAT-AC fires.
    impaired_influx = FakeInflux(
        threshold={f"{room_id}_ac_fan": True},
        current={f"{room_id}_temp": 30.0},
        slope={f"{room_id}_temp": 0.05},
    )
    second = await evaluate_room(
        session, impaired_influx, ctx, day_index=1,
        current_state=first.state,
    )
    assert second.state is RoomState.impaired

    # 3. IMPAIRED -> CRITICAL: SAT-DEW now also fires.
    critical_influx = FakeInflux(
        threshold={
            f"{room_id}_ac_fan": True,
            f"{room_id}_surface": True,
        },
        current={
            f"{room_id}_temp": 30.0,
            f"{room_id}_surface": 16.0,
            f"{room_id}_dew": 18.0,
        },
        slope={f"{room_id}_temp": 0.05},
    )
    third = await evaluate_room(
        session, critical_influx, ctx, day_index=1,
        current_state=second.state,
    )
    assert third.state is RoomState.critical
    await session.flush()

    # One system_warning (the IMPAIRED edge) + one critical_incident.
    assert (
        await _count_events(session, room_id, AuditEventType.system_warning)
        == 1
    )
    assert (
        await _count_events(
            session, room_id, AuditEventType.critical_incident
        )
        == 1
    )


async def test_irrigation_saturation_drives_critical(
    session: AsyncSession,
) -> None:
    """A SAT-IRRIG fire (unresponsive shot) takes the room CRITICAL."""
    room_id = "sm-irrig-crit"
    shot = FiredShot(
        zone_id="z1",
        vwc_entity=f"{room_id}_z1_vwc",
        fired_at="2026-05-16T10:00:00Z",
        volume_ml=200.0,
        substrate_volume_ml=4000.0,
    )
    ctx = RoomContext(room_id=room_id, fired_shots=[shot])
    # VWC barely moved -> SAT-IRRIG fires critical.
    influx = FakeInflux(delta={f"{room_id}_z1_vwc": 0.3})

    evaluation = await evaluate_room(
        session, influx, ctx, day_index=1,
        current_state=RoomState.healthy,
    )
    await session.flush()

    assert evaluation.state is RoomState.critical
    assert "SAT-IRRIG" in evaluation.reason_codes
    assert (
        await _count_events(
            session, room_id, AuditEventType.critical_incident
        )
        == 1
    )
