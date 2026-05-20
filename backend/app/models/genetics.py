"""Genetics entity — Convex-mirror of AiGrowApp's ``genetics`` table.

The strain/cultivar definition that every batch and plant references —
adopted from ``stewnight/AiGrowApp:convex/schema/grow.ts``. Column set,
naming and indexes mirror the Convex shape so a future sync layer
between OCS and AiGrowApp can match rows by ``id`` / ``org_id`` without
renaming a single field.

The Convex ``growthCharacteristics`` nested object (flowering /
vegetative time, yield, THC %, CBD %) is stored as a single ``jsonb``
column on Postgres; the nested validators live in
:mod:`app.schemas.cultivation`. Convex's three full-text
``searchIndex`` entries (``search_genetics_name`` /
``search_genetics_prefix`` / ``search_genetics_lineage``) are
approximated as plain btree indexes — Postgres full-text search is
not in scope for the alignment pass; document the deviation here.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Genetics(Base):
    """A genetic strain / cultivar.

    Attributes:
        id: Opaque string id (UUID4) — Convex doc-id style.
        org_id: Owning organisation; wire alias ``orgId``.
        name: Display name (e.g. ``"Northern Lights"``).
        prefix: Short identifier for the genetic line (e.g. ``"NL"``).
        type: Free-text type — typically ``sativa`` / ``indica`` /
            ``hybrid`` / ``auto`` / ``photo``.
        phenotype_details: Optional pheno description.
        lineage: Optional parent strains (free text).
        status: Free-text status — typically ``active`` / ``inactive`` /
            ``archived``.
        terpenes: Optional list of terpene names.
        growth_characteristics: Optional nested object with min/max
            ranges for flowering time, vegetative time, yield, THC %,
            CBD %. Stored as ``jsonb``; validated by
            ``GrowthCharacteristics`` in :mod:`app.schemas.cultivation`.
        created_by / updated_by: Optional Clerk user ids.
        created_at / updated_at: Audit timestamps.
    """

    __tablename__ = "genetics"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    prefix: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    phenotype_details: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )
    lineage: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    terpenes: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    growth_characteristics: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
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
        Index("ix_genetics_org", "org_id"),
        Index("ix_genetics_org_status", "org_id", "status"),
        Index("ix_genetics_org_type", "org_id", "type"),
        Index("ix_genetics_org_prefix", "org_id", "prefix"),
        # Convex search indexes (name / prefix / lineage) → plain btree on
        # Postgres. Full-text search is a deviation tracked in the migration.
        Index("ix_genetics_name", "name"),
        Index("ix_genetics_lineage", "lineage"),
    )
