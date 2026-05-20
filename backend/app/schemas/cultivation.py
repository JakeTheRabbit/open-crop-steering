"""Pydantic schemas for the cultivation tier — Convex-mirror.

These shapes mirror ``stewnight/AiGrowApp:convex/schema/grow.ts``
exactly (excluding ``testSamples`` which is a separate compliance
pass). JSON output (via ``model_dump(by_alias=True)``) uses camelCase
to match Convex documents on the wire; Python attribute access uses
snake_case to match the SQL ORM and the rest of the OCS codebase.

Both shapes are accepted on input (``populate_by_name=True``), so an
incoming JSON body can use either camelCase (AiGrowApp wire format) or
snake_case (OCS internal). Timestamps serialise as epoch
milliseconds — the same wire format Convex uses for ``v.number()``
timestamp fields.

A future OCS↔AiGrowApp sync layer therefore translates ``orgId`` ↔
``org_id`` and the datetime/epoch-ms timestamp purely via these
schemas — no per-field renaming on the sync hot path.

Nested object validators (``MinMaxRange``, ``GrowthCharacteristics``,
``GrowRecipePhase`` and friends) are kept as their own Pydantic models
so callers get typed access to JSONB column payloads.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, field_serializer


def _to_epoch_ms(value: dt.datetime) -> int:
    """Convert a timezone-aware datetime to epoch milliseconds.

    Convex's ``v.number()`` timestamp fields are epoch milliseconds —
    this is the canonical conversion used on every Convex-mirrored read
    schema in OCS, so the wire format matches Convex exactly.
    """
    return int(value.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Shared nested validators
# ---------------------------------------------------------------------------


class MinMaxRange(BaseModel):
    """A ``{min, max}`` numeric range — Convex's most common nested shape."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    min: float
    max: float


# ---------------------------------------------------------------------------
# Genetics — growthCharacteristics nested validator
# ---------------------------------------------------------------------------


class GrowthCharacteristics(BaseModel):
    """Per-strain ranges for flowering / vegetative / yield / cannabinoids."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    flowering_time: MinMaxRange | None = Field(
        default=None, alias="floweringTime"
    )
    vegetative_time: MinMaxRange | None = Field(
        default=None, alias="vegetativeTime"
    )
    yield_: MinMaxRange | None = Field(default=None, alias="yield")
    thc_content: MinMaxRange | None = Field(default=None, alias="thcContent")
    cbd_content: MinMaxRange | None = Field(default=None, alias="cbdContent")


class GeneticsBase(BaseModel):
    """Fields shared by the create and read shapes of a genetics row."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    prefix: str = Field(min_length=1, max_length=64)
    type: str = Field(min_length=1, max_length=64)
    phenotype_details: str | None = Field(
        default=None, max_length=1024, alias="phenotypeDetails"
    )
    lineage: str | None = Field(default=None, max_length=512)
    status: str = Field(min_length=1, max_length=64)
    terpenes: list[str] | None = None
    growth_characteristics: GrowthCharacteristics | None = Field(
        default=None, alias="growthCharacteristics"
    )
    created_by: str | None = Field(
        default=None, max_length=64, alias="createdBy"
    )
    updated_by: str | None = Field(
        default=None, max_length=64, alias="updatedBy"
    )


class GeneticsCreate(GeneticsBase):
    """Payload for creating a :class:`~app.models.genetics.Genetics`.

    ``org_id`` is server-stamped from settings — clients never supply it.
    """


class GeneticsRead(GeneticsBase):
    """A persisted genetics row, wire-compatible with Convex's doc."""

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )

    id: str
    org_id: str = Field(alias="orgId")
    created_at: dt.datetime = Field(alias="createdAt")
    updated_at: dt.datetime = Field(alias="updatedAt")

    @field_serializer("created_at", "updated_at")
    def _ser_timestamps(self, value: dt.datetime) -> int:
        return _to_epoch_ms(value)


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------


class BatchBase(BaseModel):
    """Fields shared by the create and read shapes of a batch."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    batch_code: str = Field(
        min_length=1, max_length=64, alias="batchCode"
    )
    name: str | None = Field(default=None, max_length=256)
    batch_type: str = Field(
        min_length=1, max_length=64, alias="batchType"
    )
    status: str = Field(min_length=1, max_length=64)
    current_phase: float = Field(alias="currentPhase")
    phase_start_date: int = Field(alias="phaseStartDate")
    start_date: int | None = Field(default=None, alias="startDate")
    end_date: int | None = Field(default=None, alias="endDate")
    target_plant_count: float = Field(alias="targetPlantCount")
    initial_plant_count: float = Field(alias="initialPlantCount")
    current_plant_count: float = Field(alias="currentPlantCount")
    expected_yield_grams: float | None = Field(
        default=None, alias="expectedYieldGrams"
    )
    actual_yield_wet_grams: float | None = Field(
        default=None, alias="actualYieldWetGrams"
    )
    actual_yield_dry_trimmed_grams: float | None = Field(
        default=None, alias="actualYieldDryTrimmedGrams"
    )
    created_by: str | None = Field(
        default=None, max_length=64, alias="createdBy"
    )
    updated_by: str | None = Field(
        default=None, max_length=64, alias="updatedBy"
    )


class BatchCreate(BatchBase):
    """Payload for creating a :class:`~app.models.batch.Batch`."""

    genetics: str
    grow_recipe: str | None = Field(default=None, alias="growRecipe")


class BatchRead(BatchBase):
    """A persisted batch, wire-compatible with Convex's ``batches`` doc."""

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )

    id: str
    org_id: str = Field(alias="orgId")
    genetics: str
    grow_recipe: str | None = Field(default=None, alias="growRecipe")
    created_at: dt.datetime = Field(alias="createdAt")
    updated_at: dt.datetime = Field(alias="updatedAt")

    @field_serializer("created_at", "updated_at")
    def _ser_timestamps(self, value: dt.datetime) -> int:
        return _to_epoch_ms(value)


# ---------------------------------------------------------------------------
# Plants
# ---------------------------------------------------------------------------


class PlantBase(BaseModel):
    """Fields shared by the create and read shapes of a plant."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    plant_id: str = Field(min_length=1, max_length=64, alias="plantId")
    status: str = Field(min_length=1, max_length=64)
    source_type: str | None = Field(
        default=None, max_length=64, alias="sourceType"
    )
    planted_date: int | None = Field(default=None, alias="plantedDate")
    is_mother_plant: bool = Field(alias="isMotherPlant")
    harvest_date: int | None = Field(default=None, alias="harvestDate")
    harvest_weight: float | None = Field(
        default=None, alias="harvestWeight"
    )
    dry_weight: float | None = Field(default=None, alias="dryWeight")
    trimmed_weight: float | None = Field(
        default=None, alias="trimmedWeight"
    )
    health_status: str | None = Field(
        default=None, max_length=64, alias="healthStatus"
    )
    notes: str | None = Field(default=None, max_length=2048)
    created_by: str | None = Field(
        default=None, max_length=64, alias="createdBy"
    )
    last_modified_by: str | None = Field(
        default=None, max_length=64, alias="lastModifiedBy"
    )
    status_changed_at: int | None = Field(
        default=None, alias="statusChangedAt"
    )


class PlantCreate(PlantBase):
    """Payload for creating a :class:`~app.models.plant.Plant`."""

    current_batch: str | None = Field(default=None, alias="currentBatch")
    genetics: str
    current_location: str | None = Field(
        default=None, alias="currentLocation"
    )
    source_plant: str | None = Field(default=None, alias="sourcePlant")
    source_seed_inventory: str | None = Field(
        default=None, alias="sourceSeedInventory"
    )


class PlantRead(PlantBase):
    """A persisted plant, wire-compatible with Convex's ``plants`` doc."""

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )

    id: str
    org_id: str = Field(alias="orgId")
    current_batch: str | None = Field(default=None, alias="currentBatch")
    genetics: str
    current_location: str | None = Field(
        default=None, alias="currentLocation"
    )
    source_plant: str | None = Field(default=None, alias="sourcePlant")
    source_seed_inventory: str | None = Field(
        default=None, alias="sourceSeedInventory"
    )
    created_at: dt.datetime = Field(alias="createdAt")
    updated_at: dt.datetime = Field(alias="updatedAt")

    @field_serializer("created_at", "updated_at")
    def _ser_timestamps(self, value: dt.datetime) -> int:
        return _to_epoch_ms(value)


# ---------------------------------------------------------------------------
# GrowRecipes — nested phase validators
# ---------------------------------------------------------------------------


class LightCycle(BaseModel):
    """Per-phase photoperiod — ``{hoursOn, hoursOff}``."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    hours_on: float = Field(alias="hoursOn")
    hours_off: float = Field(alias="hoursOff")


class EnvironmentalTargets(BaseModel):
    """Per-phase climate targets — each entry is a ``MinMaxRange``."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    temperature: MinMaxRange | None = None
    humidity: MinMaxRange | None = None
    co2: MinMaxRange | None = None
    vpd: MinMaxRange | None = None


class NutrientItem(BaseModel):
    """One nutrient in the phase's nutrient list."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    dosage: str = Field(min_length=1, max_length=64)
    unit: str = Field(min_length=1, max_length=32)


class PhaseNutrients(BaseModel):
    """Per-phase nutrient programme."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    ec: MinMaxRange | None = None
    ph: MinMaxRange | None = None
    feeding_schedule: str | None = Field(
        default=None, alias="feedingSchedule", max_length=512
    )
    nutrients: list[NutrientItem] | None = None


class PhaseTask(BaseModel):
    """One task template scheduled relative to phase start.

    ``task_template`` references AiGrowApp's ``task_templates`` table
    which OCS does not have; kept as an opaque id (no FK).
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    task_template: str = Field(alias="taskTemplate")
    days_from_phase_start_to_create: float = Field(
        alias="daysFromPhaseStartToCreate"
    )
    due_days_after_creation: float | None = Field(
        default=None, alias="dueDaysAfterCreation"
    )


class GrowRecipePhase(BaseModel):
    """One phase in a grow recipe."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    phase_name: str = Field(
        min_length=1, max_length=128, alias="phaseName"
    )
    duration_days: float = Field(alias="durationDays")
    order: float
    light_cycle: LightCycle | None = Field(default=None, alias="lightCycle")
    environmental_targets: EnvironmentalTargets | None = Field(
        default=None, alias="environmentalTargets"
    )
    nutrients: PhaseNutrients | None = None
    phase_tasks: list[PhaseTask] | None = Field(
        default=None, alias="phaseTasks"
    )


class GrowRecipeBase(BaseModel):
    """Fields shared by the create and read shapes of a grow recipe."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    recipe_type: list[str] = Field(alias="recipeType")
    description: str | None = Field(default=None, max_length=2048)
    version: float | None = None
    is_active: bool = Field(alias="isActive")
    estimated_total_duration_days: float | None = Field(
        default=None, alias="estimatedTotalDurationDays"
    )
    phases: list[GrowRecipePhase]
    created_by: str | None = Field(
        default=None, max_length=64, alias="createdBy"
    )
    last_modified_by: str | None = Field(
        default=None, max_length=64, alias="lastModifiedBy"
    )


class GrowRecipeCreate(GrowRecipeBase):
    """Payload for creating a :class:`~app.models.grow_recipe.GrowRecipe`."""

    genetics: str | None = None


class GrowRecipeRead(GrowRecipeBase):
    """A persisted grow recipe, wire-compatible with Convex's doc."""

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )

    id: str
    org_id: str = Field(alias="orgId")
    genetics: str | None = None
    created_at: dt.datetime = Field(alias="createdAt")
    updated_at: dt.datetime = Field(alias="updatedAt")

    @field_serializer("created_at", "updated_at")
    def _ser_timestamps(self, value: dt.datetime) -> int:
        return _to_epoch_ms(value)
