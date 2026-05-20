"""Sites tier REST API — buildings / rooms / locations.

Three Convex-aligned entities live under ``/api/sites``:

* ``/api/sites/buildings`` — top of the facility hierarchy.
* ``/api/sites/rooms`` — Convex-aligned room entity (NOT the legacy
  ``app.api.rooms`` ``room_runtime`` API the existing frontend uses).
* ``/api/sites/locations`` — leaf tier (rows, benches, sensor mounts).

Every endpoint is gated on the ``admin`` role — facility topology is a
Class-E control surface (the AI never writes it). Mutations write an
``info_event`` audit row; reads stay quiet.

JSON wire shape is camelCase + epoch-ms timestamps so it is
byte-compatible with a Convex document. Snake_case input is also
accepted because ``populate_by_name=True`` is set on every schema.

The ``orgId`` is always server-stamped from
:attr:`app.config.Settings.ocs_org_id` so a client cannot scope a new
row into a tenant it does not own. List queries filter by the same
org id for the same reason.
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
from app.models.building import Building
from app.models.location import Location
from app.models.room import Room
from app.models.user import RoleName
from app.schemas.sites import (
    BuildingCreate,
    BuildingRead,
    LocationCreate,
    LocationRead,
    RoomCreate,
    RoomRead,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/sites", tags=["sites"])


# ---------------------------------------------------------------------------
# Buildings
# ---------------------------------------------------------------------------


@router.post(
    "/buildings",
    response_model=BuildingRead,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def create_building(
    body: BuildingCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Building:
    """Create a building. ``orgId`` is server-stamped from settings."""
    settings = get_settings()
    building = Building(
        org_id=settings.ocs_org_id,
        **body.model_dump(by_alias=False, exclude_unset=False),
    )
    session.add(building)
    await session.flush()
    await session.refresh(building)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Building '{building.name}' created",
        params={"building_id": building.id, "name": building.name},
    )
    await session.commit()
    log.info("building_created", actor=identity.user_id, building_id=building.id)
    return building


@router.get(
    "/buildings",
    response_model_by_alias=True,
)
async def list_buildings(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """List every building in the current tenant."""
    settings = get_settings()
    rows = (
        await session.execute(
            select(Building)
            .where(Building.org_id == settings.ocs_org_id)
            .order_by(Building.created_at)
        )
    ).scalars()
    items = [
        BuildingRead.model_validate(b).model_dump(mode="json", by_alias=True)
        for b in rows
    ]
    log.info("buildings_listed", actor=identity.user_id, count=len(items))
    return {"buildings": items}


@router.get(
    "/buildings/{building_id}",
    response_model=BuildingRead,
    response_model_by_alias=True,
)
async def get_building(
    building_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Building:
    """Fetch a single building by id."""
    settings = get_settings()
    building = await session.get(Building, building_id)
    if building is None or building.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"building '{building_id}' not found",
        )
    log.info(
        "building_fetched", actor=identity.user_id, building_id=building_id
    )
    return building


@router.put(
    "/buildings/{building_id}",
    response_model=BuildingRead,
    response_model_by_alias=True,
)
async def update_building(
    building_id: str,
    body: BuildingCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Building:
    """Replace a building's mutable fields."""
    settings = get_settings()
    building = await session.get(Building, building_id)
    if building is None or building.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"building '{building_id}' not found",
        )
    for field, value in body.model_dump(by_alias=False).items():
        setattr(building, field, value)
    await session.flush()
    await session.refresh(building)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Building '{building.name}' updated",
        params={"building_id": building.id, "name": building.name},
    )
    await session.commit()
    log.info("building_updated", actor=identity.user_id, building_id=building_id)
    return building


@router.delete(
    "/buildings/{building_id}",
    status_code=status.HTTP_200_OK,
)
async def delete_building(
    building_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Delete a building. Rooms / locations cascade per the FK."""
    settings = get_settings()
    building = await session.get(Building, building_id)
    if building is None or building.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"building '{building_id}' not found",
        )
    await session.delete(building)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Building '{building_id}' deleted",
        params={"building_id": building_id},
    )
    await session.commit()
    log.info("building_deleted", actor=identity.user_id, building_id=building_id)
    return {"deleted": building_id}


# ---------------------------------------------------------------------------
# Rooms (the Convex-aligned room entity, not the legacy room_runtime)
# ---------------------------------------------------------------------------


@router.post(
    "/rooms",
    response_model=RoomRead,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def create_room(
    body: RoomCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Room:
    """Create a room scoped to one building."""
    settings = get_settings()
    room = Room(
        org_id=settings.ocs_org_id,
        **body.model_dump(by_alias=False),
    )
    session.add(room)
    await session.flush()
    await session.refresh(room)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Room '{room.name}' created",
        params={
            "room_id": room.id,
            "building_id": room.building_id,
            "name": room.name,
        },
    )
    await session.commit()
    log.info("room_created", actor=identity.user_id, room_id=room.id)
    return room


@router.get(
    "/rooms",
    response_model_by_alias=True,
)
async def list_rooms(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
    building_id: Annotated[
        str | None,
        Query(alias="buildingId", description="Filter to one building."),
    ] = None,
) -> dict[str, Any]:
    """List rooms in the current tenant, optionally filtered by building."""
    settings = get_settings()
    stmt = select(Room).where(Room.org_id == settings.ocs_org_id)
    if building_id is not None:
        stmt = stmt.where(Room.building_id == building_id)
    rows = (await session.execute(stmt.order_by(Room.created_at))).scalars()
    items = [
        RoomRead.model_validate(r).model_dump(mode="json", by_alias=True)
        for r in rows
    ]
    log.info(
        "rooms_listed",
        actor=identity.user_id,
        count=len(items),
        building_id=building_id,
    )
    return {"rooms": items}


@router.get(
    "/rooms/{room_id}",
    response_model=RoomRead,
    response_model_by_alias=True,
)
async def get_room(
    room_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Room:
    """Fetch a single room by id."""
    settings = get_settings()
    room = await session.get(Room, room_id)
    if room is None or room.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"room '{room_id}' not found",
        )
    log.info("room_fetched", actor=identity.user_id, room_id=room_id)
    return room


@router.put(
    "/rooms/{room_id}",
    response_model=RoomRead,
    response_model_by_alias=True,
)
async def update_room(
    room_id: str,
    body: RoomCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Room:
    """Replace a room's mutable fields."""
    settings = get_settings()
    room = await session.get(Room, room_id)
    if room is None or room.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"room '{room_id}' not found",
        )
    for field, value in body.model_dump(by_alias=False).items():
        setattr(room, field, value)
    await session.flush()
    await session.refresh(room)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Room '{room.name}' updated",
        params={"room_id": room.id, "name": room.name},
    )
    await session.commit()
    log.info("room_updated", actor=identity.user_id, room_id=room_id)
    return room


@router.delete(
    "/rooms/{room_id}",
    status_code=status.HTTP_200_OK,
)
async def delete_room(
    room_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Delete a room. Locations cascade per the FK."""
    settings = get_settings()
    room = await session.get(Room, room_id)
    if room is None or room.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"room '{room_id}' not found",
        )
    await session.delete(room)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Room '{room_id}' deleted",
        params={"room_id": room_id},
    )
    await session.commit()
    log.info("room_deleted", actor=identity.user_id, room_id=room_id)
    return {"deleted": room_id}


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


@router.post(
    "/locations",
    response_model=LocationRead,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def create_location(
    body: LocationCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Location:
    """Create a location inside one room."""
    settings = get_settings()
    location = Location(
        org_id=settings.ocs_org_id,
        **body.model_dump(by_alias=False),
    )
    session.add(location)
    await session.flush()
    await session.refresh(location)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Location '{location.label}' created",
        params={
            "location_id": location.id,
            "room_id": location.room_id,
            "label": location.label,
        },
    )
    await session.commit()
    log.info(
        "location_created", actor=identity.user_id, location_id=location.id
    )
    return location


@router.get(
    "/locations",
    response_model_by_alias=True,
)
async def list_locations(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
    room_id: Annotated[
        str | None,
        Query(alias="roomId", description="Filter to one room."),
    ] = None,
) -> dict[str, Any]:
    """List locations in the current tenant, optionally filtered by room."""
    settings = get_settings()
    stmt = select(Location).where(Location.org_id == settings.ocs_org_id)
    if room_id is not None:
        stmt = stmt.where(Location.room_id == room_id)
    rows = (await session.execute(stmt.order_by(Location.created_at))).scalars()
    items = [
        LocationRead.model_validate(loc).model_dump(mode="json", by_alias=True)
        for loc in rows
    ]
    log.info(
        "locations_listed",
        actor=identity.user_id,
        count=len(items),
        room_id=room_id,
    )
    return {"locations": items}


@router.get(
    "/locations/{location_id}",
    response_model=LocationRead,
    response_model_by_alias=True,
)
async def get_location(
    location_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Location:
    """Fetch a single location by id."""
    settings = get_settings()
    location = await session.get(Location, location_id)
    if location is None or location.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"location '{location_id}' not found",
        )
    log.info(
        "location_fetched", actor=identity.user_id, location_id=location_id
    )
    return location


@router.put(
    "/locations/{location_id}",
    response_model=LocationRead,
    response_model_by_alias=True,
)
async def update_location(
    location_id: str,
    body: LocationCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Location:
    """Replace a location's mutable fields."""
    settings = get_settings()
    location = await session.get(Location, location_id)
    if location is None or location.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"location '{location_id}' not found",
        )
    for field, value in body.model_dump(by_alias=False).items():
        setattr(location, field, value)
    await session.flush()
    await session.refresh(location)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Location '{location.label}' updated",
        params={"location_id": location.id, "label": location.label},
    )
    await session.commit()
    log.info(
        "location_updated", actor=identity.user_id, location_id=location_id
    )
    return location


@router.delete(
    "/locations/{location_id}",
    status_code=status.HTTP_200_OK,
)
async def delete_location(
    location_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Delete a location."""
    settings = get_settings()
    location = await session.get(Location, location_id)
    if location is None or location.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"location '{location_id}' not found",
        )
    await session.delete(location)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Location '{location_id}' deleted",
        params={"location_id": location_id},
    )
    await session.commit()
    log.info(
        "location_deleted", actor=identity.user_id, location_id=location_id
    )
    return {"deleted": location_id}
