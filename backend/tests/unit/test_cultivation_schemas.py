"""Unit tests for the cultivation Pydantic schemas — Convex wire shape.

The Pydantic models in :mod:`app.schemas.cultivation` are the contract
OCS exposes to AiGrowApp's integration layer for the genetics /
batches / plants / grow-recipes tier: snake_case in Python, but
``model_dump(by_alias=True)`` produces the camelCase + epoch-ms JSON
that's byte-compatible with a Convex document on the wire. These
tests pin that down so a future refactor can't silently break the
alignment.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.schemas.cultivation import (
    BatchCreate,
    BatchRead,
    EnvironmentalTargets,
    GeneticsCreate,
    GeneticsRead,
    GrowRecipeCreate,
    GrowRecipePhase,
    GrowRecipeRead,
    GrowthCharacteristics,
    LightCycle,
    MinMaxRange,
    NutrientItem,
    PhaseNutrients,
    PhaseTask,
    PlantCreate,
    PlantRead,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit


_T = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.UTC)
_T_MS = int(_T.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Shared nested validators
# ---------------------------------------------------------------------------


class TestMinMaxRange:
    """The ``{min, max}`` Convex shape — reused by every range field."""

    def test_round_trip(self) -> None:
        r = MinMaxRange.model_validate({"min": 18.0, "max": 28.0})
        assert r.min == 18.0
        assert r.max == 28.0
        assert r.model_dump() == {"min": 18.0, "max": 28.0}

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            MinMaxRange.model_validate(
                {"min": 0.0, "max": 1.0, "extra": "no"}
            )


# ---------------------------------------------------------------------------
# Genetics
# ---------------------------------------------------------------------------


class TestGrowthCharacteristics:
    """The nested ``growthCharacteristics`` object — all fields optional."""

    def test_camel_round_trip(self) -> None:
        g = GrowthCharacteristics.model_validate(
            {
                "floweringTime": {"min": 56, "max": 70},
                "vegetativeTime": {"min": 21, "max": 35},
                "yield": {"min": 400, "max": 600},
                "thcContent": {"min": 18, "max": 24},
                "cbdContent": {"min": 0.1, "max": 1.0},
            }
        )
        assert g.flowering_time is not None
        assert g.flowering_time.min == 56
        # ``yield`` is a Python keyword — mapped to ``yield_`` internally.
        assert g.yield_ is not None
        assert g.yield_.max == 600
        wire = g.model_dump(by_alias=True, exclude_none=True)
        assert wire["floweringTime"] == {"min": 56, "max": 70}
        assert wire["yield"] == {"min": 400, "max": 600}
        assert wire["thcContent"]["max"] == 24

    def test_all_fields_optional(self) -> None:
        g = GrowthCharacteristics.model_validate({})
        assert g.flowering_time is None
        assert g.yield_ is None


class TestGeneticsShape:
    """The ``genetics`` Convex shape — orgId, growthCharacteristics, terpenes."""

    def test_read_serialises_to_convex_wire_format(self) -> None:
        g = GeneticsRead.model_validate(
            {
                "id": "gen-1",
                "orgId": "open-crop-steering",
                "name": "Northern Lights",
                "prefix": "NL",
                "type": "indica",
                "status": "active",
                "lineage": "Afghan x Thai",
                "terpenes": ["myrcene", "pinene"],
                "growthCharacteristics": {
                    "floweringTime": {"min": 49, "max": 63},
                },
                "createdAt": _T,
                "updatedAt": _T,
            }
        )
        wire = g.model_dump(by_alias=True, exclude_none=True)
        assert wire["id"] == "gen-1"
        assert wire["orgId"] == "open-crop-steering"
        assert wire["prefix"] == "NL"
        assert wire["terpenes"] == ["myrcene", "pinene"]
        assert wire["growthCharacteristics"]["floweringTime"] == {
            "min": 49,
            "max": 63,
        }
        assert wire["createdAt"] == _T_MS
        assert wire["updatedAt"] == _T_MS

    def test_create_payload_omits_id_and_org_id(self) -> None:
        c = GeneticsCreate(
            name="NL",
            prefix="NL",
            type="indica",
            status="active",
        )
        with pytest.raises(ValidationError, match="extra"):
            GeneticsCreate.model_validate(
                {
                    "name": "NL",
                    "prefix": "NL",
                    "type": "indica",
                    "status": "active",
                    "id": "gen-1",
                }
            )
        assert c.name == "NL"

    def test_read_accepts_snake_case_inputs(self) -> None:
        g = GeneticsRead.model_validate(
            {
                "id": "gen-1",
                "org_id": "open-crop-steering",
                "name": "NL",
                "prefix": "NL",
                "type": "indica",
                "status": "active",
                "created_at": _T,
                "updated_at": _T,
            }
        )
        assert g.org_id == "open-crop-steering"


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------


class TestBatchShape:
    """The ``batches`` Convex shape — FKs to genetics/growRecipes."""

    def test_create_camel_round_trip(self) -> None:
        b = BatchCreate.model_validate(
            {
                "batchCode": "B-001",
                "name": "Run 1",
                "genetics": "gen-1",
                "batchType": "standard_cultivation",
                "status": "vegetative",
                "currentPhase": 1,
                "phaseStartDate": _T_MS,
                "startDate": _T_MS,
                "targetPlantCount": 50,
                "initialPlantCount": 48,
                "currentPlantCount": 47,
                "growRecipe": "rec-1",
            }
        )
        assert b.batch_code == "B-001"
        assert b.genetics == "gen-1"
        assert b.grow_recipe == "rec-1"
        wire = b.model_dump(by_alias=True, exclude_none=True)
        assert wire["batchCode"] == "B-001"
        assert wire["growRecipe"] == "rec-1"
        assert wire["phaseStartDate"] == _T_MS

    def test_read_full_round_trip(self) -> None:
        b = BatchRead.model_validate(
            {
                "id": "bat-1",
                "orgId": "open-crop-steering",
                "batchCode": "B-001",
                "genetics": "gen-1",
                "batchType": "standard_cultivation",
                "status": "flowering",
                "currentPhase": 2,
                "phaseStartDate": _T_MS,
                "targetPlantCount": 50,
                "initialPlantCount": 48,
                "currentPlantCount": 47,
                "createdAt": _T,
                "updatedAt": _T,
            }
        )
        wire = b.model_dump(by_alias=True, exclude_none=True)
        assert wire["id"] == "bat-1"
        assert wire["orgId"] == "open-crop-steering"
        assert wire["createdAt"] == _T_MS
        assert wire["batchCode"] == "B-001"

    def test_grow_recipe_optional(self) -> None:
        b = BatchCreate.model_validate(
            {
                "batchCode": "B-002",
                "genetics": "gen-1",
                "batchType": "mother_maintenance",
                "status": "vegetative",
                "currentPhase": 1,
                "phaseStartDate": _T_MS,
                "targetPlantCount": 10,
                "initialPlantCount": 10,
                "currentPlantCount": 10,
            }
        )
        assert b.grow_recipe is None


# ---------------------------------------------------------------------------
# Plants
# ---------------------------------------------------------------------------


class TestPlantShape:
    """The ``plants`` Convex shape — FKs incl. self-FK + bare inventory id."""

    def test_create_camel_round_trip(self) -> None:
        p = PlantCreate.model_validate(
            {
                "plantId": "NL-001",
                "currentBatch": "bat-1",
                "genetics": "gen-1",
                "currentLocation": "loc-1",
                "status": "seedling",
                "sourceType": "clone",
                "plantedDate": _T_MS,
                "isMotherPlant": False,
                "sourcePlant": "pla-mother-1",
                "sourceSeedInventory": "inv-1",
            }
        )
        assert p.plant_id == "NL-001"
        assert p.current_batch == "bat-1"
        assert p.current_location == "loc-1"
        assert p.source_plant == "pla-mother-1"
        # inventory ref is a bare string — Convex's ``v.id("inventory")``
        # mirrored on a table OCS doesn't own.
        assert p.source_seed_inventory == "inv-1"
        wire = p.model_dump(by_alias=True, exclude_none=True)
        assert wire["plantId"] == "NL-001"
        assert wire["currentBatch"] == "bat-1"
        assert wire["currentLocation"] == "loc-1"
        assert wire["sourcePlant"] == "pla-mother-1"
        assert wire["sourceSeedInventory"] == "inv-1"
        assert wire["isMotherPlant"] is False

    def test_read_serialises_timestamps_as_epoch_ms(self) -> None:
        p = PlantRead.model_validate(
            {
                "id": "pla-1",
                "orgId": "open-crop-steering",
                "plantId": "NL-001",
                "genetics": "gen-1",
                "status": "vegetative",
                "isMotherPlant": False,
                "createdAt": _T,
                "updatedAt": _T,
            }
        )
        wire = p.model_dump(by_alias=True, exclude_none=True)
        assert wire["createdAt"] == _T_MS
        assert wire["updatedAt"] == _T_MS
        assert wire["plantId"] == "NL-001"

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            PlantCreate.model_validate(
                {
                    "plantId": "NL-001",
                    "genetics": "gen-1",
                    "status": "seedling",
                    "isMotherPlant": False,
                    "rogueField": "no",
                }
            )


# ---------------------------------------------------------------------------
# GrowRecipes — nested phase validators
# ---------------------------------------------------------------------------


class TestGrowRecipeNestedPhase:
    """The ``growRecipes.phases[]`` deeply nested Convex shape."""

    def test_light_cycle_camel_round_trip(self) -> None:
        lc = LightCycle.model_validate({"hoursOn": 18, "hoursOff": 6})
        assert lc.hours_on == 18
        assert lc.hours_off == 6
        wire = lc.model_dump(by_alias=True)
        assert wire == {"hoursOn": 18, "hoursOff": 6}

    def test_environmental_targets_optional_substructure(self) -> None:
        env = EnvironmentalTargets.model_validate(
            {
                "temperature": {"min": 22, "max": 26},
                "vpd": {"min": 1.0, "max": 1.4},
            }
        )
        assert env.temperature is not None
        assert env.temperature.max == 26
        assert env.humidity is None
        assert env.co2 is None

    def test_phase_nutrients_with_items(self) -> None:
        n = PhaseNutrients.model_validate(
            {
                "ec": {"min": 1.2, "max": 1.6},
                "ph": {"min": 5.8, "max": 6.2},
                "feedingSchedule": "every other day",
                "nutrients": [
                    {"name": "Cal-Mag", "dosage": "2", "unit": "ml/L"},
                    {"name": "Bloom A", "dosage": "5", "unit": "ml/L"},
                ],
            }
        )
        assert n.feeding_schedule == "every other day"
        assert n.nutrients is not None
        assert len(n.nutrients) == 2
        assert n.nutrients[0].name == "Cal-Mag"
        wire = n.model_dump(by_alias=True, exclude_none=True)
        assert wire["feedingSchedule"] == "every other day"
        assert wire["nutrients"][1]["unit"] == "ml/L"

    def test_phase_task_round_trip(self) -> None:
        # taskTemplate is an opaque id — references AiGrowApp's
        # task_templates table which OCS does not own; no FK enforced.
        t = PhaseTask.model_validate(
            {
                "taskTemplate": "tmpl-defoliate",
                "daysFromPhaseStartToCreate": 7,
                "dueDaysAfterCreation": 2,
            }
        )
        assert t.task_template == "tmpl-defoliate"
        assert t.days_from_phase_start_to_create == 7
        assert t.due_days_after_creation == 2
        wire = t.model_dump(by_alias=True)
        assert wire == {
            "taskTemplate": "tmpl-defoliate",
            "daysFromPhaseStartToCreate": 7,
            "dueDaysAfterCreation": 2,
        }

    def test_full_phase_round_trip(self) -> None:
        p = GrowRecipePhase.model_validate(
            {
                "phaseName": "Flower Wk 1",
                "durationDays": 7,
                "order": 3,
                "lightCycle": {"hoursOn": 12, "hoursOff": 12},
                "environmentalTargets": {
                    "temperature": {"min": 22, "max": 26},
                    "humidity": {"min": 55, "max": 60},
                },
                "nutrients": {
                    "ec": {"min": 1.4, "max": 1.8},
                },
                "phaseTasks": [
                    {
                        "taskTemplate": "tmpl-flush",
                        "daysFromPhaseStartToCreate": 0,
                    }
                ],
            }
        )
        assert p.phase_name == "Flower Wk 1"
        assert p.light_cycle is not None
        assert p.light_cycle.hours_on == 12
        wire = p.model_dump(by_alias=True, exclude_none=True)
        assert wire["phaseName"] == "Flower Wk 1"
        assert wire["lightCycle"] == {"hoursOn": 12, "hoursOff": 12}
        assert wire["environmentalTargets"]["temperature"]["max"] == 26
        assert wire["phaseTasks"][0]["taskTemplate"] == "tmpl-flush"

    def test_nutrient_item_strict_required_fields(self) -> None:
        with pytest.raises(ValidationError, match="dosage"):
            NutrientItem.model_validate({"name": "Cal-Mag", "unit": "ml/L"})


class TestGrowRecipeShape:
    """The ``growRecipes`` Convex shape — phases JSONB + optional genetics."""

    def test_create_camel_round_trip(self) -> None:
        r = GrowRecipeCreate.model_validate(
            {
                "name": "Indoor Photo Standard",
                "genetics": "gen-1",
                "recipeType": ["indoor", "soil"],
                "isActive": True,
                "estimatedTotalDurationDays": 100,
                "phases": [
                    {
                        "phaseName": "Veg",
                        "durationDays": 28,
                        "order": 1,
                        "lightCycle": {"hoursOn": 18, "hoursOff": 6},
                    },
                    {
                        "phaseName": "Flower",
                        "durationDays": 63,
                        "order": 2,
                        "lightCycle": {"hoursOn": 12, "hoursOff": 12},
                    },
                ],
            }
        )
        assert r.recipe_type == ["indoor", "soil"]
        assert r.is_active is True
        assert len(r.phases) == 2
        assert r.phases[1].phase_name == "Flower"
        wire = r.model_dump(by_alias=True, exclude_none=True)
        assert wire["recipeType"] == ["indoor", "soil"]
        assert wire["isActive"] is True
        assert wire["estimatedTotalDurationDays"] == 100
        assert wire["phases"][0]["lightCycle"] == {
            "hoursOn": 18,
            "hoursOff": 6,
        }

    def test_read_serialises_timestamps_as_epoch_ms(self) -> None:
        r = GrowRecipeRead.model_validate(
            {
                "id": "rec-1",
                "orgId": "open-crop-steering",
                "name": "Indoor Photo Standard",
                "recipeType": ["indoor"],
                "isActive": True,
                "phases": [
                    {
                        "phaseName": "Veg",
                        "durationDays": 28,
                        "order": 1,
                    }
                ],
                "createdAt": _T,
                "updatedAt": _T,
            }
        )
        wire = r.model_dump(by_alias=True, exclude_none=True)
        assert wire["id"] == "rec-1"
        assert wire["orgId"] == "open-crop-steering"
        assert wire["createdAt"] == _T_MS
        assert wire["phases"][0]["phaseName"] == "Veg"

    def test_genetics_optional(self) -> None:
        r = GrowRecipeCreate.model_validate(
            {
                "name": "Generic Indoor",
                "recipeType": ["indoor"],
                "isActive": True,
                "phases": [
                    {"phaseName": "Veg", "durationDays": 28, "order": 1}
                ],
            }
        )
        assert r.genetics is None
