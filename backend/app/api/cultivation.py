"""Cultivation tier REST API — genetics / batches / plants / grow recipes.

Four Convex-aligned entities served under ``/api/cultivation/*``:

* ``/api/cultivation/genetics`` — strain definitions.
* ``/api/cultivation/batches`` — grow cycles.
* ``/api/cultivation/plants`` — individual tracked plants.
* ``/api/cultivation/grow-recipes`` — phase-based cultivation protocols.

Standard ``POST / GET-list / GET-one / PUT / DELETE`` surface per
entity. Every endpoint requires the ``admin`` role (Class-E entity).
Mutations write an ``info_event`` audit row; reads stay quiet.

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
from app.models.batch import Batch
from app.models.genetics import Genetics
from app.models.grow_recipe import GrowRecipe
from app.models.plant import Plant
from app.models.user import RoleName
from app.schemas.cultivation import (
    BatchCreate,
    BatchRead,
    GeneticsCreate,
    GeneticsRead,
    GrowRecipeCreate,
    GrowRecipeRead,
    PlantCreate,
    PlantRead,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/cultivation", tags=["cultivation"])


# ---------------------------------------------------------------------------
# Genetics
# ---------------------------------------------------------------------------


@router.post(
    "/genetics",
    response_model=GeneticsRead,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def create_genetics(
    body: GeneticsCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Genetics:
    """Create a genetics row."""
    settings = get_settings()
    genetics = Genetics(
        org_id=settings.ocs_org_id,
        **body.model_dump(by_alias=False),
    )
    session.add(genetics)
    await session.flush()
    await session.refresh(genetics)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Genetics '{genetics.name}' created",
        params={
            "genetics_id": genetics.id,
            "name": genetics.name,
            "prefix": genetics.prefix,
        },
    )
    await session.commit()
    log.info(
        "genetics_created",
        actor=identity.user_id,
        genetics_id=genetics.id,
    )
    return genetics


@router.get("/genetics", response_model_by_alias=True)
async def list_genetics(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[
        str | None,
        Query(alias="status", description="Filter to one status."),
    ] = None,
    type_: Annotated[
        str | None,
        Query(alias="type", description="Filter to one type."),
    ] = None,
    prefix: Annotated[
        str | None, Query(description="Filter to one prefix.")
    ] = None,
) -> dict[str, Any]:
    """List genetics in the current tenant, optionally filtered."""
    settings = get_settings()
    stmt = select(Genetics).where(Genetics.org_id == settings.ocs_org_id)
    if status_filter is not None:
        stmt = stmt.where(Genetics.status == status_filter)
    if type_ is not None:
        stmt = stmt.where(Genetics.type == type_)
    if prefix is not None:
        stmt = stmt.where(Genetics.prefix == prefix)
    rows = (
        await session.execute(stmt.order_by(Genetics.created_at))
    ).scalars()
    items = [
        GeneticsRead.model_validate(g).model_dump(mode="json", by_alias=True)
        for g in rows
    ]
    log.info(
        "genetics_listed",
        actor=identity.user_id,
        count=len(items),
        status=status_filter,
        type=type_,
        prefix=prefix,
    )
    return {"genetics": items}


@router.get(
    "/genetics/{genetics_id}",
    response_model=GeneticsRead,
    response_model_by_alias=True,
)
async def get_genetics(
    genetics_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Genetics:
    """Fetch one genetics row by id."""
    settings = get_settings()
    genetics = await session.get(Genetics, genetics_id)
    if genetics is None or genetics.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"genetics '{genetics_id}' not found",
        )
    log.info(
        "genetics_fetched", actor=identity.user_id, genetics_id=genetics_id
    )
    return genetics


@router.put(
    "/genetics/{genetics_id}",
    response_model=GeneticsRead,
    response_model_by_alias=True,
)
async def update_genetics(
    genetics_id: str,
    body: GeneticsCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Genetics:
    """Replace a genetics row's mutable fields."""
    settings = get_settings()
    genetics = await session.get(Genetics, genetics_id)
    if genetics is None or genetics.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"genetics '{genetics_id}' not found",
        )
    for field, value in body.model_dump(by_alias=False).items():
        setattr(genetics, field, value)
    await session.flush()
    await session.refresh(genetics)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Genetics '{genetics.name}' updated",
        params={"genetics_id": genetics.id, "name": genetics.name},
    )
    await session.commit()
    log.info(
        "genetics_updated", actor=identity.user_id, genetics_id=genetics_id
    )
    return genetics


@router.delete(
    "/genetics/{genetics_id}",
    status_code=status.HTTP_200_OK,
)
async def delete_genetics(
    genetics_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Delete a genetics row. Batches and plants cascade per the FK."""
    settings = get_settings()
    genetics = await session.get(Genetics, genetics_id)
    if genetics is None or genetics.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"genetics '{genetics_id}' not found",
        )
    await session.delete(genetics)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Genetics '{genetics_id}' deleted",
        params={"genetics_id": genetics_id},
    )
    await session.commit()
    log.info(
        "genetics_deleted", actor=identity.user_id, genetics_id=genetics_id
    )
    return {"deleted": genetics_id}


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------


@router.post(
    "/batches",
    response_model=BatchRead,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def create_batch(
    body: BatchCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Batch:
    """Create a batch."""
    settings = get_settings()
    batch = Batch(
        org_id=settings.ocs_org_id,
        **body.model_dump(by_alias=False),
    )
    session.add(batch)
    await session.flush()
    await session.refresh(batch)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Batch '{batch.batch_code}' created",
        params={
            "batch_id": batch.id,
            "batch_code": batch.batch_code,
            "genetics": batch.genetics,
        },
    )
    await session.commit()
    log.info("batch_created", actor=identity.user_id, batch_id=batch.id)
    return batch


@router.get("/batches", response_model_by_alias=True)
async def list_batches(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[
        str | None,
        Query(alias="status", description="Filter to one status."),
    ] = None,
    genetics: Annotated[
        str | None, Query(description="Filter to one genetics id.")
    ] = None,
    grow_recipe: Annotated[
        str | None,
        Query(alias="growRecipe", description="Filter to one grow recipe id."),
    ] = None,
) -> dict[str, Any]:
    """List batches in the current tenant, optionally filtered."""
    settings = get_settings()
    stmt = select(Batch).where(Batch.org_id == settings.ocs_org_id)
    if status_filter is not None:
        stmt = stmt.where(Batch.status == status_filter)
    if genetics is not None:
        stmt = stmt.where(Batch.genetics == genetics)
    if grow_recipe is not None:
        stmt = stmt.where(Batch.grow_recipe == grow_recipe)
    rows = (await session.execute(stmt.order_by(Batch.created_at))).scalars()
    items = [
        BatchRead.model_validate(b).model_dump(mode="json", by_alias=True)
        for b in rows
    ]
    log.info(
        "batches_listed",
        actor=identity.user_id,
        count=len(items),
        status=status_filter,
        genetics=genetics,
        grow_recipe=grow_recipe,
    )
    return {"batches": items}


@router.get(
    "/batches/{batch_id}",
    response_model=BatchRead,
    response_model_by_alias=True,
)
async def get_batch(
    batch_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Batch:
    """Fetch one batch by id."""
    settings = get_settings()
    batch = await session.get(Batch, batch_id)
    if batch is None or batch.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"batch '{batch_id}' not found",
        )
    log.info("batch_fetched", actor=identity.user_id, batch_id=batch_id)
    return batch


@router.put(
    "/batches/{batch_id}",
    response_model=BatchRead,
    response_model_by_alias=True,
)
async def update_batch(
    batch_id: str,
    body: BatchCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Batch:
    """Replace a batch's mutable fields."""
    settings = get_settings()
    batch = await session.get(Batch, batch_id)
    if batch is None or batch.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"batch '{batch_id}' not found",
        )
    for field, value in body.model_dump(by_alias=False).items():
        setattr(batch, field, value)
    await session.flush()
    await session.refresh(batch)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Batch '{batch.batch_code}' updated",
        params={"batch_id": batch.id, "batch_code": batch.batch_code},
    )
    await session.commit()
    log.info("batch_updated", actor=identity.user_id, batch_id=batch_id)
    return batch


@router.delete(
    "/batches/{batch_id}",
    status_code=status.HTTP_200_OK,
)
async def delete_batch(
    batch_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Delete a batch. Plant ``current_batch`` is set to NULL per the FK."""
    settings = get_settings()
    batch = await session.get(Batch, batch_id)
    if batch is None or batch.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"batch '{batch_id}' not found",
        )
    await session.delete(batch)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Batch '{batch_id}' deleted",
        params={"batch_id": batch_id},
    )
    await session.commit()
    log.info("batch_deleted", actor=identity.user_id, batch_id=batch_id)
    return {"deleted": batch_id}


# ---------------------------------------------------------------------------
# Plants
# ---------------------------------------------------------------------------


@router.post(
    "/plants",
    response_model=PlantRead,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def create_plant(
    body: PlantCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Plant:
    """Create a plant record."""
    settings = get_settings()
    plant = Plant(
        org_id=settings.ocs_org_id,
        **body.model_dump(by_alias=False),
    )
    session.add(plant)
    await session.flush()
    await session.refresh(plant)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Plant '{plant.plant_id}' created",
        params={
            "plant_record_id": plant.id,
            "plant_id": plant.plant_id,
            "genetics": plant.genetics,
        },
    )
    await session.commit()
    log.info("plant_created", actor=identity.user_id, plant_id=plant.id)
    return plant


@router.get("/plants", response_model_by_alias=True)
async def list_plants(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
    batch_id: Annotated[
        str | None,
        Query(alias="batchId", description="Filter to one current_batch."),
    ] = None,
    status_filter: Annotated[
        str | None,
        Query(alias="status", description="Filter to one status."),
    ] = None,
    genetics: Annotated[
        str | None, Query(description="Filter to one genetics id.")
    ] = None,
    location_id: Annotated[
        str | None,
        Query(
            alias="locationId", description="Filter to one current_location."
        ),
    ] = None,
) -> dict[str, Any]:
    """List plants in the current tenant, optionally filtered."""
    settings = get_settings()
    stmt = select(Plant).where(Plant.org_id == settings.ocs_org_id)
    if batch_id is not None:
        stmt = stmt.where(Plant.current_batch == batch_id)
    if status_filter is not None:
        stmt = stmt.where(Plant.status == status_filter)
    if genetics is not None:
        stmt = stmt.where(Plant.genetics == genetics)
    if location_id is not None:
        stmt = stmt.where(Plant.current_location == location_id)
    rows = (await session.execute(stmt.order_by(Plant.created_at))).scalars()
    items = [
        PlantRead.model_validate(p).model_dump(mode="json", by_alias=True)
        for p in rows
    ]
    log.info(
        "plants_listed",
        actor=identity.user_id,
        count=len(items),
        batch_id=batch_id,
        status=status_filter,
        genetics=genetics,
        location_id=location_id,
    )
    return {"plants": items}


@router.get(
    "/plants/{plant_record_id}",
    response_model=PlantRead,
    response_model_by_alias=True,
)
async def get_plant(
    plant_record_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Plant:
    """Fetch one plant record by id."""
    settings = get_settings()
    plant = await session.get(Plant, plant_record_id)
    if plant is None or plant.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"plant '{plant_record_id}' not found",
        )
    log.info(
        "plant_fetched", actor=identity.user_id, plant_id=plant_record_id
    )
    return plant


@router.put(
    "/plants/{plant_record_id}",
    response_model=PlantRead,
    response_model_by_alias=True,
)
async def update_plant(
    plant_record_id: str,
    body: PlantCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Plant:
    """Replace a plant record's mutable fields."""
    settings = get_settings()
    plant = await session.get(Plant, plant_record_id)
    if plant is None or plant.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"plant '{plant_record_id}' not found",
        )
    for field, value in body.model_dump(by_alias=False).items():
        setattr(plant, field, value)
    await session.flush()
    await session.refresh(plant)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Plant '{plant.plant_id}' updated",
        params={"plant_record_id": plant.id, "plant_id": plant.plant_id},
    )
    await session.commit()
    log.info(
        "plant_updated", actor=identity.user_id, plant_id=plant_record_id
    )
    return plant


@router.delete(
    "/plants/{plant_record_id}",
    status_code=status.HTTP_200_OK,
)
async def delete_plant(
    plant_record_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Delete a plant record."""
    settings = get_settings()
    plant = await session.get(Plant, plant_record_id)
    if plant is None or plant.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"plant '{plant_record_id}' not found",
        )
    await session.delete(plant)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Plant '{plant_record_id}' deleted",
        params={"plant_record_id": plant_record_id},
    )
    await session.commit()
    log.info(
        "plant_deleted", actor=identity.user_id, plant_id=plant_record_id
    )
    return {"deleted": plant_record_id}


# ---------------------------------------------------------------------------
# Grow recipes
# ---------------------------------------------------------------------------


@router.post(
    "/grow-recipes",
    response_model=GrowRecipeRead,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def create_grow_recipe(
    body: GrowRecipeCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GrowRecipe:
    """Create a grow recipe."""
    settings = get_settings()
    recipe = GrowRecipe(
        org_id=settings.ocs_org_id,
        **body.model_dump(by_alias=False),
    )
    session.add(recipe)
    await session.flush()
    await session.refresh(recipe)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Grow recipe '{recipe.name}' created",
        params={"grow_recipe_id": recipe.id, "name": recipe.name},
    )
    await session.commit()
    log.info(
        "grow_recipe_created",
        actor=identity.user_id,
        grow_recipe_id=recipe.id,
    )
    return recipe


@router.get("/grow-recipes", response_model_by_alias=True)
async def list_grow_recipes(
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
    genetics: Annotated[
        str | None, Query(description="Filter to one genetics id.")
    ] = None,
    is_active: Annotated[
        bool | None,
        Query(alias="isActive", description="Filter on is_active flag."),
    ] = None,
) -> dict[str, Any]:
    """List grow recipes in the current tenant, optionally filtered."""
    settings = get_settings()
    stmt = select(GrowRecipe).where(GrowRecipe.org_id == settings.ocs_org_id)
    if genetics is not None:
        stmt = stmt.where(GrowRecipe.genetics == genetics)
    if is_active is not None:
        stmt = stmt.where(GrowRecipe.is_active == is_active)
    rows = (
        await session.execute(stmt.order_by(GrowRecipe.created_at))
    ).scalars()
    items = [
        GrowRecipeRead.model_validate(r).model_dump(mode="json", by_alias=True)
        for r in rows
    ]
    log.info(
        "grow_recipes_listed",
        actor=identity.user_id,
        count=len(items),
        genetics=genetics,
        is_active=is_active,
    )
    return {"growRecipes": items}


@router.get(
    "/grow-recipes/{recipe_id}",
    response_model=GrowRecipeRead,
    response_model_by_alias=True,
)
async def get_grow_recipe(
    recipe_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GrowRecipe:
    """Fetch one grow recipe by id."""
    settings = get_settings()
    recipe = await session.get(GrowRecipe, recipe_id)
    if recipe is None or recipe.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"grow recipe '{recipe_id}' not found",
        )
    log.info(
        "grow_recipe_fetched",
        actor=identity.user_id,
        grow_recipe_id=recipe_id,
    )
    return recipe


@router.put(
    "/grow-recipes/{recipe_id}",
    response_model=GrowRecipeRead,
    response_model_by_alias=True,
)
async def update_grow_recipe(
    recipe_id: str,
    body: GrowRecipeCreate,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GrowRecipe:
    """Replace a grow recipe's mutable fields."""
    settings = get_settings()
    recipe = await session.get(GrowRecipe, recipe_id)
    if recipe is None or recipe.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"grow recipe '{recipe_id}' not found",
        )
    for field, value in body.model_dump(by_alias=False).items():
        setattr(recipe, field, value)
    await session.flush()
    await session.refresh(recipe)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Grow recipe '{recipe.name}' updated",
        params={"grow_recipe_id": recipe.id, "name": recipe.name},
    )
    await session.commit()
    log.info(
        "grow_recipe_updated",
        actor=identity.user_id,
        grow_recipe_id=recipe_id,
    )
    return recipe


@router.delete(
    "/grow-recipes/{recipe_id}",
    status_code=status.HTTP_200_OK,
)
async def delete_grow_recipe(
    recipe_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Delete a grow recipe. Batches' ``grow_recipe`` is SET NULL."""
    settings = get_settings()
    recipe = await session.get(GrowRecipe, recipe_id)
    if recipe is None or recipe.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"grow recipe '{recipe_id}' not found",
        )
    await session.delete(recipe)
    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=f"Grow recipe '{recipe_id}' deleted",
        params={"grow_recipe_id": recipe_id},
    )
    await session.commit()
    log.info(
        "grow_recipe_deleted",
        actor=identity.user_id,
        grow_recipe_id=recipe_id,
    )
    return {"deleted": recipe_id}
