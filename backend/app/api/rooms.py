"""Room-configuration API — the entity picker's backend.

The supervisor needs to know which Home Assistant entity is a room's
temp sensor, lights switch, per-zone VWC probe, and so on. v0.1
deferred this "room store"; this router is it.

* ``GET  /api/rooms/ha-registry`` — every HA area + entity (merged with
  current state) for the GUI entity picker to choose from.
* ``GET  /api/rooms``            — the configured rooms + their maps.
* ``PUT  /api/rooms/{room_id}``  — save a room's equipment map.
* ``DELETE /api/rooms/{room_id}``— remove a room.

The saved :class:`~app.api.config_wizard.RoomEquipmentMap` lands in
``room_runtime.equipment_map``; ``app.cli``'s ``_rooms_provider`` builds
the supervisor's room list from it.

Every route requires ``admin`` — equipment mapping is a Class-E surface
(plan v3 #10/#11): the AI never writes it, only admins do.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.config_wizard import RoomEquipmentMap
from app.core.acl import require_role
from app.core.audit import log_audit
from app.core.auth import Identity
from app.db import get_session
from app.ha_client import HAClient, HAClientError
from app.models.audit_event import AuditEventType
from app.models.room_runtime import RoomRuntime
from app.models.user import RoleName

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/rooms", tags=["rooms"])


class SaveRoomBody(BaseModel):
    """PUT payload — a room's display name + its equipment map."""

    display_name: str | None = None
    equipment_map: RoomEquipmentMap


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def _room_dict(room: RoomRuntime) -> dict[str, Any]:
    return {
        "room_id": room.room_id,
        "display_name": room.display_name,
        "rollout_stage": room.rollout_stage,
        "current_state": room.current_state,
        "equipment_map": room.equipment_map,
    }


@router.get("/ha-registry")
async def ha_registry(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
) -> dict[str, Any]:
    """List every HA area + entity for the entity picker.

    Merges the WS area/entity/device registry with current REST states
    so each entity carries its area, domain, friendly name and live
    value — enough for the picker to show "sensor.f1_…_temperature =
    24.1 °C  [F1]".
    """
    try:
        async with HAClient() as ha:
            reg = await ha.list_registry()
            states = await ha.get_states()
    except (HAClientError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Home Assistant registry fetch failed: {exc}",
        ) from exc

    area_name = {a["area_id"]: a.get("name") or a["area_id"] for a in reg["areas"]}
    dev_area = {d["id"]: d.get("area_id") for d in reg["devices"]}
    state_by_id = {s["entity_id"]: s for s in states}

    entities: list[dict[str, Any]] = []
    for ent in reg["entities"]:
        eid = ent["entity_id"]
        aid = ent.get("area_id") or dev_area.get(ent.get("device_id"))
        st = state_by_id.get(eid, {})
        attrs = st.get("attributes", {})
        entities.append(
            {
                "entity_id": eid,
                "name": (
                    attrs.get("friendly_name")
                    or ent.get("name")
                    or ent.get("original_name")
                    or eid
                ),
                "domain": _domain(eid),
                "area": area_name.get(aid),
                "state": st.get("state"),
                "unit": attrs.get("unit_of_measurement"),
            }
        )
    entities.sort(key=lambda e: (e["area"] or "~", e["entity_id"]))

    areas = sorted(
        (
            {"area_id": a["area_id"], "name": a.get("name") or a["area_id"]}
            for a in reg["areas"]
        ),
        key=lambda a: a["name"].lower(),
    )
    log.info(
        "ha_registry_listed",
        actor=identity.user_id,
        areas=len(areas),
        entities=len(entities),
    )
    return {"areas": areas, "entities": entities}


@router.get("")
async def list_rooms(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """List every configured room with its saved equipment map."""
    rows = (
        await session.execute(select(RoomRuntime).order_by(RoomRuntime.room_id))
    ).scalars()
    rooms = [_room_dict(r) for r in rows]
    log.info("rooms_listed", actor=identity.user_id, count=len(rooms))
    return {"rooms": rooms}


@router.put("/{room_id}", status_code=status.HTTP_200_OK)
async def save_room(
    room_id: str,
    body: SaveRoomBody,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Create-or-update a room's equipment map (the entity assignments)."""
    if body.equipment_map.room_id != room_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"equipment_map.room_id '{body.equipment_map.room_id}' "
            f"!= path room_id '{room_id}'",
        )

    room = await session.get(RoomRuntime, room_id)
    created = room is None
    if room is None:
        room = RoomRuntime(room_id=room_id)
        session.add(room)
    room.equipment_map = body.equipment_map.model_dump(mode="json")
    if body.display_name is not None:
        room.display_name = body.display_name
    await session.flush()

    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        room_id=room_id,
        summary=(
            f"Room equipment map {'created' if created else 'updated'} "
            f"for '{room_id}'"
        ),
        params={"equipment_map": room.equipment_map},
    )
    await session.commit()
    log.info(
        "room_saved", actor=identity.user_id, room_id=room_id, created=created
    )
    return _room_dict(room)


@router.delete("/{room_id}", status_code=status.HTTP_200_OK)
async def delete_room(
    room_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Remove a configured room."""
    room = await session.get(RoomRuntime, room_id)
    if room is None:
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
        room_id=room_id,
        summary=f"Room '{room_id}' removed",
    )
    await session.commit()
    log.info("room_deleted", actor=identity.user_id, room_id=room_id)
    return {"deleted": room_id}
