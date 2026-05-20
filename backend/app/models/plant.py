"""Plant entity — Convex-mirror of AiGrowApp's ``plants`` table.

An individual tracked plant within a :class:`~app.models.batch.Batch` —
adopted from ``stewnight/AiGrowApp:convex/schema/grow.ts``. Required
for seed-to-sale traceability under the NZ Medicinal Cannabis Scheme.

Column set, naming and indexes mirror the Convex shape so a future sync
layer between OCS and AiGrowApp can match rows by ``id`` / ``org_id``
without renaming a single field.

FK strategy:

* ``current_batch`` → :class:`~app.models.batch.Batch` (SET NULL).
* ``genetics`` → :class:`~app.models.genetics.Genetics` (CASCADE).
* ``current_location`` → :class:`~app.models.location.Location`
  (SET NULL).
* ``source_plant`` → self-FK on ``plants.id`` (SET NULL) — for clones.
* ``source_seed_inventory`` references AiGrowApp's ``inventory`` table
  which OCS does not have; stored as bare ``String(36)`` with no FK.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Plant(Base):
    """An individual tracked plant.

    Attributes:
        id: Opaque string id (UUID4) — Convex doc-id style.
        org_id: Owning organisation; wire alias ``orgId``.
        plant_id: User-visible plant identifier (e.g. ``"NL-001"``);
            wire alias ``plantId``.
        current_batch: Optional FK to :class:`~app.models.batch.Batch`;
            wire alias ``currentBatch``.
        genetics: FK to :class:`~app.models.genetics.Genetics`.
        current_location: Optional FK to
            :class:`~app.models.location.Location`;
            wire alias ``currentLocation``.
        status: Free-text — typically ``seedling`` / ``vegetative`` /
            ``flowering`` / ``harvest`` / ``dried`` / ``cured`` /
            ``destroyed`` / ``disposed``.
        source_type: Free-text — typically ``seed`` / ``clone`` /
            ``tissue_culture``; wire alias ``sourceType``.
        planted_date: Optional epoch-ms; wire alias ``plantedDate``.
        is_mother_plant: Whether this plant is held as a mother;
            wire alias ``isMotherPlant``.
        source_plant: Optional self-FK to the mother plant for clones;
            wire alias ``sourcePlant``.
        source_seed_inventory: Optional reference to AiGrowApp's
            ``inventory`` table. OCS has no ``inventory`` table; stored
            as a bare string id with no FK. Wire alias
            ``sourceSeedInventory``.
        harvest_date / harvest_weight / dry_weight / trimmed_weight:
            Optional harvest tracking.
        health_status: Free-text — typically ``healthy`` /
            ``pest_issue`` / ``disease`` / ``nutrient_deficiency`` /
            ``other``; wire alias ``healthStatus``.
        notes: Optional free-text notes.
        created_by / last_modified_by: Optional Clerk user ids.
        status_changed_at: Optional epoch-ms when status last changed;
            wire alias ``statusChangedAt``.
        created_at / updated_at: Audit timestamps.
    """

    __tablename__ = "plants"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plant_id: Mapped[str] = mapped_column(String(64), nullable=False)

    current_batch: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("batches.id", ondelete="SET NULL"),
        nullable=True,
    )
    genetics: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("genetics.id", ondelete="CASCADE"),
        nullable=False,
    )
    current_location: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    planted_date: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_mother_plant: Mapped[bool] = mapped_column(Boolean, nullable=False)

    source_plant: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("plants.id", ondelete="SET NULL"),
        nullable=True,
    )
    # external ref — AiGrowApp's inventory table; OCS does not own this
    # entity, so no FK is enforced.
    source_seed_inventory: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )

    harvest_date: Mapped[int | None] = mapped_column(Integer, nullable=True)
    harvest_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    dry_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    trimmed_weight: Mapped[float | None] = mapped_column(Float, nullable=True)

    health_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_modified_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    status_changed_at: Mapped[int | None] = mapped_column(
        Integer, nullable=True
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
        Index("ix_plants_org", "org_id"),
        Index("ix_plants_org_and_batch", "org_id", "current_batch"),
        Index("ix_plants_org_and_location", "org_id", "current_location"),
        Index("ix_plants_org_status", "org_id", "status"),
        Index("ix_plants_org_genetics", "org_id", "genetics"),
        Index("ix_plants_org_plant_id", "org_id", "plant_id"),
        Index(
            "ix_plants_org_batch_plant_id",
            "org_id",
            "current_batch",
            "plant_id",
        ),
        Index(
            "ix_plants_org_batch_planted_date",
            "org_id",
            "current_batch",
            "planted_date",
        ),
    )
