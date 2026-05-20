"""Sensors tier REST API — sensors / sensor readings / integrations.

Three Convex-aligned entities served under separate prefixes:

* ``/api/sensors`` — the sensor entity.
* ``/api/sensor-readings`` — the high-volume time-series table.
* ``/api/sensor-integrations`` — connection definitions feeding sensors.

Each entity exposes the standard ``POST / GET-list / GET-one / PUT /
DELETE`` surface. The three resources are mounted as three sibling
:class:`APIRouter` instances and re-exported at module-level so
``main.py`` can include them one at a time, keeping the existing
include pattern.

Every endpoint requires the ``admin`` role — sensors and their
integrations are Class-E entities. Mutations write an ``info_event``
audit row; reads stay quiet.

JSON wire shape is camelCase + epoch-ms timestamps so it is
byte-compatible with a Convex document. Snake_case input is also
accepted because ``populate_by_name=True`` is set on every schema.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.acl import require_role
from app.core.audit import log_audit
from app.core.auth import Identity
from app.db import get_session
from app.models.audit_event import AuditEventType
from app.models.sensor import Sensor
from app.models.sensor_integration import SensorIntegration
from app.models.sensor_reading import SensorReading
from app.models.user import RoleName
from app.schemas.sensors import (
    SensorCreate,
    SensorIntegrationCreate,
    SensorIntegrationRead,
    SensorRead,
    SensorReadingCreate,
    SensorReadingRead,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------


sensors_router = APIRouter(prefix="/api/sensors", tags=["sensors"])


def _sensor_payload(body: SensorCreate) -> dict[str, Any]:
    """Convert a :class:`SensorCreate` to ORM kwargs.

    The ``alerts`` field is serialized to plain dicts so it lands in the
    JSONB column as JSON-safe primitives.
    """
    return body.model_dump(by_alias=False)


@sensors_router.post(
    "",
    response_model=SensorRead,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def create_sensor(
    body: SensorCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Sensor:
    """Create a sensor."""
    settings = get_settings()
    sensor = Sensor(org_id=settings.ocs_org_id, **_sensor_payload(body))
    session.add(sensor)
    await session.flush()
    await session.refresh(sensor)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Sensor '{sensor.name}' created",
        params={"sensor_id": sensor.id, "name": sensor.name, "type": sensor.type},
    )
    await session.commit()
    log.info("sensor_created", actor=identity.user_id, sensor_id=sensor.id)
    return sensor


@sensors_router.get("", response_model_by_alias=True)
async def list_sensors(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
    room_id: Annotated[
        str | None, Query(alias="roomId", description="Filter to one room.")
    ] = None,
    type_: Annotated[
        str | None,
        Query(alias="type", description="Filter to one sensor type."),
    ] = None,
    status_filter: Annotated[
        str | None,
        Query(alias="status", description="Filter to one status."),
    ] = None,
) -> dict[str, Any]:
    """List sensors in the current tenant, optionally filtered."""
    settings = get_settings()
    stmt = select(Sensor).where(Sensor.org_id == settings.ocs_org_id)
    if room_id is not None:
        stmt = stmt.where(Sensor.room_id == room_id)
    if type_ is not None:
        stmt = stmt.where(Sensor.type == type_)
    if status_filter is not None:
        stmt = stmt.where(Sensor.status == status_filter)
    rows = (await session.execute(stmt.order_by(Sensor.created_at))).scalars()
    items = [
        SensorRead.model_validate(s).model_dump(mode="json", by_alias=True)
        for s in rows
    ]
    log.info(
        "sensors_listed",
        actor=identity.user_id,
        count=len(items),
        room_id=room_id,
        type=type_,
        status=status_filter,
    )
    return {"sensors": items}


@sensors_router.get(
    "/{sensor_id}",
    response_model=SensorRead,
    response_model_by_alias=True,
)
async def get_sensor(
    sensor_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Sensor:
    """Fetch one sensor by id."""
    settings = get_settings()
    sensor = await session.get(Sensor, sensor_id)
    if sensor is None or sensor.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"sensor '{sensor_id}' not found",
        )
    log.info("sensor_fetched", actor=identity.user_id, sensor_id=sensor_id)
    return sensor


@sensors_router.put(
    "/{sensor_id}",
    response_model=SensorRead,
    response_model_by_alias=True,
)
async def update_sensor(
    sensor_id: str,
    body: SensorCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Sensor:
    """Replace a sensor's mutable fields."""
    settings = get_settings()
    sensor = await session.get(Sensor, sensor_id)
    if sensor is None or sensor.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"sensor '{sensor_id}' not found",
        )
    for field, value in _sensor_payload(body).items():
        setattr(sensor, field, value)
    await session.flush()
    await session.refresh(sensor)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Sensor '{sensor.name}' updated",
        params={"sensor_id": sensor.id, "name": sensor.name},
    )
    await session.commit()
    log.info("sensor_updated", actor=identity.user_id, sensor_id=sensor_id)
    return sensor


@sensors_router.delete("/{sensor_id}", status_code=status.HTTP_200_OK)
async def delete_sensor(
    sensor_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Delete a sensor. Sensor readings cascade per the FK."""
    settings = get_settings()
    sensor = await session.get(Sensor, sensor_id)
    if sensor is None or sensor.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"sensor '{sensor_id}' not found",
        )
    await session.delete(sensor)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Sensor '{sensor_id}' deleted",
        params={"sensor_id": sensor_id},
    )
    await session.commit()
    log.info("sensor_deleted", actor=identity.user_id, sensor_id=sensor_id)
    return {"deleted": sensor_id}


# ---------------------------------------------------------------------------
# Sensor readings
# ---------------------------------------------------------------------------


readings_router = APIRouter(prefix="/api/sensor-readings", tags=["sensor-readings"])


@readings_router.post(
    "",
    response_model=SensorReadingRead,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def create_sensor_reading(
    body: SensorReadingCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SensorReading:
    """Create a sensor reading."""
    settings = get_settings()
    reading = SensorReading(
        org_id=settings.ocs_org_id,
        **body.model_dump(by_alias=False),
    )
    session.add(reading)
    await session.flush()
    await session.refresh(reading)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Sensor reading recorded for sensor {reading.sensor_id}",
        params={
            "sensor_reading_id": reading.id,
            "sensor_id": reading.sensor_id,
            "timestamp": reading.timestamp,
        },
    )
    await session.commit()
    log.info(
        "sensor_reading_created",
        actor=identity.user_id,
        sensor_reading_id=reading.id,
    )
    return reading


@readings_router.get("", response_model_by_alias=True)
async def list_sensor_readings(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
    sensor_id: Annotated[
        str | None,
        Query(alias="sensorId", description="Filter to one sensor."),
    ] = None,
    room_id: Annotated[
        str | None, Query(alias="roomId", description="Filter to one room.")
    ] = None,
    batch_id: Annotated[
        str | None, Query(alias="batchId", description="Filter to one batch.")
    ] = None,
    since: Annotated[
        int | None,
        Query(description="Inclusive lower bound on timestamp (epoch ms)."),
    ] = None,
    until: Annotated[
        int | None,
        Query(description="Exclusive upper bound on timestamp (epoch ms)."),
    ] = None,
) -> dict[str, Any]:
    """List sensor readings in the current tenant, optionally filtered."""
    settings = get_settings()
    stmt = select(SensorReading).where(
        SensorReading.org_id == settings.ocs_org_id
    )
    if sensor_id is not None:
        stmt = stmt.where(SensorReading.sensor_id == sensor_id)
    if room_id is not None:
        stmt = stmt.where(SensorReading.room_id == room_id)
    if batch_id is not None:
        stmt = stmt.where(SensorReading.batch_id == batch_id)
    if since is not None:
        stmt = stmt.where(SensorReading.timestamp >= since)
    if until is not None:
        stmt = stmt.where(SensorReading.timestamp < until)
    rows = (
        await session.execute(stmt.order_by(SensorReading.timestamp))
    ).scalars()
    items = [
        SensorReadingRead.model_validate(r).model_dump(
            mode="json", by_alias=True
        )
        for r in rows
    ]
    log.info(
        "sensor_readings_listed",
        actor=identity.user_id,
        count=len(items),
        sensor_id=sensor_id,
        since=since,
        until=until,
    )
    return {"sensorReadings": items}


@readings_router.get(
    "/{reading_id}",
    response_model=SensorReadingRead,
    response_model_by_alias=True,
)
async def get_sensor_reading(
    reading_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SensorReading:
    """Fetch one sensor reading by id."""
    settings = get_settings()
    reading = await session.get(SensorReading, reading_id)
    if reading is None or reading.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"sensor reading '{reading_id}' not found",
        )
    log.info(
        "sensor_reading_fetched",
        actor=identity.user_id,
        sensor_reading_id=reading_id,
    )
    return reading


@readings_router.put(
    "/{reading_id}",
    response_model=SensorReadingRead,
    response_model_by_alias=True,
)
async def update_sensor_reading(
    reading_id: str,
    body: SensorReadingCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SensorReading:
    """Replace a sensor reading's mutable fields."""
    settings = get_settings()
    reading = await session.get(SensorReading, reading_id)
    if reading is None or reading.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"sensor reading '{reading_id}' not found",
        )
    for field, value in body.model_dump(by_alias=False).items():
        setattr(reading, field, value)
    await session.flush()
    await session.refresh(reading)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Sensor reading {reading_id} updated",
        params={"sensor_reading_id": reading.id},
    )
    await session.commit()
    log.info(
        "sensor_reading_updated",
        actor=identity.user_id,
        sensor_reading_id=reading_id,
    )
    return reading


@readings_router.delete("/{reading_id}", status_code=status.HTTP_200_OK)
async def delete_sensor_reading(
    reading_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Delete one sensor reading."""
    settings = get_settings()
    reading = await session.get(SensorReading, reading_id)
    if reading is None or reading.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"sensor reading '{reading_id}' not found",
        )
    await session.delete(reading)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Sensor reading '{reading_id}' deleted",
        params={"sensor_reading_id": reading_id},
    )
    await session.commit()
    log.info(
        "sensor_reading_deleted",
        actor=identity.user_id,
        sensor_reading_id=reading_id,
    )
    return {"deleted": reading_id}


# ---------------------------------------------------------------------------
# Sensor integrations
# ---------------------------------------------------------------------------


integrations_router = APIRouter(
    prefix="/api/sensor-integrations", tags=["sensor-integrations"]
)


@integrations_router.post(
    "",
    response_model=SensorIntegrationRead,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def create_sensor_integration(
    body: SensorIntegrationCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SensorIntegration:
    """Create a sensor integration."""
    settings = get_settings()
    integration = SensorIntegration(
        org_id=settings.ocs_org_id,
        **body.model_dump(by_alias=False),
    )
    session.add(integration)
    await session.flush()
    await session.refresh(integration)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Sensor integration '{integration.name}' created",
        params={
            "sensor_integration_id": integration.id,
            "name": integration.name,
            "type": integration.type,
        },
    )
    await session.commit()
    log.info(
        "sensor_integration_created",
        actor=identity.user_id,
        sensor_integration_id=integration.id,
    )
    return integration


@integrations_router.get("", response_model_by_alias=True)
async def list_sensor_integrations(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
    type_: Annotated[
        str | None,
        Query(alias="type", description="Filter to one integration type."),
    ] = None,
    status_filter: Annotated[
        str | None,
        Query(alias="status", description="Filter to one status."),
    ] = None,
) -> dict[str, Any]:
    """List sensor integrations in the current tenant."""
    settings = get_settings()
    stmt = select(SensorIntegration).where(
        SensorIntegration.org_id == settings.ocs_org_id
    )
    if type_ is not None:
        stmt = stmt.where(SensorIntegration.type == type_)
    if status_filter is not None:
        stmt = stmt.where(SensorIntegration.status == status_filter)
    rows = (
        await session.execute(stmt.order_by(SensorIntegration.created_at))
    ).scalars()
    items = [
        SensorIntegrationRead.model_validate(i).model_dump(
            mode="json", by_alias=True
        )
        for i in rows
    ]
    log.info(
        "sensor_integrations_listed",
        actor=identity.user_id,
        count=len(items),
        type=type_,
        status=status_filter,
    )
    return {"sensorIntegrations": items}


@integrations_router.get(
    "/{integration_id}",
    response_model=SensorIntegrationRead,
    response_model_by_alias=True,
)
async def get_sensor_integration(
    integration_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SensorIntegration:
    """Fetch one sensor integration by id."""
    settings = get_settings()
    integration = await session.get(SensorIntegration, integration_id)
    if integration is None or integration.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"sensor integration '{integration_id}' not found",
        )
    log.info(
        "sensor_integration_fetched",
        actor=identity.user_id,
        sensor_integration_id=integration_id,
    )
    return integration


@integrations_router.put(
    "/{integration_id}",
    response_model=SensorIntegrationRead,
    response_model_by_alias=True,
)
async def update_sensor_integration(
    integration_id: str,
    body: SensorIntegrationCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SensorIntegration:
    """Replace a sensor integration's mutable fields."""
    settings = get_settings()
    integration = await session.get(SensorIntegration, integration_id)
    if integration is None or integration.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"sensor integration '{integration_id}' not found",
        )
    for field, value in body.model_dump(by_alias=False).items():
        setattr(integration, field, value)
    await session.flush()
    await session.refresh(integration)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Sensor integration '{integration.name}' updated",
        params={
            "sensor_integration_id": integration.id,
            "name": integration.name,
        },
    )
    await session.commit()
    log.info(
        "sensor_integration_updated",
        actor=identity.user_id,
        sensor_integration_id=integration_id,
    )
    return integration


@integrations_router.delete(
    "/{integration_id}",
    status_code=status.HTTP_200_OK,
)
async def delete_sensor_integration(
    integration_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Delete a sensor integration."""
    settings = get_settings()
    integration = await session.get(SensorIntegration, integration_id)
    if integration is None or integration.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"sensor integration '{integration_id}' not found",
        )
    await session.delete(integration)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Sensor integration '{integration_id}' deleted",
        params={"sensor_integration_id": integration_id},
    )
    await session.commit()
    log.info(
        "sensor_integration_deleted",
        actor=identity.user_id,
        sensor_integration_id=integration_id,
    )
    return {"deleted": integration_id}
