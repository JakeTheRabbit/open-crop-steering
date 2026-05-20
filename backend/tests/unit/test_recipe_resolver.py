"""Unit tests for :mod:`app.core.recipe_resolver` + migration converter.

Covers:

* The :class:`GrowRecipeBase` post-validate hook that stamps
  ``start_day`` / ``end_day`` on every phase and computes
  ``cycle_day_count`` from the prefix-sum of ``duration_days`` in
  ``order``.
* :func:`resolve_effective_target` — phase-default-only, override-on,
  override-with-partial-tolerance, day-out-of-cycle, undeclared param.
* :func:`resolve_all_effective_targets` — dense-grid shape, override
  shadowing, override-only param appended.
* :func:`validate_recipe` — every finding code it can emit.
* :func:`tools.migrate_recipe_revision_to_grow_recipes.convert_revision`
  — single-phase shape, per-day override per legacy param row,
  metadata round-trip.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from app.core.recipe_resolver import (
    RecipeResolutionError,
    resolve_all_effective_targets,
    resolve_effective_target,
    validate_recipe,
)
from app.models.grow_recipe_day_override import GrowRecipeDayOverride
from app.models.recipe_revision import (
    RecipeRevision,
    RecipeRevisionParam,
    RecipeStatus,
)
from app.schemas.cultivation import GrowRecipeBase
from pydantic import ValidationError
from tools.migrate_recipe_revision_to_grow_recipes import (
    ConversionResult,
    convert_revision,
)

pytestmark = pytest.mark.unit


_T = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.UTC)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _recipe_two_phases() -> GrowRecipeBase:
    """A two-phase recipe: veg (7 days) + flower (7 days). Cycle = 14."""
    return GrowRecipeBase.model_validate(
        {
            "name": "Test 14-day",
            "recipeType": ["indoor"],
            "isActive": True,
            "phases": [
                {
                    "phaseName": "Veg",
                    "durationDays": 7,
                    "order": 1,
                    "targets": {
                        "temp_day": {"value": 25.0, "tolerance": 0.5, "unit": "C"},
                        "rh_day": {"value": 70.0, "tolerance": 3.0, "unit": "%"},
                    },
                },
                {
                    "phaseName": "Flower",
                    "durationDays": 7,
                    "order": 2,
                    "targets": {
                        "temp_day": {"value": 27.0, "tolerance": 0.5, "unit": "C"},
                        "rh_day": {"value": 55.0, "tolerance": 3.0, "unit": "%"},
                    },
                },
            ],
        }
    )


def _override(
    *,
    day: int,
    param: str,
    value: float,
    tolerance: float | None = None,
    unit: str | None = None,
    recipe_id: str = "rec-1",
) -> GrowRecipeDayOverride:
    """Build a detached :class:`GrowRecipeDayOverride` for tests."""
    return GrowRecipeDayOverride(
        id="ov-" + str(day) + "-" + param,
        org_id="open-crop-steering",
        recipe_id=recipe_id,
        day=day,
        param_name=param,
        value=value,
        tolerance=tolerance,
        unit=unit,
    )


# ---------------------------------------------------------------------------
# Schema post-validate — start_day / end_day / cycle_day_count
# ---------------------------------------------------------------------------


class TestGrowRecipePhaseBoundaries:
    """``GrowRecipeBase`` stamps boundaries + cycle_day_count after validate."""

    def test_two_contiguous_phases(self) -> None:
        r = _recipe_two_phases()
        assert r.cycle_day_count == 14
        assert r.phases[0].start_day == 1
        assert r.phases[0].end_day == 7
        assert r.phases[1].start_day == 8
        assert r.phases[1].end_day == 14

    def test_phases_sorted_by_order(self) -> None:
        """``order`` drives the layout, not list position."""
        r = GrowRecipeBase.model_validate(
            {
                "name": "Reordered",
                "recipeType": ["indoor"],
                "isActive": True,
                "phases": [
                    {
                        "phaseName": "second",
                        "durationDays": 5,
                        "order": 2,
                    },
                    {
                        "phaseName": "first",
                        "durationDays": 3,
                        "order": 1,
                    },
                ],
            }
        )
        assert r.phases[0].phase_name == "first"
        assert r.phases[0].start_day == 1
        assert r.phases[0].end_day == 3
        assert r.phases[1].phase_name == "second"
        assert r.phases[1].start_day == 4
        assert r.phases[1].end_day == 8
        assert r.cycle_day_count == 8

    def test_duplicate_order_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate phase order"):
            GrowRecipeBase.model_validate(
                {
                    "name": "Bad",
                    "recipeType": ["indoor"],
                    "isActive": True,
                    "phases": [
                        {"phaseName": "a", "durationDays": 1, "order": 1},
                        {"phaseName": "b", "durationDays": 1, "order": 1},
                    ],
                }
            )

    def test_fractional_duration_rejected(self) -> None:
        with pytest.raises(ValidationError, match="fractional"):
            GrowRecipeBase.model_validate(
                {
                    "name": "Bad",
                    "recipeType": ["indoor"],
                    "isActive": True,
                    "phases": [
                        {
                            "phaseName": "a",
                            "durationDays": 1.5,
                            "order": 1,
                        }
                    ],
                }
            )

    def test_zero_phases_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least 1"):
            GrowRecipeBase.model_validate(
                {
                    "name": "Empty",
                    "recipeType": ["indoor"],
                    "isActive": True,
                    "phases": [],
                }
            )

    def test_cycle_day_count_property_available(self) -> None:
        r = _recipe_two_phases()
        # cycle_day_count is a plain @property (NOT a Pydantic
        # computed_field) so it does NOT appear in model_dump but is
        # available for Python callers (resolver, validator, API).
        assert r.cycle_day_count == 14
        wire = r.model_dump(by_alias=True, exclude_none=True)
        assert "cycleDayCount" not in wire
        # Phase boundaries DO land on the wire.
        assert wire["phases"][0]["startDay"] == 1
        assert wire["phases"][0]["endDay"] == 7
        assert wire["phases"][1]["startDay"] == 8
        assert wire["phases"][1]["endDay"] == 14


# ---------------------------------------------------------------------------
# resolve_effective_target
# ---------------------------------------------------------------------------


class TestResolveEffectiveTarget:
    """One-cell resolve covers phase defaults + day overrides."""

    def test_phase_default_no_overrides(self) -> None:
        r = _recipe_two_phases()
        result = resolve_effective_target(r, day=3, param="temp_day", overrides=[])
        assert result.day == 3
        assert result.param_name == "temp_day"
        assert result.value == 25.0
        assert result.tolerance == 0.5
        assert result.unit == "C"
        assert result.phase_name == "Veg"
        assert result.phase_order == 1
        assert result.source == "phase_default"

    def test_override_shadows_default(self) -> None:
        r = _recipe_two_phases()
        ov = _override(
            day=3, param="temp_day", value=24.0, tolerance=0.3, unit="C"
        )
        result = resolve_effective_target(r, day=3, param="temp_day", overrides=[ov])
        assert result.value == 24.0
        assert result.tolerance == 0.3
        assert result.unit == "C"
        assert result.source == "day_override"

    def test_override_neighbour_days_unaffected(self) -> None:
        """An override on day 3 must NOT bleed onto day 2 or day 4."""
        r = _recipe_two_phases()
        ov = _override(day=3, param="temp_day", value=99.0)
        # Day 2: pure phase default (Veg).
        before = resolve_effective_target(
            r, day=2, param="temp_day", overrides=[ov]
        )
        assert before.value == 25.0
        assert before.source == "phase_default"
        # Day 4: pure phase default (still Veg).
        after = resolve_effective_target(
            r, day=4, param="temp_day", overrides=[ov]
        )
        assert after.value == 25.0
        assert after.source == "phase_default"

    def test_override_partial_tolerance_fallback(self) -> None:
        """An override with no tolerance falls back to the phase default's."""
        r = _recipe_two_phases()
        ov = _override(day=3, param="temp_day", value=24.0)
        result = resolve_effective_target(r, day=3, param="temp_day", overrides=[ov])
        assert result.value == 24.0
        # Tolerance falls back to the Veg default of 0.5.
        assert result.tolerance == 0.5
        # Unit also falls back.
        assert result.unit == "C"
        assert result.source == "day_override"

    def test_override_crosses_phase_boundary(self) -> None:
        """An override on day 10 (in Flower) uses Flower's phase metadata."""
        r = _recipe_two_phases()
        ov = _override(day=10, param="temp_day", value=26.5)
        result = resolve_effective_target(r, day=10, param="temp_day", overrides=[ov])
        assert result.value == 26.5
        # The phase metadata that gets attached must be Flower's, not Veg's.
        assert result.phase_name == "Flower"
        assert result.phase_order == 2

    def test_day_before_cycle_raises(self) -> None:
        r = _recipe_two_phases()
        with pytest.raises(RecipeResolutionError, match="outside"):
            resolve_effective_target(r, day=0, param="temp_day", overrides=[])

    def test_day_after_cycle_raises(self) -> None:
        r = _recipe_two_phases()
        with pytest.raises(RecipeResolutionError, match="outside"):
            resolve_effective_target(r, day=15, param="temp_day", overrides=[])

    def test_undeclared_param_and_no_override_raises(self) -> None:
        r = _recipe_two_phases()
        with pytest.raises(
            RecipeResolutionError, match="no phase default and no"
        ):
            resolve_effective_target(r, day=3, param="ppfd", overrides=[])

    def test_override_only_param_works_without_phase_default(self) -> None:
        """An override declaring a brand-new param resolves as override-only."""
        r = _recipe_two_phases()
        ov = _override(day=3, param="ppfd", value=450.0, unit="umol/m2/s")
        result = resolve_effective_target(r, day=3, param="ppfd", overrides=[ov])
        assert result.value == 450.0
        assert result.unit == "umol/m2/s"
        # No phase default — tolerance falls back to None.
        assert result.tolerance is None
        assert result.source == "day_override"


# ---------------------------------------------------------------------------
# resolve_all_effective_targets
# ---------------------------------------------------------------------------


class TestResolveAllEffectiveTargets:
    """The dense-grid form returns one row per (day, declared param)."""

    def test_no_overrides_returns_phase_default_grid(self) -> None:
        r = _recipe_two_phases()
        # 2 phases x 7 days x 2 params each = 28 cells.
        grid = resolve_all_effective_targets(r, overrides=[])
        assert len(grid) == 28
        assert all(cell.source == "phase_default" for cell in grid)
        # Day 1 in Veg.
        day1 = [c for c in grid if c.day == 1]
        assert {c.param_name for c in day1} == {"temp_day", "rh_day"}
        assert next(c for c in day1 if c.param_name == "temp_day").value == 25.0
        # Day 14 in Flower.
        day14 = [c for c in grid if c.day == 14]
        assert next(c for c in day14 if c.param_name == "temp_day").value == 27.0

    def test_override_only_in_overrides(self) -> None:
        r = _recipe_two_phases()
        ov_temp = _override(day=5, param="temp_day", value=24.0)
        ov_orphan = _override(day=5, param="ppfd", value=450.0, unit="umol")
        grid = resolve_all_effective_targets(r, overrides=[ov_temp, ov_orphan])
        # Day 5 now has 3 cells (temp_day overridden, rh_day default, ppfd override-only).
        day5 = [c for c in grid if c.day == 5]
        assert len(day5) == 3
        params_to_sources = {c.param_name: c.source for c in day5}
        assert params_to_sources == {
            "temp_day": "day_override",
            "rh_day": "phase_default",
            "ppfd": "day_override",
        }
        # Other days unaffected by the override-only param.
        day6 = [c for c in grid if c.day == 6]
        assert {c.param_name for c in day6} == {"temp_day", "rh_day"}

    def test_grid_is_day_then_param_sorted(self) -> None:
        r = _recipe_two_phases()
        grid = resolve_all_effective_targets(r, overrides=[])
        # Stable order: day ascending, then param_name ascending.
        seen: list[tuple[int, str]] = [(c.day, c.param_name) for c in grid]
        assert seen == sorted(seen)


# ---------------------------------------------------------------------------
# validate_recipe
# ---------------------------------------------------------------------------


class TestValidateRecipe:
    """The semantic validator emits every finding the wizard can show."""

    def test_clean_recipe_has_no_hard_findings(self) -> None:
        r = _recipe_two_phases()
        findings = validate_recipe(r, overrides=[])
        # No hard refusals.
        assert all(not f.hard for f in findings)

    def test_contiguity_break_surfaces_finding(self) -> None:
        """Hand-mutate the phase boundary to fake a gap."""
        r = _recipe_two_phases()
        # Break contiguity: shift Flower start to day 9 (leaves day 8 gapped).
        r.phases[1].start_day = 9
        findings = validate_recipe(r, overrides=[])
        codes = [f.code for f in findings]
        assert "recipe_phases_not_contiguous" in codes
        # The contiguity finding is hard.
        for f in findings:
            if f.code == "recipe_phases_not_contiguous":
                assert f.hard is True

    def test_first_phase_not_at_day_1_surfaces_finding(self) -> None:
        r = _recipe_two_phases()
        r.phases[0].start_day = 2
        findings = validate_recipe(r, overrides=[])
        codes = [f.code for f in findings]
        assert "recipe_phases_do_not_start_at_day_1" in codes

    def test_last_phase_short_of_cycle_surfaces_finding(self) -> None:
        r = _recipe_two_phases()
        r.phases[-1].end_day = 13  # cycle is 14, leaves day 14 uncovered.
        findings = validate_recipe(r, overrides=[])
        codes = [f.code for f in findings]
        assert "recipe_phases_do_not_cover_cycle" in codes

    def test_override_day_out_of_cycle(self) -> None:
        r = _recipe_two_phases()
        ov = _override(day=99, param="temp_day", value=25.0)
        findings = validate_recipe(r, overrides=[ov])
        hard = [f for f in findings if f.hard]
        assert any(f.code == "override_day_out_of_cycle" for f in hard)

    def test_override_param_orphan_is_warning(self) -> None:
        r = _recipe_two_phases()
        # 'ppfd' isn't declared by either phase's targets map.
        ov = _override(day=3, param="ppfd", value=450.0)
        findings = validate_recipe(r, overrides=[ov])
        orphans = [f for f in findings if f.code == "override_param_orphan"]
        assert len(orphans) == 1
        assert orphans[0].hard is False

    def test_empty_phase_targets_is_warning(self) -> None:
        r = GrowRecipeBase.model_validate(
            {
                "name": "Empty phase",
                "recipeType": ["indoor"],
                "isActive": True,
                "phases": [
                    {
                        "phaseName": "Veg",
                        "durationDays": 7,
                        "order": 1,
                        # Intentionally NO targets.
                    }
                ],
            }
        )
        findings = validate_recipe(r, overrides=[])
        warnings = [f for f in findings if f.code == "phase_targets_empty"]
        assert len(warnings) == 1
        assert warnings[0].hard is False


# ---------------------------------------------------------------------------
# tools.migrate_recipe_revision_to_grow_recipes.convert_revision
# ---------------------------------------------------------------------------


def _stub_revision(
    *,
    params: list[dict[str, Any]],
    cycle_day_count: int = 84,
) -> RecipeRevision:
    """Build a detached :class:`RecipeRevision` + params for converter tests."""
    rev = RecipeRevision(
        id=1,
        room_id="r1",
        version=2,
        name="Test recipe",
        cycle_day_count=cycle_day_count,
        status=RecipeStatus.approved,
        created_by="cult-1",
        approved_by="qap-1",
        notes=None,
    )
    rev.params = [
        RecipeRevisionParam(
            id=i,
            recipe_revision_id=1,
            room_id="r1",
            day_index=int(p["day_index"]),
            param_name=str(p["param_name"]),
            value=float(p["value"]),
            tolerance=p.get("tolerance"),
            unit=p.get("unit"),
        )
        for i, p in enumerate(params, start=1)
    ]
    return rev


class TestMigrateRevisionToGrowRecipes:
    """One-revision-in -> (one recipe, N overrides)-out conversion."""

    def test_empty_params_produces_recipe_with_no_overrides(self) -> None:
        rev = _stub_revision(params=[], cycle_day_count=84)
        result = convert_revision(rev, org_id="open-crop-steering", now=_T)
        assert isinstance(result, ConversionResult)
        assert result.recipe.org_id == "open-crop-steering"
        # Exactly one placeholder phase spanning the whole cycle.
        assert len(result.recipe.phases) == 1
        phase = result.recipe.phases[0]
        assert phase["startDay"] == 1
        assert phase["endDay"] == 84
        assert phase["durationDays"] == 84.0
        assert result.overrides == []

    def test_each_param_becomes_one_override(self) -> None:
        rev = _stub_revision(
            params=[
                {
                    "day_index": 1,
                    "param_name": "temp_day",
                    "value": 25.0,
                    "tolerance": 0.5,
                    "unit": "C",
                },
                {
                    "day_index": 1,
                    "param_name": "rh_day",
                    "value": 70.0,
                    "tolerance": 3.0,
                    "unit": "%",
                },
                {
                    "day_index": 2,
                    "param_name": "temp_day",
                    "value": 25.5,
                    "tolerance": 0.5,
                    "unit": "C",
                },
            ],
            cycle_day_count=84,
        )
        result = convert_revision(rev, org_id="open-crop-steering", now=_T)
        assert len(result.overrides) == 3
        # Same recipe_id stamped on every override.
        rid = result.recipe.id
        assert all(ov.recipe_id == rid for ov in result.overrides)
        # Per-cell value / tolerance / unit preserved.
        by_cell = {
            (ov.day, ov.param_name): ov for ov in result.overrides
        }
        assert by_cell[(1, "temp_day")].value == 25.0
        assert by_cell[(1, "temp_day")].tolerance == 0.5
        assert by_cell[(1, "temp_day")].unit == "C"
        assert by_cell[(2, "temp_day")].value == 25.5

    def test_metadata_round_trip(self) -> None:
        rev = _stub_revision(params=[], cycle_day_count=84)
        result = convert_revision(rev, org_id="org-abc", now=_T)
        # Description embeds the source revision id for forensic tracing.
        assert "1" in (result.recipe.description or "")
        # version field carries through.
        assert result.recipe.version == 2.0
        # Cultivator is preserved as created_by; approver as last_modified.
        assert result.recipe.created_by == "cult-1"
        assert result.recipe.last_modified_by == "qap-1"
        assert result.recipe.created_at == _T
        assert result.recipe.updated_at == _T

    def test_converted_recipe_resolves_through_resolver(self) -> None:
        """The conversion output is consumable by the resolver end-to-end."""
        rev = _stub_revision(
            params=[
                {
                    "day_index": 1,
                    "param_name": "temp_day",
                    "value": 25.0,
                    "tolerance": 0.5,
                    "unit": "C",
                },
                {
                    "day_index": 2,
                    "param_name": "temp_day",
                    "value": 25.5,
                    "tolerance": 0.5,
                    "unit": "C",
                },
            ],
            cycle_day_count=14,
        )
        result = convert_revision(rev, org_id="open-crop-steering", now=_T)
        validated = GrowRecipeBase.model_validate(
            {
                "name": result.recipe.name,
                "recipeType": result.recipe.recipe_type,
                "isActive": result.recipe.is_active,
                "phases": result.recipe.phases,
            }
        )
        assert validated.cycle_day_count == 14
        # Day-1 override resolves as day_override.
        day1 = resolve_effective_target(
            validated, day=1, param="temp_day", overrides=result.overrides
        )
        assert day1.value == 25.0
        assert day1.source == "day_override"
        # A day with NO override but inside the cycle has no phase
        # default -> raises (single placeholder phase has no targets).
        with pytest.raises(RecipeResolutionError):
            resolve_effective_target(
                validated, day=5, param="temp_day", overrides=result.overrides
            )
