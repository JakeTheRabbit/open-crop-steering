"""Batch entity — Convex-mirror of AiGrowApp's ``batches`` table.

A growing cycle that groups plants of the same genetics through a
shared schedule — adopted from
``stewnight/AiGrowApp:convex/schema/grow.ts``. Each batch references a
:class:`~app.models.genetics.Genetics` row and may optionally reference
a :class:`~app.models.grow_recipe.GrowRecipe` for phase-based protocols.

Column set, naming and indexes mirror the Convex shape so a future sync
layer between OCS and AiGrowApp can match rows by ``id`` / ``org_id``
without renaming a single field.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Batch(Base):
    """A growing cycle / batch.

    Attributes:
        id: Opaque string id (UUID4) — Convex doc-id style.
        org_id: Owning organisation; wire alias ``orgId``.
        batch_code: Auto-generated unique code; wire alias ``batchCode``.
        name: Optional display name.
        genetics: FK to :class:`~app.models.genetics.Genetics`. Required.
        batch_type: Free-text — typically ``standard_cultivation`` /
            ``mother_maintenance`` / ``clone_production`` /
            ``r_and_d`` / ``trial``; wire alias ``batchType``.
        status: Free-text — typically ``planning`` / ``germination`` /
            ``vegetative`` / ``flowering`` / ``harvest`` / ``drying`` /
            ``curing`` / ``completed`` / ``cancelled``.
        current_phase: Current phase number in the grow recipe;
            wire alias ``currentPhase``.
        phase_start_date: Epoch-ms when current phase started;
            wire alias ``phaseStartDate``.
        start_date / end_date: Optional batch lifecycle bounds
            (epoch-ms); wire aliases ``startDate`` / ``endDate``.
        target_plant_count / initial_plant_count / current_plant_count:
            Plant headcount tracking; wire aliases ``targetPlantCount``
            etc.
        expected_yield_grams / actual_yield_wet_grams /
            actual_yield_dry_trimmed_grams: Yield numbers in grams.
        grow_recipe: Optional FK to
            :class:`~app.models.grow_recipe.GrowRecipe`;
            wire alias ``growRecipe``.
        created_by / updated_by: Optional Clerk user ids.
        created_at / updated_at: Audit timestamps.
    """

    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)

    genetics: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("genetics.id", ondelete="CASCADE"),
        nullable=False,
    )
    batch_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)

    current_phase: Mapped[float] = mapped_column(Float, nullable=False)
    phase_start_date: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_date: Mapped[int | None] = mapped_column(Integer, nullable=True)

    target_plant_count: Mapped[float] = mapped_column(Float, nullable=False)
    initial_plant_count: Mapped[float] = mapped_column(Float, nullable=False)
    current_plant_count: Mapped[float] = mapped_column(Float, nullable=False)

    expected_yield_grams: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    actual_yield_wet_grams: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    actual_yield_dry_trimmed_grams: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )

    grow_recipe: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("grow_recipes.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

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
        Index("ix_batches_org", "org_id"),
        Index("ix_batches_org_status", "org_id", "status"),
        Index("ix_batches_org_genetics", "org_id", "genetics"),
        Index("ix_batches_org_grow_recipe", "org_id", "grow_recipe"),
        Index("ix_batches_org_name", "org_id", "name"),
        Index("ix_batches_org_batch_code", "org_id", "batch_code"),
        Index("ix_batches_org_start_date", "org_id", "start_date"),
    )
