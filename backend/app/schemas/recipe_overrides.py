"""Pydantic schemas for per-day :class:`GrowRecipe` overrides.

OCS-only extension on top of AiGrowApp's ``growRecipes`` shape. Each
override row is one ``(recipe, day, param_name) -> {value, tolerance?,
unit?}`` cell that overrides the corresponding phase default.

Three shapes live here:

* :class:`DayOverrideCreate` — input. ``day``, ``paramName`` and
  ``value`` are required; ``tolerance`` and ``unit`` are optional. The
  ``recipeId`` is taken from the URL path, not from the body.
* :class:`DayOverrideRead` — persisted row, wire-compatible with
  Convex-style camelCase + epoch-ms timestamps.
* :class:`EffectiveTarget` — the merge output: one cell of the
  ``(day, paramName) -> {value, tolerance?, unit?}`` grid plus a
  ``source`` discriminator telling the UI whether the cell is
  ``"phase_default"`` or ``"day_override"``.

JSON output (via ``model_dump(by_alias=True)``) uses camelCase
throughout to stay byte-aligned with the rest of the Convex-mirror
schemas. Snake_case input is also accepted because
``populate_by_name=True`` is set on every model.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


def _to_epoch_ms(value: dt.datetime) -> int:
    """Convert a timezone-aware datetime to epoch milliseconds."""
    return int(value.timestamp() * 1000)


class DayOverrideCreate(BaseModel):
    """Payload for one (day, paramName) override.

    The owning recipe id is supplied by the URL path of the bulk-replace
    endpoint; it is not part of the body so a single body can be reused
    across recipes without copy-edits.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    day: int = Field(ge=1, description="1-based day within the recipe's cycle.")
    param_name: str = Field(
        min_length=1, max_length=64, alias="paramName"
    )
    value: float
    tolerance: float | None = None
    unit: str | None = Field(default=None, max_length=32)


class DayOverrideRead(BaseModel):
    """A persisted :class:`GrowRecipeDayOverride` row on the wire."""

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )

    id: str
    org_id: str = Field(alias="orgId")
    recipe_id: str = Field(alias="recipeId")
    day: int
    param_name: str = Field(alias="paramName")
    value: float
    tolerance: float | None = None
    unit: str | None = None
    created_at: dt.datetime = Field(alias="createdAt")
    updated_at: dt.datetime = Field(alias="updatedAt")

    @field_serializer("created_at", "updated_at")
    def _ser_timestamps(self, value: dt.datetime) -> int:
        return _to_epoch_ms(value)


EffectiveTargetSource = Literal["phase_default", "day_override"]


class EffectiveTarget(BaseModel):
    """One resolved ``(day, paramName) -> target`` cell.

    Produced by :func:`app.core.recipe_resolver.resolve_effective_target`
    and :func:`app.core.recipe_resolver.resolve_all_effective_targets`.

    Attributes:
        day: 1-based day within the cycle.
        param_name: Parameter name (e.g. ``"tempDay"`` / ``"ppfd"``).
        value: Effective numeric value at this cell.
        tolerance: Effective acceptable deviation, or ``None`` if neither
            the override nor the phase default specified one.
        unit: Effective display unit, or ``None`` if neither side
            specified one.
        phase_name: Name of the phase that owns this day — handy for the
            planner UI to render the phase boundary.
        phase_order: The phase's ``order`` field — convenience for the
            UI to sort/colour by phase.
        source: ``"phase_default"`` if the cell came from the phase
            target map, ``"day_override"`` if a matching override row
            shadowed it.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    day: int = Field(ge=1)
    param_name: str = Field(alias="paramName")
    value: float
    tolerance: float | None = None
    unit: str | None = None
    phase_name: str = Field(alias="phaseName")
    phase_order: float = Field(alias="phaseOrder")
    source: EffectiveTargetSource
