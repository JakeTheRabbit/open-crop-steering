"""GrowRecipe entity — Convex-mirror of AiGrowApp's ``growRecipes`` table.

A phase-based cultivation protocol — light cycles, environmental
targets, nutrients and phase-keyed tasks — adopted from
``stewnight/AiGrowApp:convex/schema/grow.ts``. Each batch optionally
references a recipe to drive its phase schedule.

Column set, naming and indexes mirror the Convex shape so a future sync
layer between OCS and AiGrowApp can match rows by ``id`` / ``org_id``
without renaming a single field.

The Convex ``phases`` array is a deeply nested structure (per-phase
light cycle, env targets, nutrients, phase tasks). It is stored as a
single ``jsonb`` column on Postgres; full typed validators live in
:mod:`app.schemas.cultivation` (``GrowRecipePhase`` and its sub-models).

Note: the per-phase ``phaseTasks[].taskTemplate`` field references
AiGrowApp's ``task_templates`` table which OCS does not have. Because
it lives inside the JSONB payload, no FK is needed; values are kept as
opaque string ids and validated as such.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GrowRecipe(Base):
    """A cultivation protocol.

    Attributes:
        id: Opaque string id (UUID4) — Convex doc-id style.
        org_id: Owning organisation; wire alias ``orgId``.
        name: Display name.
        genetics: Optional FK to
            :class:`~app.models.genetics.Genetics`.
        recipe_type: List of recipe types (e.g. ``["indoor", "hydroponic"]``);
            wire alias ``recipeType``.
        description: Optional free-text description.
        version: Optional version number.
        is_active: Whether the recipe is currently active; wire alias
            ``isActive``.
        estimated_total_duration_days: Optional rolled-up duration;
            wire alias ``estimatedTotalDurationDays``.
        phases: Array of phase objects (light cycle, env targets,
            nutrients, phase tasks). Stored as ``jsonb``; validated by
            ``GrowRecipePhase`` in :mod:`app.schemas.cultivation`.
        created_by / last_modified_by: Optional Clerk user ids.
        created_at / updated_at: Audit timestamps.
    """

    __tablename__ = "grow_recipes"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)

    genetics: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("genetics.id", ondelete="SET NULL"),
        nullable=True,
    )

    recipe_type: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    version: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    estimated_total_duration_days: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )

    phases: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)

    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_modified_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

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
        Index("ix_grow_recipes_org", "org_id"),
        Index("ix_grow_recipes_org_genetics", "org_id", "genetics"),
        Index("ix_grow_recipes_org_active", "org_id", "is_active"),
    )
