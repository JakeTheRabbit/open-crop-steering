"""Equipment tier REST API — durable assets (HVAC, lighting, …).

A single Convex-aligned entity served under ``/api/equipment``.

Standard ``POST / GET-list / GET-one / PUT / DELETE`` surface. Every
endpoint requires the ``admin`` role (Class-E entity). Mutations write
an ``info_event`` audit row; reads stay quiet.

JSON wire shape is camelCase + epoch-ms timestamps so it is
byte-compatible with a Convex document. Snake_case input is also
accepted because ``populate_by_name=True`` is set on the schema.
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
from app.models.equipment import Equipment
from app.models.user import RoleName
from app.schemas.equipment import EquipmentCreate, EquipmentRead

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/equipment", tags=["equipment"])


@router.post(
    "",
    response_model=EquipmentRead,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def create_equipment(
    body: EquipmentCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Equipment:
    """Create an equipment record."""
    settings = get_settings()
    equipment = Equipment(
        org_id=settings.ocs_org_id,
        **body.model_dump(by_alias=False),
    )
    session.add(equipment)
    await session.flush()
    await session.refresh(equipment)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Equipment '{equipment.name}' created",
        params={
            "equipment_id": equipment.id,
            "name": equipment.name,
            "type": equipment.type,
        },
    )
    await session.commit()
    log.info(
        "equipment_created",
        actor=identity.user_id,
        equipment_id=equipment.id,
    )
    return equipment


@router.get("", response_model_by_alias=True)
async def list_equipment(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
    room_id: Annotated[
        str | None, Query(alias="roomId", description="Filter to one room.")
    ] = None,
    type_: Annotated[
        str | None,
        Query(alias="type", description="Filter to one equipment type."),
    ] = None,
    status_filter: Annotated[
        str | None,
        Query(alias="status", description="Filter to one status."),
    ] = None,
) -> dict[str, Any]:
    """List equipment in the current tenant, optionally filtered."""
    settings = get_settings()
    stmt = select(Equipment).where(Equipment.org_id == settings.ocs_org_id)
    if room_id is not None:
        stmt = stmt.where(Equipment.room_id == room_id)
    if type_ is not None:
        stmt = stmt.where(Equipment.type == type_)
    if status_filter is not None:
        stmt = stmt.where(Equipment.status == status_filter)
    rows = (
        await session.execute(stmt.order_by(Equipment.created_at))
    ).scalars()
    items = [
        EquipmentRead.model_validate(e).model_dump(mode="json", by_alias=True)
        for e in rows
    ]
    log.info(
        "equipment_listed",
        actor=identity.user_id,
        count=len(items),
        room_id=room_id,
        type=type_,
        status=status_filter,
    )
    return {"equipment": items}


@router.get(
    "/{equipment_id}",
    response_model=EquipmentRead,
    response_model_by_alias=True,
)
async def get_equipment(
    equipment_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Equipment:
    """Fetch one equipment record by id."""
    settings = get_settings()
    equipment = await session.get(Equipment, equipment_id)
    if equipment is None or equipment.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"equipment '{equipment_id}' not found",
        )
    log.info(
        "equipment_fetched",
        actor=identity.user_id,
        equipment_id=equipment_id,
    )
    return equipment


@router.put(
    "/{equipment_id}",
    response_model=EquipmentRead,
    response_model_by_alias=True,
)
async def update_equipment(
    equipment_id: str,
    body: EquipmentCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Equipment:
    """Replace an equipment record's mutable fields."""
    settings = get_settings()
    equipment = await session.get(Equipment, equipment_id)
    if equipment is None or equipment.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"equipment '{equipment_id}' not found",
        )
    for field, value in body.model_dump(by_alias=False).items():
        setattr(equipment, field, value)
    await session.flush()
    await session.refresh(equipment)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Equipment '{equipment.name}' updated",
        params={"equipment_id": equipment.id, "name": equipment.name},
    )
    await session.commit()
    log.info(
        "equipment_updated",
        actor=identity.user_id,
        equipment_id=equipment_id,
    )
    return equipment


@router.delete(
    "/{equipment_id}",
    status_code=status.HTTP_200_OK,
)
async def delete_equipment(
    equipment_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Delete an equipment record."""
    settings = get_settings()
    equipment = await session.get(Equipment, equipment_id)
    if equipment is None or equipment.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"equipment '{equipment_id}' not found",
        )
    await session.delete(equipment)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Equipment '{equipment_id}' deleted",
        params={"equipment_id": equipment_id},
    )
    await session.commit()
    log.info(
        "equipment_deleted",
        actor=identity.user_id,
        equipment_id=equipment_id,
    )
    return {"deleted": equipment_id}
