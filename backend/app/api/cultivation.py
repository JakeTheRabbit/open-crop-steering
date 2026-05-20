"""Cultivation tier REST API — genetics / batches / plants / grow recipes.

Four Convex-aligned entities served under ``/api/cultivation/*``:

* ``/api/cultivation/genetics`` — strain definitions.
* ``/api/cultivation/batches`` — grow cycles.
* ``/api/cultivation/plants`` — individual tracked plants.
* ``/api/cultivation/grow-recipes`` — phase-based cultivation protocols.

Standard ``POST / GET-list / GET-one / PUT / DELETE`` surface per
entity. Every endpoint requires the ``admin`` role (Class-E entity).
Mutations write an ``info_event`` audit row; reads stay quiet.

Grow recipes additionally expose two OCS-only routes for per-day
overrides:

* ``GET  /grow-recipes/{id}/effective-targets`` — phase-default +
  override merge, returning the dense ``(day, paramName)`` grid (or one
  ``day=N`` row when the query param is supplied).
* ``PUT  /grow-recipes/{id}/day-overrides`` — bulk-replace this recipe's
  overrides atomically (DELETE-then-INSERT in one transaction).

JSON wire shape is camelCase + epoch-ms timestamps so it is
byte-compatible with a Convex document. Snake_case input is also
accepted because ``populate_by_name=True`` is set on every schema.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.acl import require_role
from app.core.audit import log_audit
from app.core.auth import Identity
from app.core.recipe_resolver import (
    RecipeResolutionError,
    resolve_all_effective_targets,
)
from app.db import get_session
from app.models.audit_event import AuditEventType
from app.models.batch import Batch
from app.models.genetics import Genetics
from app.models.grow_recipe import GrowRecipe
from app.models.grow_recipe_day_override import GrowRecipeDayOverride
from app.models.plant import Plant
from app.models.user import RoleName
from app.schemas.cultivation import (
    BatchCreate,
    BatchRead,
    GeneticsCreate,
    GeneticsRead,
    GrowRecipeBase,
    GrowRecipeCreate,
    GrowRecipeRead,
    PlantCreate,
    PlantRead,
)
from app.schemas.recipe_overrides import DayOverrideCreate, DayOverrideRead

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


# ---------------------------------------------------------------------------
# Grow recipes — per-day overrides (OCS extension)
# ---------------------------------------------------------------------------


class DayOverridesBulkReplace(BaseModel):
    """Body for the bulk-replace endpoint.

    Holds the full new set of overrides for one recipe. The endpoint
    DELETEs every existing override row for the recipe and INSERTs the
    new set atomically inside a single transaction, so callers don't
    have to worry about ordering or partial state.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    overrides: list[DayOverrideCreate] = Field(default_factory=list)


def _load_recipe_as_base(recipe: GrowRecipe) -> GrowRecipeBase:
    """Re-hydrate a SQLAlchemy :class:`GrowRecipe` as a validated schema.

    Validates against :class:`GrowRecipeRead` (which sets
    ``from_attributes=True`` and therefore knows how to pull values off
    an ORM instance) and upcasts to :class:`GrowRecipeBase` — the
    resolver and validator both work over the base shape, and the only
    reason to validate through :class:`GrowRecipeRead` here is to use
    its ``from_attributes=True`` so the SQLAlchemy instance feeds in
    directly without an intermediate ``dict()`` step.

    The schema's post-validate hook on :class:`GrowRecipeBase` then
    stamps ``start_day`` / ``end_day`` on every phase and re-sorts by
    ``order``, surfacing structural errors (e.g. fractional duration
    days) as a 422 at request time rather than deep in the resolver.
    """
    return GrowRecipeRead.model_validate(recipe)


@router.get(
    "/grow-recipes/{recipe_id}/effective-targets",
    response_model_by_alias=True,
)
async def get_effective_targets(
    recipe_id: str,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
    day: Annotated[
        int | None,
        Query(
            ge=1,
            description=(
                "Filter to a single day within the cycle. Returns every "
                "param declared by the phase containing that day (or by "
                "an override row on that day). Omit to get the dense "
                "(day x param) grid for the whole cycle."
            ),
        ),
    ] = None,
) -> dict[str, Any]:
    """Resolve the effective per-day targets for a recipe.

    Reads the recipe + all its overrides, runs the resolver, and returns
    the wire-format ``EffectiveTarget`` rows. With ``day=N`` set, only
    rows for that single day are returned (useful for the day editor in
    the planner UI); without ``day`` the full grid is returned (useful
    for the recipe overview).
    """
    settings = get_settings()
    recipe = await session.get(GrowRecipe, recipe_id)
    if recipe is None or recipe.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"grow recipe '{recipe_id}' not found",
        )

    validated = _load_recipe_as_base(recipe)

    override_rows = (
        await session.execute(
            select(GrowRecipeDayOverride).where(
                GrowRecipeDayOverride.recipe_id == recipe_id,
                GrowRecipeDayOverride.org_id == settings.ocs_org_id,
            )
        )
    ).scalars()
    overrides = list(override_rows)

    if day is not None:
        if day < 1 or day > validated.cycle_day_count:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"day {day} is outside this recipe's cycle "
                    f"[1, {validated.cycle_day_count}]"
                ),
            )
        # Single-day filter — emit only rows for the requested day.
        all_rows = resolve_all_effective_targets(validated, overrides)
        items = [r for r in all_rows if r.day == day]
    else:
        items = resolve_all_effective_targets(validated, overrides)

    log.info(
        "grow_recipe_effective_targets_resolved",
        actor=identity.user_id,
        grow_recipe_id=recipe_id,
        day=day,
        cell_count=len(items),
    )
    return {
        "effectiveTargets": [
            t.model_dump(mode="json", by_alias=True) for t in items
        ],
        "cycleDayCount": validated.cycle_day_count,
    }


@router.put(
    "/grow-recipes/{recipe_id}/day-overrides",
    response_model_by_alias=True,
)
async def replace_day_overrides(
    recipe_id: str,
    body: DayOverridesBulkReplace,
    identity: Annotated[Identity, Depends(require_role(RoleName.admin))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Bulk-replace every override row for a recipe atomically.

    The body holds the FULL new override set. Existing override rows for
    this recipe are deleted, the new set is inserted, and the
    transaction commits as one unit — there is no intermediate state
    where the recipe has zero overrides visible to other readers.

    Each new override's ``day`` is range-checked against the recipe's
    ``cycle_day_count`` (resolved via the validated schema) and the
    resolver itself is run as a smoke test against the new set; a
    :class:`RecipeResolutionError` is mapped to HTTP 422 so the planner
    UI can show the cell that failed.
    """
    settings = get_settings()
    recipe = await session.get(GrowRecipe, recipe_id)
    if recipe is None or recipe.org_id != settings.ocs_org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"grow recipe '{recipe_id}' not found",
        )

    validated = _load_recipe_as_base(recipe)

    # Range-check every incoming override BEFORE any DB mutation —
    # avoids leaving the recipe override-less if the body is malformed.
    for override in body.overrides:
        if override.day < 1 or override.day > validated.cycle_day_count:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"override on day {override.day} param "
                    f"{override.param_name!r} is outside this recipe's "
                    f"cycle [1, {validated.cycle_day_count}]"
                ),
            )

    # DELETE-then-INSERT in one transaction. Loading the existing rows
    # through the ORM (rather than a bulk DELETE statement) keeps the
    # identity map consistent and lets ON DELETE CASCADE / SQLAlchemy
    # event listeners run if any are wired up.
    existing = (
        await session.execute(
            select(GrowRecipeDayOverride).where(
                GrowRecipeDayOverride.recipe_id == recipe_id,
                GrowRecipeDayOverride.org_id == settings.ocs_org_id,
            )
        )
    ).scalars()
    deleted_count = 0
    for row in existing:
        await session.delete(row)
        deleted_count += 1
    # Flush the deletes BEFORE adding the new rows so the unique
    # constraint on (recipe_id, day, param_name) does not trip on a
    # day/param pair the caller is also re-supplying.
    await session.flush()

    new_rows: list[GrowRecipeDayOverride] = []
    for override in body.overrides:
        new_row = GrowRecipeDayOverride(
            org_id=settings.ocs_org_id,
            recipe_id=recipe_id,
            day=override.day,
            param_name=override.param_name,
            value=override.value,
            tolerance=override.tolerance,
            unit=override.unit,
        )
        session.add(new_row)
        new_rows.append(new_row)
    await session.flush()
    for new_row in new_rows:
        await session.refresh(new_row)

    # Smoke-test the resolver against the new state for any over-defined
    # cells (a structurally-broken recipe would have failed earlier; the
    # resolver is exercised here to surface override-vs-phase
    # interactions like an orphan param on a phase with no defaults).
    try:
        resolve_all_effective_targets(validated, new_rows)
    except RecipeResolutionError as exc:
        # Roll back by raising — the FastAPI ``get_session`` context will
        # handle the rollback on exception.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    await log_audit(
        session,
        event_type=AuditEventType.info_event,
        actor_id=identity.user_id,
        actor_role="admin",
        summary=(
            f"Grow recipe '{recipe.name}' day-overrides replaced "
            f"(deleted={deleted_count}, inserted={len(new_rows)})"
        ),
        params={
            "grow_recipe_id": recipe.id,
            "deleted": deleted_count,
            "inserted": len(new_rows),
        },
    )
    await session.commit()

    # Re-resolve a single ``(day=1, first declared param)`` cell would be
    # cheap but adds no value — the caller can hit GET
    # /effective-targets to confirm. Return the new override set echo'd
    # back so the UI can hydrate without a second round trip.
    log.info(
        "grow_recipe_day_overrides_replaced",
        actor=identity.user_id,
        grow_recipe_id=recipe_id,
        deleted=deleted_count,
        inserted=len(new_rows),
    )
    return {
        "deleted": deleted_count,
        "inserted": len(new_rows),
        "overrides": [
            DayOverrideRead.model_validate(r).model_dump(
                mode="json", by_alias=True
            )
            for r in new_rows
        ],
    }
