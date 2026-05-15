"""Snapshot builder — assembles the supervisor's sole LLM input.

A *snapshot* is the frozen picture of one room at one supervisor tick:
current sensor values, recent InfluxDB trends, equipment-saturation
flags, the stale-sensor list, a recent-audit summary, the open-pending
count, the rollout stage, the cycle day and the active recipe revision.

Why it matters architecturally:

* The snapshot is the **only** thing the LLM is given to reason about —
  the model never queries HA / InfluxDB itself.
* It is persisted as a :class:`~app.models.sensor_snapshot.SensorSnapshot`
  row with a stable ``snapshot_id``.
* Every LLM proposal echoes that ``snapshot_id`` back; the strict parser
  (:func:`app.llm_client.parse_decision`) rejects a proposal whose
  echoed id does not match the snapshot the call was made for. That
  closes the "AI reasoned about state A but state has since shifted to
  B" race (plan risk #4).

Current-state sourcing follows the locked decision: the Home Assistant
WebSocket API is the authoritative source for current actuator/sensor
state; InfluxDB is the fallback (and the only source of *trend* data).
:func:`build_snapshot` therefore tries HA first per entity and falls
back to ``influx.current_value`` when HA has no usable value.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import desc, func, select

from app.core.equipment import RoomContext, evaluate_all
from app.models.audit_event import AuditEvent
from app.models.pending_approval import PendingApproval, PendingStatus
from app.models.sensor_snapshot import SensorSnapshot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.equipment import _InfluxLike

log = structlog.get_logger(__name__)

#: Schema version embedded in every snapshot payload. Bumped if the
#: payload shape changes so old snapshots remain self-describing.
SNAPSHOT_SCHEMA_VERSION = "ocs.snapshot.v1"

#: How far back the recent-audit summary looks.
_AUDIT_WINDOW = timedelta(hours=24)

#: Max recent-audit rows folded into the summary.
_AUDIT_LIMIT = 20

#: Default trend window for InfluxDB slope queries.
_TREND_WINDOW = timedelta(minutes=30)

#: Reading older than this (vs the tick time) marks a sensor stale.
STALE_AFTER = timedelta(minutes=15)

#: Equipment-id -> the saturation indicator that flags it. Keys are the
#: stable equipment ids used in ``payload["equipment_status"]``.
_EQUIPMENT_INDICATOR: dict[str, str] = {
    "ac": "SAT-AC",
    "dehu": "SAT-DEHU",
    "co2": "SAT-CO2",
    "fan": "SAT-FAN",
    "irrigation": "SAT-IRRIG",
    "pressure": "SAT-PRESS",
    "condensation": "SAT-DEW",
}


@dataclass(slots=True)
class SensorReading:
    """One sensor's current value plus its trend, for the snapshot payload.

    Attributes:
        entity: InfluxDB ``entity_id`` tag (bare, no domain prefix).
        value: Current value — HA WS preferred, InfluxDB fallback.
        source: ``"ha_ws"`` or ``"influx"`` (or ``"none"`` if unavailable).
        trend_slope: Least-squares slope over the trend window (units/s),
            or ``None`` if InfluxDB had too little data.
        stale: ``True`` if the value could not be sourced fresh.
    """

    entity: str
    value: float | None
    source: str
    trend_slope: float | None
    stale: bool


@dataclass(slots=True)
class SnapshotRequest:
    """Inputs needed to build a snapshot for one room.

    Attributes:
        room_id: Room being snapshotted.
        room_context: The room's equipment + sensor map, used both to
            drive saturation predicates and to enumerate the sensors
            whose current value + trend go into the payload.
        recipe_revision_id: Active approved recipe revision for the room.
        cycle_day: Current cultivation cycle day.
        rollout_stage: The room's rollout-stage name.
        sensor_entities: Extra ``entity_id`` tags to capture beyond the
            ones implied by ``room_context`` (optional).
        captured_at: Tick time the snapshot represents (defaults to now).
    """

    room_id: str
    room_context: RoomContext
    recipe_revision_id: int
    cycle_day: int
    rollout_stage: str
    sensor_entities: Sequence[str] = field(default_factory=tuple)
    captured_at: dt.datetime | None = None


def _room_context_entities(ctx: RoomContext) -> list[str]:
    """Collect every ``entity_id`` tag named on a :class:`RoomContext`."""
    singles = [
        ctx.temp_actual_entity,
        ctx.ac_fan_entity,
        ctx.rh_actual_entity,
        ctx.dehu_switch_entity,
        ctx.co2_actual_entity,
        ctx.co2_solenoid_entity,
        ctx.pressure_diff_entity,
        ctx.coldest_surface_entity,
        ctx.dew_point_entity,
    ]
    entities: list[str] = [e for e in singles if e]
    entities.extend(ctx.fan_intensity_entities)
    for top, bottom in ctx.stratification_entities:
        entities.extend((top, bottom))
    # de-dup, order-stable
    seen: dict[str, None] = {}
    for e in entities:
        seen.setdefault(e, None)
    return list(seen)


async def _ha_current_value(ha: Any, entity_tag: str) -> float | None:
    """Best-effort current value for an Influx tag, via the HA WS/REST API.

    HA entity ids carry a domain prefix (``sensor.f1_temp``) whereas the
    InfluxDB tag is bare (``f1_temp``). We try the ``sensor.`` domain —
    by far the most common for the values in a snapshot — and treat any
    failure, missing entity, or non-numeric state as "HA has nothing",
    so the caller falls back to InfluxDB.

    Args:
        ha: Home Assistant client (anything exposing ``get_state``).
        entity_tag: The bare InfluxDB ``entity_id`` tag.

    Returns:
        The numeric HA state, or ``None`` if unavailable.
    """
    if ha is None or not hasattr(ha, "get_state"):
        return None
    try:
        state = await ha.get_state(f"sensor.{entity_tag}")
    except Exception:  # any HA failure -> fall back to Influx
        log.debug("snapshot_ha_lookup_failed", entity=entity_tag)
        return None
    raw = (state or {}).get("state")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def _read_sensor(
    influx: _InfluxLike,
    ha: Any,
    entity: str,
    *,
    trend_window: timedelta,
) -> SensorReading:
    """Build a :class:`SensorReading` — HA WS value preferred, Influx fallback."""
    ha_value = await _ha_current_value(ha, entity)
    if ha_value is not None:
        value: float | None = ha_value
        source = "ha_ws"
    else:
        value = influx.current_value(entity)
        source = "influx" if value is not None else "none"

    slope = influx.trend_slope(entity, trend_window)
    stale = value is None
    return SensorReading(
        entity=entity,
        value=value,
        source=source,
        trend_slope=slope,
        stale=stale,
    )


async def _equipment_status(
    influx: _InfluxLike, ctx: RoomContext
) -> tuple[dict[str, str], list[str]]:
    """Run every saturation predicate; return the by-id status + fired codes.

    Returns:
        ``(equipment_status, saturated_codes)`` where ``equipment_status``
        maps each stable equipment id to either its ``SAT-*`` code (when
        the predicate fired) or ``"ok"`` (evaluated, not saturated) or
        ``"unmonitored"`` (the room lacks the sensors). ``saturated_codes``
        is the list of ``SAT-*`` ids that fired.
    """
    results = await evaluate_all(influx, ctx)
    by_indicator = {r.indicator_id: r for r in results}

    status: dict[str, str] = {}
    saturated: list[str] = []
    for equip_id, indicator in _EQUIPMENT_INDICATOR.items():
        result = by_indicator.get(indicator)
        if result is None or not result.evaluated:
            status[equip_id] = "unmonitored"
        elif result.saturated:
            status[equip_id] = result.indicator_id
            saturated.append(result.indicator_id)
        else:
            status[equip_id] = "ok"
    return status, saturated


async def _recent_audit_summary(
    session: AsyncSession, room_id: str, *, now: dt.datetime
) -> dict[str, Any]:
    """Summarise the room's audit activity over the trailing window."""
    since = now - _AUDIT_WINDOW
    rows = (
        await session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.room_id == room_id,
                AuditEvent.occurred_at >= since,
            )
            .order_by(desc(AuditEvent.occurred_at))
            .limit(_AUDIT_LIMIT)
        )
    ).scalars().all()

    by_type: dict[str, int] = {}
    recent: list[dict[str, Any]] = []
    for row in rows:
        key = row.event_type.value
        by_type[key] = by_type.get(key, 0) + 1
        recent.append(
            {
                "id": row.id,
                "event_type": key,
                "occurred_at": (
                    row.occurred_at.isoformat() if row.occurred_at else None
                ),
                "summary": row.summary,
                "reason_codes": list(row.reason_codes or []),
            }
        )
    return {
        "window_hours": int(_AUDIT_WINDOW.total_seconds() // 3600),
        "count": len(rows),
        "by_type": by_type,
        "recent": recent,
    }


async def _open_pending_count(session: AsyncSession, room_id: str) -> int:
    """Count the room's currently-open pending approvals."""
    result = await session.execute(
        select(func.count())
        .select_from(PendingApproval)
        .where(
            PendingApproval.room_id == room_id,
            PendingApproval.status == PendingStatus.open,
        )
    )
    return int(result.scalar_one())


async def build_snapshot(
    session: AsyncSession,
    influx: _InfluxLike,
    ha: Any,
    request: SnapshotRequest,
) -> SensorSnapshot:
    """Assemble and persist the room snapshot the supervisor feeds the LLM.

    Steps:

    1. **Current state + trends** — for every sensor named on the
       :class:`RoomContext` (plus any extra ``sensor_entities``) read the
       current value (HA WS preferred, InfluxDB fallback) and the
       InfluxDB trend slope.
    2. **Equipment status** — run all seven saturation predicates and
       record the by-equipment-id status (``SAT-*`` / ``ok`` /
       ``unmonitored``).
    3. **Stale sensors** — any sensor whose current value could not be
       sourced.
    4. **Recent audit summary** + **open-pending count** for the room.
    5. Build the payload, persist a :class:`SensorSnapshot` row, return
       it (with its ``id`` populated).

    Args:
        session: Active async session — a ``SensorSnapshot`` row is added
            and flushed (the caller commits).
        influx: InfluxDB client (or compatible double) — current-value
            fallback + trend source.
        ha: Home Assistant client (or compatible double / ``None``) —
            preferred current-value source.
        request: The :class:`SnapshotRequest` describing the room.

    Returns:
        The persisted :class:`SensorSnapshot` (its ``id`` is the
        ``snapshot_id`` proposals must echo).
    """
    now = request.captured_at or dt.datetime.now(dt.UTC)
    ctx = request.room_context

    entities: list[str] = _room_context_entities(ctx)
    for extra in request.sensor_entities:
        if extra not in entities:
            entities.append(extra)

    readings: dict[str, dict[str, Any]] = {}
    stale_sensors: list[str] = []
    for entity in entities:
        reading = await _read_sensor(
            influx, ha, entity, trend_window=_TREND_WINDOW
        )
        readings[entity] = {
            "value": reading.value,
            "source": reading.source,
            "trend_slope": reading.trend_slope,
            "stale": reading.stale,
        }
        if reading.stale:
            stale_sensors.append(entity)

    equipment_status, saturated_codes = await _equipment_status(influx, ctx)
    audit_summary = await _recent_audit_summary(session, request.room_id, now=now)
    open_pending = await _open_pending_count(session, request.room_id)

    payload: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "room_id": request.room_id,
        "captured_at": now.isoformat(),
        "cycle_day": request.cycle_day,
        "rollout_stage": request.rollout_stage,
        "recipe_revision_id": request.recipe_revision_id,
        "setpoints": {
            "temp": ctx.temp_setpoint,
            "rh": ctx.rh_setpoint,
            "co2": ctx.co2_setpoint,
        },
        "sensors": readings,
        "equipment_status": equipment_status,
        "saturated_indicators": saturated_codes,
        "stale_sensors": stale_sensors,
        "recent_audit": audit_summary,
        "open_pending_count": open_pending,
        "trend_window_minutes": int(_TREND_WINDOW.total_seconds() // 60),
    }

    snapshot = SensorSnapshot(
        room_id=request.room_id,
        captured_at=now,
        payload=payload,
        recipe_revision_id=request.recipe_revision_id,
        cycle_day=request.cycle_day,
        rollout_stage=request.rollout_stage,
    )
    session.add(snapshot)
    await session.flush()
    await session.refresh(snapshot)

    # Stamp the assigned id into the payload so the LLM sees the
    # ``snapshot_id`` it must echo back. Reassign the dict (not mutate in
    # place) so SQLAlchemy registers the JSONB change.
    snapshot.payload = {**snapshot.payload, "snapshot_id": snapshot.id}
    await session.flush()

    log.info(
        "snapshot_built",
        room_id=request.room_id,
        snapshot_id=snapshot.id,
        sensors=len(readings),
        stale=len(stale_sensors),
        saturated=saturated_codes,
        open_pending=open_pending,
    )
    return snapshot
