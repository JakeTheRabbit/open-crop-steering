"""GrowRecipeDayOverride — sparse per-day override of a recipe phase default.

Pass 6 of the OCS↔AiGrowApp schema alignment. AiGrowApp's
``growRecipes.phases`` array is the source of truth for *what the
targets are during a phase*; OCS extends that shape with **per-day
overrides** so a grower can keep the fine-grained per-day control they
already had in the legacy ``recipe_revision_param`` flow while still
slotting every day into a meaningful phase.

Storage is **sparse**: only the (recipe, day, param) cells that actually
deviate from the phase default get a row here. A recipe with zero
overrides has zero rows in this table.

The merge / read API lives in :mod:`app.core.recipe_resolver`. The hot
path is *"for recipe R, day N, param X, what's the effective target?"*
— that resolver looks up the phase containing day N, reads
``phase.targets[param_name]`` as the default, and overlays a matching
override row (if any) on top.

This table is independent of the legacy ``recipe_revision`` /
``recipe_revision_param`` pair (which the supervisor still reads from
today via the ``effective_target`` materialized view). A future pass
will cut the supervisor over to read from ``grow_recipes`` +
``grow_recipe_day_overrides`` instead; until then both shapes coexist.

The Convex side has no equivalent — this is an OCS-only extension. The
``org_id`` column is therefore not in any AiGrowApp schema; it follows
the OCS convention of stamping every row with the tenant id so future
multi-tenant filters apply uniformly.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GrowRecipeDayOverride(Base):
    """One per-day, per-param override of a :class:`GrowRecipe` phase default.

    Each row is one cell in a sparse ``(day x param)`` grid: the value
    the grower wants on day ``day`` for parameter ``param_name``,
    overriding whatever the containing phase's ``targets[param_name]``
    would otherwise provide.

    The ``UNIQUE (recipe_id, day, param_name)`` constraint enforces
    one-override-per-cell. The bulk-replace endpoint in
    :mod:`app.api.cultivation` uses DELETE-then-INSERT inside a single
    transaction to apply a full new override set atomically.

    Attributes:
        id: Opaque string id (UUID4 with dashes) — same doc-id style as
            every other Convex-mirror table.
        org_id: Owning organisation; wire alias ``orgId``. Stamped from
            ``settings.ocs_org_id`` at create time so multi-tenant
            filters apply uniformly. No AiGrowApp counterpart — this
            table is an OCS-only extension.
        recipe_id: The parent :class:`GrowRecipe`. ``ON DELETE CASCADE``
            because an override has no meaning without its recipe.
        day: 1-based day within the recipe's cycle. Must fall inside
            ``[1, sum(phases[*].duration_days)]`` — the resolver and
            validator check this; the column itself only requires the
            value be a positive integer.
        param_name: The parameter being overridden (e.g. ``"tempDay"``,
            ``"temp_day"``, ``"ppfd"``, ``"co2"``). Loose — any string
            up to 64 chars — to match the legacy
            :class:`RecipeRevisionParam` convention which uses arbitrary
            preset-defined names.
        value: The numeric setpoint for this day x param cell.
        tolerance: Optional acceptable deviation around ``value`` in the
            same units as ``value``. ``None`` means "use the phase
            default tolerance if there is one".
        unit: Optional display unit (``"C"``, ``"%"``, ``"ppm"``, ...).
            ``None`` means "use the phase default unit if there is one".
        created_at / updated_at: Audit timestamps.
    """

    __tablename__ = "grow_recipe_day_overrides"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)

    recipe_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("grow_recipes.id", ondelete="CASCADE"),
        nullable=False,
    )
    day: Mapped[int] = mapped_column(Integer, nullable=False)
    param_name: Mapped[str] = mapped_column(String(64), nullable=False)

    value: Mapped[float] = mapped_column(Float, nullable=False)
    tolerance: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "recipe_id",
            "day",
            "param_name",
            name="uq_grow_recipe_day_override_recipe_day_param",
        ),
        Index("ix_grow_recipe_day_overrides_org", "org_id"),
        Index(
            "ix_grow_recipe_day_overrides_recipe",
            "recipe_id",
        ),
        Index(
            "ix_grow_recipe_day_overrides_recipe_day",
            "recipe_id",
            "day",
        ),
    )
