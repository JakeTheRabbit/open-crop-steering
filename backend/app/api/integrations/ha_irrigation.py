"""HA-Irrigation-Strategy integration HTTP surface.

Two admin-only endpoints under ``/api/integrations/ha-irrigation/*``:

* ``GET /discover`` — read-only registry walk against HA; returns the
  zones / candidate entities the frontend's entity-mapper UI consumes.
* ``POST /register`` — idempotent OCS-side bootstrap; takes the
  operator-confirmed mapping and creates ``buildings`` / ``rooms`` /
  ``locations`` / ``sensors`` / ``equipment`` rows.

Wire shape is camelCase via Pydantic ``Field(alias=...)`` to match every
other Convex-aligned response on the OCS API (see
``app.schemas.sites`` / ``app.schemas.sensors``). Snake_case input is
also accepted because every schema has ``populate_by_name=True``.

Discovery never writes to HA. Registration writes only to OCS Postgres
— there is **no** HA service-call from this pass; the supervisor /
executor / setpoint writers come in later integration passes (P2+).
"""

from __future__ import annotations

from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.acl import require_role
from app.core.auth import Identity
from app.db import get_session
from app.ha_client import HAClient, HAClientError
from app.integrations.ha_irrigation import discovery, registry
from app.models.user import RoleName

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/api/integrations/ha-irrigation", tags=["ha-irrigation"]
)


# ---------------------------------------------------------------------------
# Discovery response schemas
# ---------------------------------------------------------------------------


class _CamelModel(BaseModel):
    """Base model — camelCase aliases, strict input, extra forbidden."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class SuggestedRoom(_CamelModel):
    """The suggested room (name + external system id)."""

    name: str
    external_system_id: str = Field(alias="externalSystemId")


class CandidateEntities(_CamelModel):
    """Per-zone candidate entities — defaults for the frontend pickers."""

    vwc_sensors: list[str] = Field(alias="vwcSensors", default_factory=list)
    ec_sensors: list[str] = Field(alias="ecSensors", default_factory=list)
    valves: list[str] = Field(default_factory=list)


class ZoneCandidate(_CamelModel):
    """One detected zone with its candidate entities."""

    zone_index: int = Field(alias="zoneIndex")
    suggested_location_label: str = Field(alias="suggestedLocationLabel")
    candidate_entities: CandidateEntities = Field(alias="candidateEntities")


class RoomLevelCandidates(_CamelModel):
    """Room-level (non-zone-scoped) candidate entities."""

    pump: list[str] = Field(default_factory=list)
    mainline_valve: list[str] = Field(
        alias="mainlineValve", default_factory=list
    )
    steering_intent: list[str] = Field(
        alias="steeringIntent", default_factory=list
    )
    ec_targets: list[str] = Field(alias="ecTargets", default_factory=list)
    anomaly_binary_sensor: list[str] = Field(
        alias="anomalyBinarySensor", default_factory=list
    )
    phase_select: list[str] = Field(
        alias="phaseSelect", default_factory=list
    )
    rootsense_report_sensor: list[str] = Field(
        alias="rootsenseReportSensor", default_factory=list
    )


class DiscoverResponse(_CamelModel):
    """Successful ``GET /discover`` response.

    The frontend agent reads ``candidateEntities.*`` lists as the
    defaults for multi-entity-pickers the operator can edit. ``warnings``
    surfaces detected oddities (missing zone valve, RootSense report
    sensor absent, ...).
    """

    ok: Literal[True] = True
    suggested_room: SuggestedRoom = Field(alias="suggestedRoom")
    detected_zone_count: int = Field(alias="detectedZoneCount")
    zones: list[ZoneCandidate]
    room_level_candidates: RoomLevelCandidates = Field(
        alias="roomLevelCandidates"
    )
    warnings: list[str] = Field(default_factory=list)


class DiscoverErrorResponse(_CamelModel):
    """Failure response — HA unreachable, auth bounce, etc."""

    ok: Literal[False] = False
    error: str


# ---------------------------------------------------------------------------
# Registration request / response schemas
# ---------------------------------------------------------------------------


class RoomBody(_CamelModel):
    """Room-shape request — required name + optional building id."""

    name: str = Field(min_length=1, max_length=256)
    building_id: str | None = Field(default=None, alias="buildingId")


class ZoneBody(_CamelModel):
    """One zone's confirmed entity assignments."""

    zone_index: int = Field(alias="zoneIndex", ge=1)
    location_label: str = Field(
        alias="locationLabel", min_length=1, max_length=256
    )
    vwc_sensors: list[str] = Field(alias="vwcSensors", default_factory=list)
    ec_sensors: list[str] = Field(alias="ecSensors", default_factory=list)
    valves: list[str] = Field(default_factory=list)


class RoomLevelBody(_CamelModel):
    """Room-level (non-zone-scoped) confirmed entity assignments."""

    pump: str | None = None
    mainline_valve: str | None = Field(default=None, alias="mainlineValve")
    steering_intent: str | None = Field(
        default=None, alias="steeringIntent"
    )
    ec_targets: list[str] = Field(alias="ecTargets", default_factory=list)
    anomaly_binary_sensor: str | None = Field(
        default=None, alias="anomalyBinarySensor"
    )
    phase_select: str | None = Field(default=None, alias="phaseSelect")
    rootsense_report_sensor: str | None = Field(
        default=None, alias="rootsenseReportSensor"
    )


class RegisterRequest(_CamelModel):
    """``POST /register`` request body."""

    room: RoomBody
    zones: list[ZoneBody] = Field(default_factory=list)
    room_level: RoomLevelBody = Field(
        alias="roomLevel", default_factory=RoomLevelBody
    )


class LocationRecordOut(_CamelModel):
    """One per-zone location record in the response."""

    zone_index: int = Field(alias="zoneIndex")
    location_id: str = Field(alias="locationId")


class SensorRecordOut(_CamelModel):
    """One sensor record in the response."""

    external_id: str = Field(alias="externalId")
    sensor_id: str = Field(alias="sensorId")
    type: str
    scope: Literal["room", "zone"]


class EquipmentRecordOut(_CamelModel):
    """One equipment record in the response."""

    external_id: str = Field(alias="externalId")
    equipment_id: str = Field(alias="equipmentId")
    scope: Literal["room", "zone"]


class RegistrationRecordsOut(_CamelModel):
    """Per-record id-by-external-id map for the frontend."""

    room_id: str = Field(alias="roomId")
    building_id: str = Field(alias="buildingId")
    locations: list[LocationRecordOut] = Field(default_factory=list)
    sensors: list[SensorRecordOut] = Field(default_factory=list)
    equipment: list[EquipmentRecordOut] = Field(default_factory=list)


class RegisterResponse(_CamelModel):
    """``POST /register`` response body."""

    ok: Literal[True] = True
    room_id: str = Field(alias="roomId")
    building_id: str = Field(alias="buildingId")
    locations_created: int = Field(alias="locationsCreated")
    locations_updated: int = Field(alias="locationsUpdated")
    sensors_created: int = Field(alias="sensorsCreated")
    sensors_updated: int = Field(alias="sensorsUpdated")
    equipment_created: int = Field(alias="equipmentCreated")
    equipment_updated: int = Field(alias="equipmentUpdated")
    records: RegistrationRecordsOut


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _discovery_to_response(
    result: discovery.DiscoveryResult,
) -> DiscoverResponse:
    """Convert the dataclass discovery result into the Pydantic response."""
    return DiscoverResponse(
        ok=True,
        suggested_room=SuggestedRoom(
            name=result.suggested_room.name,
            external_system_id=result.suggested_room.external_system_id,
        ),
        detected_zone_count=result.detected_zone_count,
        zones=[
            ZoneCandidate(
                zone_index=zone.zone_index,
                suggested_location_label=zone.suggested_location_label,
                candidate_entities=CandidateEntities(
                    vwc_sensors=list(zone.candidate_entities.vwc_sensors),
                    ec_sensors=list(zone.candidate_entities.ec_sensors),
                    valves=list(zone.candidate_entities.valves),
                ),
            )
            for zone in result.zones
        ],
        room_level_candidates=RoomLevelCandidates(
            pump=list(result.room_level_candidates.pump),
            mainline_valve=list(result.room_level_candidates.mainline_valve),
            steering_intent=list(
                result.room_level_candidates.steering_intent
            ),
            ec_targets=list(result.room_level_candidates.ec_targets),
            anomaly_binary_sensor=list(
                result.room_level_candidates.anomaly_binary_sensor
            ),
            phase_select=list(result.room_level_candidates.phase_select),
            rootsense_report_sensor=list(
                result.room_level_candidates.rootsense_report_sensor
            ),
        ),
        warnings=list(result.warnings),
    )


def _registration_to_response(
    result: registry.RegistrationResult,
) -> RegisterResponse:
    """Convert the dataclass registration result into the response model."""
    return RegisterResponse(
        ok=True,
        room_id=result.room_id,
        building_id=result.building_id,
        locations_created=result.locations_created,
        locations_updated=result.locations_updated,
        sensors_created=result.sensors_created,
        sensors_updated=result.sensors_updated,
        equipment_created=result.equipment_created,
        equipment_updated=result.equipment_updated,
        records=RegistrationRecordsOut(
            room_id=result.records.room_id,
            building_id=result.records.building_id,
            locations=[
                LocationRecordOut(
                    zone_index=row.zone_index, location_id=row.location_id
                )
                for row in result.records.locations
            ],
            sensors=[
                SensorRecordOut(
                    external_id=row.external_id,
                    sensor_id=row.sensor_id,
                    type=row.type,
                    scope=row.scope,
                )
                for row in result.records.sensors
            ],
            equipment=[
                EquipmentRecordOut(
                    external_id=row.external_id,
                    equipment_id=row.equipment_id,
                    scope=row.scope,
                )
                for row in result.records.equipment
            ],
        ),
    )


def _request_to_mapping(body: RegisterRequest) -> registry.MappingInput:
    """Convert the Pydantic request body into the registry input dataclass."""
    return registry.MappingInput(
        room=registry.RoomInput(
            name=body.room.name, building_id=body.room.building_id
        ),
        zones=[
            registry.ZoneInput(
                zone_index=zone.zone_index,
                location_label=zone.location_label,
                vwc_sensors=list(zone.vwc_sensors),
                ec_sensors=list(zone.ec_sensors),
                valves=list(zone.valves),
            )
            for zone in body.zones
        ],
        room_level=registry.RoomLevelInput(
            pump=body.room_level.pump,
            mainline_valve=body.room_level.mainline_valve,
            steering_intent=body.room_level.steering_intent,
            ec_targets=list(body.room_level.ec_targets),
            anomaly_binary_sensor=body.room_level.anomaly_binary_sensor,
            phase_select=body.room_level.phase_select,
            rootsense_report_sensor=body.room_level.rootsense_report_sensor,
        ),
    )


def _build_ha_client() -> HAClient:
    """Return a fresh :class:`HAClient`. Indirection so tests can override.

    Test setup overrides this via dependency injection on the FastAPI
    app, so a fake HA client can stand in. Production paths get the
    real client.
    """
    return HAClient()


@router.get(
    "/discover",
    response_model=DiscoverResponse,
    response_model_by_alias=True,
)
async def discover_ha_irrigation(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    ha_client: Annotated[HAClient, Depends(_build_ha_client)],
) -> DiscoverResponse:
    """Walk HA's entity registry and group crop_steering entities by role.

    Returns the room suggestion + per-zone candidate entities the
    frontend's entity-mapper renders. Read-only: never writes to HA.
    A second call returns the same payload (modulo HA-side changes).

    Returns 502 if the HA connection fails — frontend should treat the
    failure as recoverable and offer a retry.
    """
    try:
        async with ha_client:
            result = await discovery.discover(ha_client)
    except (HAClientError, OSError) as exc:
        log.warning(
            "ha_irrigation_discover_failed",
            actor=identity.user_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Home Assistant unreachable: {exc}",
        ) from exc

    log.info(
        "ha_irrigation_discovered",
        actor=identity.user_id,
        zones=result.detected_zone_count,
        warnings=len(result.warnings),
    )
    return _discovery_to_response(result)


@router.post(
    "/register",
    response_model=RegisterResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def register_ha_irrigation(
    body: RegisterRequest,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RegisterResponse:
    """Persist the operator-confirmed mapping into the OCS schema.

    Idempotent. A second call with the same body returns the same ids;
    a second call with a different mapping updates the existing record
    in place. All writes are inside one transaction — partial state on
    failure is impossible.

    Auth: admin (Class-E facility-mapping surface).
    """
    settings = get_settings()
    try:
        result = await registry.register_mapping(
            session,
            mapping=_request_to_mapping(body),
            settings=settings,
            actor_id=identity.user_id,
            actor_role="admin",
        )
    except ValueError as exc:
        # The only ValueError raised by register_mapping is "building id
        # not found in tenant" — surface as 404 to the frontend.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    await session.commit()
    log.info(
        "ha_irrigation_registered",
        actor=identity.user_id,
        room_id=result.room_id,
        building_id=result.building_id,
        zones=len(body.zones),
    )
    return _registration_to_response(result)
