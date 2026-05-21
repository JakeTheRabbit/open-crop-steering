"""HA-Irrigation-Strategy registration — idempotent OCS-side persistence.

Given an operator-confirmed mapping (a :class:`MappingInput` carrying the
suggested room + per-zone candidates + room-level candidates), insert the
matching ``buildings`` / ``rooms`` / ``locations`` / ``sensors`` /
``equipment`` rows into the OCS Postgres.

**Idempotency**. Every record is keyed by something stable from the
input mapping, so re-running the same registration with the same body is
a no-op (existing-row id is returned):

* Building — keyed by ``(org_id, name)``; lazy-created as ``"Facility"``
  when the mapping omits ``buildingId``.
* Room — keyed by ``(org_id, building_id, name)``.
* Location — keyed by ``(room_id, code)`` where ``code = ZONE_{N}``.
  Stored in ``Location.path`` because the ORM has no dedicated ``code``
  column; the ``ZONE_{N}`` shape is matched exactly on re-runs.
* Sensor / Equipment — keyed by ``(room_id, external_id)`` where
  ``external_id`` is the HA entity id.

A second registration with a *different* mapping (e.g. the operator
changed which entity is the pump) updates the matching record in place
rather than creating a duplicate. Stale rows (a sensor that was in the
old mapping but is no longer referenced) are left intact — this pass is
additive; pruning is a future concern when the entity-mapper UI grows a
"remove" action.

No HA call is ever made from this module. The supervisor / executor /
setpoint writers come in later integration passes (P2+).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.sensors import resolve_home_assistant_integration_id
from app.core.audit import log_audit
from app.models.audit_event import AuditEventType
from app.models.building import Building
from app.models.equipment import Equipment
from app.models.location import Location
from app.models.room import Room
from app.models.sensor import Sensor

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants — naming defaults
# ---------------------------------------------------------------------------


#: Name of the lazy-created building used when the operator omits
#: ``buildingId``. Single-tenant installs uniformly land here.
DEFAULT_FACILITY_NAME = "Facility"


# ---------------------------------------------------------------------------
# Settings protocol — what register_mapping needs from app.config.Settings
# ---------------------------------------------------------------------------


class SettingsProtocol(Protocol):
    """The subset of :class:`app.config.Settings` register_mapping uses."""

    ocs_org_id: str


# ---------------------------------------------------------------------------
# Input dataclasses (mirrored 1:1 by the Pydantic schemas in the API layer)
# ---------------------------------------------------------------------------


@dataclass
class ZoneInput:
    """One zone's confirmed entity assignments.

    Fields are plain ``list[str]`` so the API layer can hand the
    operator's selections through unchanged — empty lists are
    explicitly allowed (an operator may register a zone with no EC
    sensor yet, for example).
    """

    zone_index: int
    location_label: str
    vwc_sensors: list[str] = field(default_factory=list)
    ec_sensors: list[str] = field(default_factory=list)
    valves: list[str] = field(default_factory=list)


@dataclass
class RoomLevelInput:
    """Room-level (non-zone-scoped) confirmed entity assignments.

    Singular fields (``pump``, ``mainline_valve``, …) take exactly one
    entity id because each role only ever has one entity instance.
    ``ec_targets`` is a list because HA-IS exposes nine of them.
    """

    pump: str | None = None
    mainline_valve: str | None = None
    steering_intent: str | None = None
    ec_targets: list[str] = field(default_factory=list)
    anomaly_binary_sensor: str | None = None
    phase_select: str | None = None
    rootsense_report_sensor: str | None = None


@dataclass
class RoomInput:
    """Room-shape input — required name plus optional pre-existing building."""

    name: str
    building_id: str | None = None


@dataclass
class MappingInput:
    """Operator-confirmed mapping payload."""

    room: RoomInput
    zones: list[ZoneInput] = field(default_factory=list)
    room_level: RoomLevelInput = field(default_factory=RoomLevelInput)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


#: Synonym used by :class:`SensorRecord` / :class:`EquipmentRecord`'s
#: ``scope`` field — "room" for a room-level (no location) row,
#: "zone" for a zone-scoped (per-location) row.
RecordScope = Literal["room", "zone"]


@dataclass
class LocationRecord:
    """One per-zone location row that landed in Postgres."""

    zone_index: int
    location_id: str


@dataclass
class SensorRecord:
    """One sensor row that landed in Postgres (created or updated)."""

    external_id: str
    sensor_id: str
    type: str
    scope: RecordScope


@dataclass
class EquipmentRecord:
    """One equipment row that landed in Postgres (created or updated)."""

    external_id: str
    equipment_id: str
    scope: RecordScope


@dataclass
class RegistrationRecords:
    """Per-record details — what was created/updated, keyed for joining."""

    room_id: str
    building_id: str
    locations: list[LocationRecord] = field(default_factory=list)
    sensors: list[SensorRecord] = field(default_factory=list)
    equipment: list[EquipmentRecord] = field(default_factory=list)


@dataclass
class RegistrationResult:
    """The top-level register_mapping result.

    Counts (``locations_created`` etc.) make the frontend's "saved X
    sensors, updated Y equipment" toast trivial; the nested
    :class:`RegistrationRecords` carries the full id-by-external-id map
    so the UI can immediately render links.
    """

    room_id: str
    building_id: str
    locations_created: int
    locations_updated: int
    sensors_created: int
    sensors_updated: int
    equipment_created: int
    equipment_updated: int
    records: RegistrationRecords


# ---------------------------------------------------------------------------
# Helpers — sensor/equipment "type" + "data_unit" inference
# ---------------------------------------------------------------------------


def _sensor_spec_for(role: str) -> tuple[str, str]:
    """Return ``(type, data_unit)`` for a sensor role string.

    Roles map onto :class:`app.models.sensor.Sensor` type values:

    * ``"vwc"`` → ``("moisture", "%")``
    * ``"ec"`` → ``("ec", "mS/cm")``
    * ``"anomaly"`` → ``("other", "")`` — binary sensor, no native type.
    * ``"rootsense_report"`` → ``("other", "")`` — text/JSON state, no
      meaningful unit.
    """
    if role == "vwc":
        return ("moisture", "%")
    if role == "ec":
        return ("ec", "mS/cm")
    return ("other", "")


def _control_target_equipment_type() -> str:
    """Return the :class:`Equipment.type` for a control-target row.

    HA-IS's steering-intent slider, EC targets, and phase select are
    write-able control surfaces, not physical assets. The
    :class:`Equipment.type` Literal union does not include a
    purpose-built ``control_target`` value, so they land under
    ``"other"`` — the integration spec §7 explicitly accepts this as
    the documented choice. Pulled into its own function for visibility:
    "yes, control targets land in ``other``, and here is the one place
    that decision is made."
    """
    return "other"


def _equipment_name(role: str, external_id: str) -> str:
    """Build a human-friendly equipment ``name`` from a role + entity id."""
    return f"HA-IS {role}: {external_id}"


def _sensor_name(role: str, external_id: str) -> str:
    """Build a human-friendly sensor ``name`` from a role + entity id."""
    return f"HA-IS {role}: {external_id}"


# ---------------------------------------------------------------------------
# Upsert helpers — match-or-create, returning ``(row, created)``
# ---------------------------------------------------------------------------


async def _upsert_building(
    session: AsyncSession,
    *,
    org_id: str,
    building_id_hint: str | None,
) -> tuple[Building, bool]:
    """Resolve the target building.

    Three branches:

    1. ``building_id_hint`` set → return the matching :class:`Building`
       if it exists in this tenant. ``ValueError`` if not (a frontend
       sent a stale / cross-tenant id; the API turns that into a 404).
    2. ``"Facility"`` already exists for this tenant → return it. This
       is the dominant idempotency path — every re-registration hits it.
    3. None of the above → lazy-create the ``"Facility"`` row.

    Args:
        session: An open async DB session.
        org_id: Tenant id (server-stamped from settings).
        building_id_hint: Optional caller-supplied building id.

    Returns:
        ``(building, created)``. ``created`` is ``True`` only when the
        ``Facility`` was lazy-created on this call.
    """
    if building_id_hint is not None:
        existing = await session.get(Building, building_id_hint)
        if existing is None or existing.org_id != org_id:
            raise ValueError(
                f"building '{building_id_hint}' not found in tenant"
            )
        return existing, False

    facility = (
        await session.execute(
            select(Building).where(
                Building.org_id == org_id,
                Building.name == DEFAULT_FACILITY_NAME,
            )
        )
    ).scalars().first()
    if facility is not None:
        return facility, False

    facility = Building(org_id=org_id, name=DEFAULT_FACILITY_NAME)
    session.add(facility)
    await session.flush()
    await session.refresh(facility)
    return facility, True


async def _upsert_room(
    session: AsyncSession,
    *,
    org_id: str,
    building_id: str,
    name: str,
) -> tuple[Room, bool]:
    """Match-or-create a :class:`Room` by ``(org_id, building_id, name)``.

    Returns ``(room, created)``. Re-runs return ``created=False``.
    """
    existing = (
        await session.execute(
            select(Room).where(
                Room.org_id == org_id,
                Room.building_id == building_id,
                Room.name == name,
            )
        )
    ).scalars().first()
    if existing is not None:
        return existing, False

    room = Room(org_id=org_id, building_id=building_id, name=name)
    session.add(room)
    await session.flush()
    await session.refresh(room)
    return room, True


async def _upsert_location(
    session: AsyncSession,
    *,
    org_id: str,
    room_id: str,
    code: str,
    label: str,
) -> tuple[Location, bool]:
    """Match-or-create a :class:`Location` by ``(room_id, code)``.

    The ORM has no native ``code`` column, so the zone code (``ZONE_1``,
    ``ZONE_2``, …) lives in :attr:`Location.path` and is matched there.
    Idempotent: a re-run returns the existing row.
    """
    existing = (
        await session.execute(
            select(Location).where(
                Location.org_id == org_id,
                Location.room_id == room_id,
                Location.path == code,
            )
        )
    ).scalars().first()
    if existing is not None:
        # An operator may have renamed the label — keep the saved
        # version in sync without breaking the (room_id, code) key.
        if existing.label != label:
            existing.label = label
            await session.flush()
        return existing, False

    location = Location(
        org_id=org_id,
        room_id=room_id,
        label=label,
        path=code,
        type="zone",
    )
    session.add(location)
    await session.flush()
    await session.refresh(location)
    return location, True


async def _upsert_sensor(
    session: AsyncSession,
    *,
    org_id: str,
    room_id: str,
    location_id: str | None,
    external_id: str,
    role: str,
    integration_id: str,
) -> tuple[Sensor, bool]:
    """Match-or-create a :class:`Sensor` keyed by ``(room_id, external_id)``.

    Updates ``location_id`` / ``type`` / ``data_unit`` / ``integration_id``
    on re-runs so the existing record reflects the latest mapping
    (e.g. a zone re-assignment).
    """
    sensor_type, data_unit = _sensor_spec_for(role)
    name = _sensor_name(role, external_id)
    existing = (
        await session.execute(
            select(Sensor).where(
                Sensor.org_id == org_id,
                Sensor.room_id == room_id,
                Sensor.external_id == external_id,
            )
        )
    ).scalars().first()
    if existing is not None:
        changed = False
        if existing.location_id != location_id:
            existing.location_id = location_id
            changed = True
        if existing.type != sensor_type:
            existing.type = sensor_type
            changed = True
        if existing.data_unit != data_unit:
            existing.data_unit = data_unit
            changed = True
        if existing.integration_id != integration_id:
            existing.integration_id = integration_id
            changed = True
        if existing.name != name:
            existing.name = name
            changed = True
        if changed:
            await session.flush()
        return existing, False

    sensor = Sensor(
        org_id=org_id,
        room_id=room_id,
        location_id=location_id,
        external_id=external_id,
        integration_id=integration_id,
        name=name,
        code=external_id,
        type=sensor_type,
        data_unit=data_unit,
        status="active",
        is_active=True,
    )
    session.add(sensor)
    await session.flush()
    await session.refresh(sensor)
    return sensor, True


async def _upsert_equipment(
    session: AsyncSession,
    *,
    org_id: str,
    room_id: str,
    location_id: str | None,
    external_id: str,
    role: str,
    equipment_type: str,
    integration_id: str,
) -> tuple[Equipment, bool]:
    """Match-or-create an :class:`Equipment` row keyed by ``(room_id, external_id)``.

    Updates ``location_id`` / ``type`` / ``integration_id`` on re-runs
    so the existing record reflects the latest mapping. ``room_id`` is
    held constant — a zone-rescope inside the same room updates the
    location pointer; a room change is a much bigger operation we leave
    to a future "delete + re-register" flow.
    """
    name = _equipment_name(role, external_id)
    existing = (
        await session.execute(
            select(Equipment).where(
                Equipment.org_id == org_id,
                Equipment.room_id == room_id,
                Equipment.external_id == external_id,
            )
        )
    ).scalars().first()
    if existing is not None:
        changed = False
        if existing.location_id != location_id:
            existing.location_id = location_id
            changed = True
        if existing.type != equipment_type:
            existing.type = equipment_type
            changed = True
        if existing.integration_id != integration_id:
            existing.integration_id = integration_id
            changed = True
        if existing.name != name:
            existing.name = name
            changed = True
        if changed:
            await session.flush()
        return existing, False

    equipment = Equipment(
        org_id=org_id,
        room_id=room_id,
        location_id=location_id,
        external_id=external_id,
        integration_id=integration_id,
        name=name,
        code=external_id,
        type=equipment_type,
        status="operational",
        is_active=True,
    )
    session.add(equipment)
    await session.flush()
    await session.refresh(equipment)
    return equipment, True


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def register_mapping(
    session: AsyncSession,
    mapping: MappingInput,
    settings: SettingsProtocol,
    *,
    actor_id: str | None = None,
    actor_role: str | None = None,
) -> RegistrationResult:
    """Persist the operator-confirmed mapping into the OCS schema.

    All writes share one transaction — the caller commits. If anything
    raises, the caller rolls back and Postgres unwinds the whole
    operation; partial state is impossible.

    Args:
        session: An open async DB session.
        mapping: The operator-confirmed mapping payload.
        settings: Settings object (provides ``ocs_org_id``).
        actor_id: Optional acting user id, written into every audit row.
        actor_role: Optional acting role string ("admin" by default
            from the API layer).

    Returns:
        A :class:`RegistrationResult` carrying the created/updated
        counts and the full id-by-external-id record map.
    """
    org_id = settings.ocs_org_id

    # --- 1. Building + Room + integration resolver --------------------
    building, building_created = await _upsert_building(
        session, org_id=org_id, building_id_hint=mapping.room.building_id
    )
    room, room_created = await _upsert_room(
        session,
        org_id=org_id,
        building_id=building.id,
        name=mapping.room.name,
    )
    integration_id = await resolve_home_assistant_integration_id(
        session, org_id
    )

    counts = {
        "locations_created": 0,
        "locations_updated": 0,
        "sensors_created": 0,
        "sensors_updated": 0,
        "equipment_created": 0,
        "equipment_updated": 0,
    }
    records = RegistrationRecords(
        room_id=room.id, building_id=building.id
    )

    # --- 2. Per-zone Locations + zone sensors + zone valves -----------
    for zone in mapping.zones:
        code = f"ZONE_{zone.zone_index}"
        location, location_created = await _upsert_location(
            session,
            org_id=org_id,
            room_id=room.id,
            code=code,
            label=zone.location_label,
        )
        if location_created:
            counts["locations_created"] += 1
        else:
            counts["locations_updated"] += 1
        records.locations.append(
            LocationRecord(zone_index=zone.zone_index, location_id=location.id)
        )

        for vwc_id in zone.vwc_sensors:
            sensor, created = await _upsert_sensor(
                session,
                org_id=org_id,
                room_id=room.id,
                location_id=location.id,
                external_id=vwc_id,
                role="vwc",
                integration_id=integration_id,
            )
            counts["sensors_created" if created else "sensors_updated"] += 1
            records.sensors.append(
                SensorRecord(
                    external_id=vwc_id,
                    sensor_id=sensor.id,
                    type=sensor.type,
                    scope="zone",
                )
            )

        for ec_id in zone.ec_sensors:
            sensor, created = await _upsert_sensor(
                session,
                org_id=org_id,
                room_id=room.id,
                location_id=location.id,
                external_id=ec_id,
                role="ec",
                integration_id=integration_id,
            )
            counts["sensors_created" if created else "sensors_updated"] += 1
            records.sensors.append(
                SensorRecord(
                    external_id=ec_id,
                    sensor_id=sensor.id,
                    type=sensor.type,
                    scope="zone",
                )
            )

        for valve_id in zone.valves:
            equipment, created = await _upsert_equipment(
                session,
                org_id=org_id,
                room_id=room.id,
                location_id=location.id,
                external_id=valve_id,
                role="zone_valve",
                equipment_type="irrigation",
                integration_id=integration_id,
            )
            counts["equipment_created" if created else "equipment_updated"] += 1
            records.equipment.append(
                EquipmentRecord(
                    external_id=valve_id,
                    equipment_id=equipment.id,
                    scope="zone",
                )
            )

    # --- 3. Room-level equipment + sensors ----------------------------
    room_level = mapping.room_level

    room_equipment_specs: list[tuple[str | None, str, str]] = [
        (room_level.pump, "pump", "irrigation"),
        (room_level.mainline_valve, "mainline_valve", "irrigation"),
        (room_level.steering_intent, "steering_intent",
         _control_target_equipment_type()),
        (room_level.phase_select, "phase_select",
         _control_target_equipment_type()),
    ]
    for ec_target_id in room_level.ec_targets:
        room_equipment_specs.append(
            (ec_target_id, "ec_target", _control_target_equipment_type())
        )

    for ext_id, role, eq_type in room_equipment_specs:
        if not ext_id:
            continue
        equipment, created = await _upsert_equipment(
            session,
            org_id=org_id,
            room_id=room.id,
            location_id=None,
            external_id=ext_id,
            role=role,
            equipment_type=eq_type,
            integration_id=integration_id,
        )
        counts["equipment_created" if created else "equipment_updated"] += 1
        records.equipment.append(
            EquipmentRecord(
                external_id=ext_id, equipment_id=equipment.id, scope="room"
            )
        )

    # The anomaly binary sensor + RootSense report sensor land as
    # ``Sensor`` rows of type "other" — they're not standard sensor types
    # but they ARE state-bearing entities the supervisor reads.
    room_sensor_specs: list[tuple[str | None, str]] = [
        (room_level.anomaly_binary_sensor, "anomaly"),
        (room_level.rootsense_report_sensor, "rootsense_report"),
    ]
    for ext_id, role in room_sensor_specs:
        if not ext_id:
            continue
        sensor, created = await _upsert_sensor(
            session,
            org_id=org_id,
            room_id=room.id,
            location_id=None,
            external_id=ext_id,
            role=role,
            integration_id=integration_id,
        )
        counts["sensors_created" if created else "sensors_updated"] += 1
        records.sensors.append(
            SensorRecord(
                external_id=ext_id,
                sensor_id=sensor.id,
                type=sensor.type,
                scope="room",
            )
        )

    # --- 4. Audit row per mutation -----------------------------------
    # One info_event per mutating call — building+room+location creates,
    # plus a summary row capturing every sensor/equipment counts so the
    # chain captures the integration setup end-to-end.
    if building_created:
        await log_audit(
            session,
            event_type=AuditEventType.info_event,
            actor_id=actor_id,
            actor_role=actor_role,
            summary=(
                f"Facility building '{building.name}' lazy-created for "
                "HA-Irrigation-Strategy registration"
            ),
            params={"building_id": building.id, "name": building.name},
        )
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=actor_id,
        actor_role=actor_role,
        summary=(
            f"HA-Irrigation-Strategy registration for room '{room.name}' "
            f"({'created' if room_created else 'updated'}): "
            f"{counts['locations_created']} locations created, "
            f"{counts['locations_updated']} updated; "
            f"{counts['sensors_created']} sensors created, "
            f"{counts['sensors_updated']} updated; "
            f"{counts['equipment_created']} equipment created, "
            f"{counts['equipment_updated']} updated"
        ),
        params={
            "room_id": room.id,
            "building_id": building.id,
            "room_created": room_created,
            **counts,
        },
    )

    log.info(
        "ha_irrigation_registered",
        org_id=org_id,
        room_id=room.id,
        building_id=building.id,
        zones=len(mapping.zones),
        **counts,
    )
    return RegistrationResult(
        room_id=room.id,
        building_id=building.id,
        locations_created=counts["locations_created"],
        locations_updated=counts["locations_updated"],
        sensors_created=counts["sensors_created"],
        sensors_updated=counts["sensors_updated"],
        equipment_created=counts["equipment_created"],
        equipment_updated=counts["equipment_updated"],
        records=records,
    )
