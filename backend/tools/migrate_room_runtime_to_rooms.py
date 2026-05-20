"""One-shot data move: legacy ``room_runtime.equipment_map`` → Convex rooms.

Not part of the product. Not an Alembic migration. The legacy
``room_runtime.equipment_map`` JSONB column has been the entity
picker's persistence shape since v0.1; pass 5b rewires the picker
onto the new relational tables (``buildings`` → ``rooms`` →
``sensors`` / ``equipment``). This script is the one-time data move
that copies what's already in ``equipment_map`` into those tables so
the operator's hand-picked HA entities aren't lost.

It is intentionally **a tool, not a migration**:

* It touches data the user owns (F2's specific entities) — schema
  migrations should never paw at user-owned content.
* It must be re-runnable safely so an admin can re-import after fixing
  a row by hand.
* Migrations run on every deploy; this should run **once**, manually,
  after admin review.

Run it from inside the running container (so DATABASE_URL et al. are
already in the env)::

    docker exec ocs sh -c \\
        'cd /opt/ocs && python -m tools.migrate_room_runtime_to_rooms'

Or locally::

    cd backend && ../.venv/Scripts/python -m tools.migrate_room_runtime_to_rooms

The script prints a per-room summary (rooms created / sensors created /
equipment created) and exits 0.

Mapping table — legacy field → new entity/type
==============================================

Sensors (each entity becomes one ``sensors`` row, ``isActive=True``,
``status="active"``, ``orgId`` server-default):

==========================  ===================  ===========================
Legacy field                Convex ``type``      Notes
==========================  ===================  ===========================
``temp_sensors``            ``"temperature"``    Room-level air temp probes.
``rh_sensors``              ``"humidity"``       Room-level RH probes.
``co2_sensors``             ``"co2"``            Room-level CO2 heads.
``leaf_temp_sensors``       ``"temperature"``    IR canopy temp; ``notes``
                                                 carries "leaf temperature".
``under_canopy_rh_probes``  ``"humidity"``       ``notes`` carries
                                                 "under-canopy RH probe".
``vwc_sensors``             ``"moisture"``       Substrate VWC (room-level).
``ec_sensors``              ``"ec"``             Substrate EC (room-level).
``ppfd_sensors``            ``"light"``          Canopy PPFD probes.
``dli_sensors``             ``"light"``          DLI; ``notes`` flags it.
``pm1_sensors``,            ``"air_quality"``    PM1/2.5/4/10; ``notes``
``pm25_sensors``,                                disambiguates the channel.
``pm4_sensors``,
``pm10_sensors``
Per-zone ``vwc_sensors``    ``"moisture"``       ``notes`` carries
                                                 ``f"zone {zone_id} VWC"``.
Per-zone ``ec_sensors``     ``"ec"``             ``notes`` carries
                                                 ``f"zone {zone_id} EC"``.
Per-tank ``ph_sensor``      ``"ph"``             ``notes`` carries
                                                 ``f"tank {tank_id} pH"``.
Per-tank ``ec_sensor``      ``"ec"``             ``notes`` carries
                                                 ``f"tank {tank_id} EC"``.
==========================  ===================  ===========================

Equipment (each entity becomes one ``equipment`` row,
``status="operational"``):

==============================  ====================  =====================
Legacy field                    Convex ``type``       Notes
==============================  ====================  =====================
``light_entities``              ``"lighting"``
``ac_entities``                 ``"hvac"``            "AC unit".
``dehumidifier_entities``       ``"hvac"``            "dehumidifier".
``reheat_entities``             ``"hvac"``            "reheat coil".
``exhaust_entities``            ``"hvac"``            "exhaust fan".
``co2_solenoid_entities``       ``"hvac"``            "CO2 solenoid".
``irrigation_pump_entities``    ``"irrigation"``      Room-level pump.
``mainline_valve_entities``     ``"irrigation"``      Room-level supply.
Per-zone ``valve_entities``     ``"irrigation"``      ``notes`` carries
                                                       ``f"zone {zone_id} valve"``.
Per-tank ``doser_entities``     ``"irrigation"``      ``notes`` carries
                                                       ``f"tank {tank_id} doser"``.
``cooling_capacity_entity``     ``"monitoring"``      Single ``climate.*``
                                                       or fan-stage source.
==============================  ====================  =====================

Idempotence
===========

Re-running the script does NOT duplicate rows. It looks up:

* ``buildings`` by ``(org_id, name="Facility")``.
* ``rooms`` by ``(org_id, building_id, name=room_id.upper())``.
* ``sensors``  by ``(org_id, room_id, external_id)``.
* ``equipment`` by ``(org_id, room_id, external_id)``.

Anything already present is left alone. New fields on the source are
*added*; no row is ever deleted by this script. To re-import after a
schema change, drop the rows by hand first.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import structlog
from app.api.sensors import resolve_home_assistant_integration_id
from app.config import get_settings
from app.db import dispose_engine, get_session_factory
from app.models.building import Building
from app.models.equipment import Equipment
from app.models.room import Room
from app.models.room_runtime import RoomRuntime
from app.models.sensor import Sensor
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

#: Display name for the single auto-created building (single-site install).
FACILITY_BUILDING_NAME = "Facility"

#: UUID5 namespace seed for deterministic room/sensor/equipment ids.
#: Picked once for this script; do not change — re-running with a
#: different namespace would generate fresh ids on every invocation
#: and defeat idempotence on a wiped target.
_DETERMINISTIC_NS = uuid.UUID("c0c5d000-0000-5000-8000-000000000001")


# ---------------------------------------------------------------------------
# Pure mapping layer — translated from the legacy equipment_map JSONB shape
# into a list of records the loader can INSERT. No DB access in here, no
# side effects: the unit tests in tests/unit/test_migrate_room_runtime.py
# exercise this directly.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SensorRecord:
    """Shape of one ``sensors`` row to be created."""

    external_id: str
    type: str
    name: str
    code: str
    data_unit: str
    notes: str | None = None


@dataclass(frozen=True)
class EquipmentRecord:
    """Shape of one ``equipment`` row to be created."""

    external_id: str
    type: str
    name: str
    code: str
    notes: str | None = None


@dataclass(frozen=True)
class RoomRecords:
    """Every record derived from one ``room_runtime`` row."""

    room_id: str
    display_name: str | None
    sensors: list[SensorRecord] = field(default_factory=list)
    equipment: list[EquipmentRecord] = field(default_factory=list)


#: Mapping from legacy ``equipment_map`` sensor list key → (Convex ``type``,
#: ``data_unit``, friendly role label). The role label is folded into the
#: sensor's ``name`` and ``notes`` so an operator can tell apart the dozen
#: ``"humidity"`` rows in a room. Each tuple is
#: ``(legacy_key, sensor.type, data_unit, role_label)``.
_SENSOR_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("temp_sensors", "temperature", "°C", "air temperature"),
    ("rh_sensors", "humidity", "%", "relative humidity"),
    ("co2_sensors", "co2", "ppm", "CO2"),
    ("leaf_temp_sensors", "temperature", "°C", "leaf temperature"),
    ("under_canopy_rh_probes", "humidity", "%", "under-canopy RH probe"),
    ("vwc_sensors", "moisture", "%", "substrate VWC"),
    ("ec_sensors", "ec", "mS/cm", "substrate EC"),
    ("ppfd_sensors", "light", "µmol/m²/s", "PPFD"),
    ("dli_sensors", "light", "mol/m²/day", "DLI"),
    ("pm1_sensors", "air_quality", "µg/m³", "PM1.0"),
    ("pm25_sensors", "air_quality", "µg/m³", "PM2.5"),
    ("pm4_sensors", "air_quality", "µg/m³", "PM4.0"),
    ("pm10_sensors", "air_quality", "µg/m³", "PM10"),
)

#: Mapping from legacy ``equipment_map`` actuator list key → (Convex
#: ``type``, friendly role label). Each tuple is
#: ``(legacy_key, equipment.type, role_label)``.
_EQUIPMENT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("light_entities", "lighting", "grow light"),
    ("ac_entities", "hvac", "AC unit"),
    ("dehumidifier_entities", "hvac", "dehumidifier"),
    ("reheat_entities", "hvac", "reheat coil"),
    ("exhaust_entities", "hvac", "exhaust fan"),
    ("co2_solenoid_entities", "hvac", "CO2 solenoid"),
    ("irrigation_pump_entities", "irrigation", "irrigation pump"),
    ("mainline_valve_entities", "irrigation", "mainline valve"),
)


def _slugify_code(value: str) -> str:
    """Cheap, deterministic sensor/equipment ``code`` from an entity id.

    Convex requires ``code`` (a per-org unique label, not enforced here).
    The HA entity id is already globally unique inside one install, so
    folding it to a slug is enough — and stays stable across reruns so
    the idempotence lookup still hits.
    """
    out: list[str] = []
    for ch in value.lower():
        out.append(ch if ch.isalnum() else "-")
    s = "".join(out).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s or "entity"


def _name_for(entity_id: str, role_label: str) -> str:
    """Build a human-readable display name for a sensor/equipment row."""
    return f"{role_label} ({entity_id})"


def _expand_room_level_sensors(
    equipment_map: dict[str, Any],
    rec: RoomRecords,
) -> None:
    for key, type_, unit, role_label in _SENSOR_FIELDS:
        for entity_id in equipment_map.get(key) or []:
            if not entity_id:
                continue
            rec.sensors.append(
                SensorRecord(
                    external_id=entity_id,
                    type=type_,
                    name=_name_for(entity_id, role_label),
                    code=_slugify_code(entity_id),
                    data_unit=unit,
                    notes=role_label,
                )
            )


def _expand_room_level_equipment(
    equipment_map: dict[str, Any],
    rec: RoomRecords,
) -> None:
    for key, type_, role_label in _EQUIPMENT_FIELDS:
        for entity_id in equipment_map.get(key) or []:
            if not entity_id:
                continue
            rec.equipment.append(
                EquipmentRecord(
                    external_id=entity_id,
                    type=type_,
                    name=_name_for(entity_id, role_label),
                    code=_slugify_code(entity_id),
                    notes=role_label,
                )
            )
    cooling = equipment_map.get("cooling_capacity_entity")
    if cooling:
        rec.equipment.append(
            EquipmentRecord(
                external_id=cooling,
                type="monitoring",
                name=_name_for(cooling, "cooling capacity source"),
                code=_slugify_code(cooling),
                notes="cooling capacity source",
            )
        )


def _expand_zone(zone: dict[str, Any], rec: RoomRecords) -> None:
    zone_id = str(zone.get("zone_id") or "")
    if not zone_id:
        return
    for entity_id in zone.get("valve_entities") or []:
        if not entity_id:
            continue
        rec.equipment.append(
            EquipmentRecord(
                external_id=entity_id,
                type="irrigation",
                name=_name_for(entity_id, f"{zone_id} valve"),
                code=_slugify_code(entity_id),
                notes=f"zone {zone_id} valve",
            )
        )
    for entity_id in zone.get("vwc_sensors") or []:
        if not entity_id:
            continue
        rec.sensors.append(
            SensorRecord(
                external_id=entity_id,
                type="moisture",
                name=_name_for(entity_id, f"{zone_id} VWC"),
                code=_slugify_code(entity_id),
                data_unit="%",
                notes=f"zone {zone_id} VWC",
            )
        )
    for entity_id in zone.get("ec_sensors") or []:
        if not entity_id:
            continue
        rec.sensors.append(
            SensorRecord(
                external_id=entity_id,
                type="ec",
                name=_name_for(entity_id, f"{zone_id} EC"),
                code=_slugify_code(entity_id),
                data_unit="mS/cm",
                notes=f"zone {zone_id} EC",
            )
        )


def _expand_tank(tank: dict[str, Any], rec: RoomRecords) -> None:
    tank_id = str(tank.get("tank_id") or "")
    if not tank_id:
        return
    ph = tank.get("ph_sensor")
    if ph:
        rec.sensors.append(
            SensorRecord(
                external_id=ph,
                type="ph",
                name=_name_for(ph, f"{tank_id} pH"),
                code=_slugify_code(ph),
                data_unit="pH",
                notes=f"tank {tank_id} pH",
            )
        )
    ec = tank.get("ec_sensor")
    if ec:
        rec.sensors.append(
            SensorRecord(
                external_id=ec,
                type="ec",
                name=_name_for(ec, f"{tank_id} EC"),
                code=_slugify_code(ec),
                data_unit="mS/cm",
                notes=f"tank {tank_id} EC",
            )
        )
    for entity_id in tank.get("doser_entities") or []:
        if not entity_id:
            continue
        rec.equipment.append(
            EquipmentRecord(
                external_id=entity_id,
                type="irrigation",
                name=_name_for(entity_id, f"{tank_id} doser"),
                code=_slugify_code(entity_id),
                notes=f"tank {tank_id} doser",
            )
        )


def map_room_runtime_row(
    room_id: str,
    display_name: str | None,
    equipment_map: dict[str, Any] | None,
) -> RoomRecords:
    """Translate one ``room_runtime`` row into its new-table records.

    Pure function — no DB, no I/O. ``equipment_map`` may be
    ``{}`` (an unconfigured room) or ``None`` (legacy NULL); both
    produce an empty :attr:`RoomRecords.sensors` /
    :attr:`RoomRecords.equipment` list.

    Args:
        room_id: The legacy ``room_runtime.room_id``.
        display_name: The optional operator-friendly room name.
        equipment_map: The legacy JSONB blob with HA entity lists.

    Returns:
        A :class:`RoomRecords` describing every new-table row this
        room should land.
    """
    rec = RoomRecords(room_id=room_id, display_name=display_name)
    if not equipment_map or not isinstance(equipment_map, dict):
        return rec

    _expand_room_level_sensors(equipment_map, rec)
    _expand_room_level_equipment(equipment_map, rec)
    for zone in equipment_map.get("zones") or []:
        if isinstance(zone, dict):
            _expand_zone(zone, rec)
    for tank in equipment_map.get("tanks") or []:
        if isinstance(tank, dict):
            _expand_tank(tank, rec)
    return rec


def map_room_runtime_rows(
    rows: Iterable[tuple[str, str | None, dict[str, Any] | None]],
) -> list[RoomRecords]:
    """Translate every supplied ``room_runtime`` tuple in one call."""
    return [
        map_room_runtime_row(room_id, display_name, equipment_map)
        for room_id, display_name, equipment_map in rows
    ]


# ---------------------------------------------------------------------------
# Loader — drives the DB inserts. Idempotent.
# ---------------------------------------------------------------------------


def _det_id(*parts: str) -> str:
    """Build a deterministic UUID5 string from the supplied parts."""
    return str(uuid.uuid5(_DETERMINISTIC_NS, "|".join(parts)))


async def _ensure_facility_building(
    session: AsyncSession,
    org_id: str,
) -> Building:
    """Return the org's "Facility" building, creating it if absent."""
    existing = (
        await session.execute(
            select(Building).where(
                Building.org_id == org_id,
                Building.name == FACILITY_BUILDING_NAME,
            )
        )
    ).scalars().first()
    if existing is not None:
        return existing
    building = Building(
        id=_det_id(org_id, "building", FACILITY_BUILDING_NAME),
        org_id=org_id,
        name=FACILITY_BUILDING_NAME,
    )
    session.add(building)
    await session.flush()
    log.info(
        "facility_building_created",
        org_id=org_id,
        building_id=building.id,
    )
    return building


async def _ensure_room(
    session: AsyncSession,
    org_id: str,
    building: Building,
    legacy_room_id: str,
    display_name: str | None,
) -> tuple[Room, bool]:
    """Return the Convex ``rooms`` row for ``legacy_room_id`` (create if needed).

    Returns:
        ``(room, created)`` where ``created`` is ``True`` only on the
        run that inserted the row.
    """
    name = legacy_room_id.upper()
    existing = (
        await session.execute(
            select(Room).where(
                Room.org_id == org_id,
                Room.building_id == building.id,
                Room.name == name,
            )
        )
    ).scalars().first()
    if existing is not None:
        return existing, False
    room = Room(
        id=_det_id(org_id, "room", building.id, legacy_room_id),
        org_id=org_id,
        building_id=building.id,
        name=name,
        purpose=display_name,
    )
    session.add(room)
    await session.flush()
    log.info(
        "room_created",
        org_id=org_id,
        room_id=room.id,
        legacy_room_id=legacy_room_id,
    )
    return room, True


async def _insert_records(
    session: AsyncSession,
    org_id: str,
    integration_id: str,
    room: Room,
    records: RoomRecords,
) -> tuple[int, int]:
    """Insert the sensor + equipment records for one room. Returns (s, e)."""
    sensors_added = 0
    equipment_added = 0

    # Existing sensors / equipment in this room keyed by external_id —
    # one round-trip per kind beats one round-trip per record.
    existing_sensors = {
        s.external_id
        for s in (
            await session.execute(
                select(Sensor).where(
                    Sensor.org_id == org_id,
                    Sensor.room_id == room.id,
                )
            )
        ).scalars()
        if s.external_id
    }
    existing_equipment = {
        e.external_id
        for e in (
            await session.execute(
                select(Equipment).where(
                    Equipment.org_id == org_id,
                    Equipment.room_id == room.id,
                )
            )
        ).scalars()
        if e.external_id
    }

    for s_rec in records.sensors:
        if s_rec.external_id in existing_sensors:
            continue
        sensor = Sensor(
            id=_det_id(org_id, "sensor", room.id, s_rec.external_id),
            org_id=org_id,
            room_id=room.id,
            integration_id=integration_id,
            external_id=s_rec.external_id,
            name=s_rec.name,
            code=s_rec.code,
            type=s_rec.type,
            data_unit=s_rec.data_unit,
            status="active",
            is_active=True,
            notes=s_rec.notes,
        )
        session.add(sensor)
        sensors_added += 1

    for e_rec in records.equipment:
        if e_rec.external_id in existing_equipment:
            continue
        equipment = Equipment(
            id=_det_id(org_id, "equipment", room.id, e_rec.external_id),
            org_id=org_id,
            room_id=room.id,
            integration_id=integration_id,
            external_id=e_rec.external_id,
            name=e_rec.name,
            code=e_rec.code,
            type=e_rec.type,
            status="operational",
            is_active=True,
            notes=e_rec.notes,
        )
        session.add(equipment)
        equipment_added += 1

    await session.flush()
    return sensors_added, equipment_added


async def run() -> None:
    """Drive the full migration end-to-end."""
    settings = get_settings()
    factory = get_session_factory()

    async with factory() as session:
        building = await _ensure_facility_building(session, settings.ocs_org_id)
        integration_id = await resolve_home_assistant_integration_id(
            session, settings.ocs_org_id
        )

        runtimes = (
            await session.execute(
                select(RoomRuntime).order_by(RoomRuntime.room_id)
            )
        ).scalars().all()

        total_rooms_created = 0
        total_sensors_created = 0
        total_equipment_created = 0
        for runtime in runtimes:
            records = map_room_runtime_row(
                runtime.room_id,
                runtime.display_name,
                runtime.equipment_map,
            )
            room, created = await _ensure_room(
                session,
                settings.ocs_org_id,
                building,
                runtime.room_id,
                runtime.display_name,
            )
            if created:
                total_rooms_created += 1
            sensors_added, equipment_added = await _insert_records(
                session,
                settings.ocs_org_id,
                integration_id,
                room,
                records,
            )
            total_sensors_created += sensors_added
            total_equipment_created += equipment_added
            print(
                f"  room {runtime.room_id!r:<6} "
                f"→ room.id={room.id}  "
                f"+{sensors_added} sensors  "
                f"+{equipment_added} equipment"
            )

        await session.commit()
        print(
            "\nmigration complete: "
            f"+{total_rooms_created} rooms, "
            f"+{total_sensors_created} sensors, "
            f"+{total_equipment_created} equipment."
        )

    await dispose_engine()


if __name__ == "__main__":  # pragma: no cover — script entrypoint
    asyncio.run(run())
