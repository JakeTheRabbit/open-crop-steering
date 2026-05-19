"""Building entity — Convex-mirror of AiGrowApp's ``buildings`` table.

This is the top of the facility hierarchy
``buildings → rooms → locations`` adopted from
``stewnight/AiGrowApp:convex/schema/sites.ts``. The column set, the
naming and the indexes mirror the Convex shape so a future sync layer
between OCS and AiGrowApp can match rows by ``id`` / ``org_id`` without
renaming a single field.

Wire-level alignment lives in :mod:`app.schemas.sites` — the Pydantic
``BuildingRead`` model serialises ``org_id`` as ``orgId`` and the
``datetime`` timestamps as Convex's epoch-millisecond integers, so JSON
emitted by OCS is byte-compatible with a Convex ``buildings`` document.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, Float, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Building(Base):
    """A physical building / structure on a cultivation site.

    Attributes:
        id: Opaque string id (UUID4 with dashes) — Convex doc-id style.
        org_id: Owning organisation; wire alias ``orgId``. Required for
            multi-tenant isolation, indexed.
        name: Display name.
        address: Optional street address.
        stories: Optional list of story labels (e.g. ``["G", "1", "2"]``).
        width / height / length: Optional dimensions in metres.
        created_at / updated_at: Audit timestamps.
    """

    __tablename__ = "buildings"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    stories: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    width: Mapped[float | None] = mapped_column(Float, nullable=True)
    height: Mapped[float | None] = mapped_column(Float, nullable=True)
    length: Mapped[float | None] = mapped_column(Float, nullable=True)

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

    __table_args__ = (Index("ix_buildings_org", "org_id"),)
