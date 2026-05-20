"""Equipment entity — Convex-mirror of AiGrowApp's ``equipment`` table.

Mirrors ``stewnight/AiGrowApp:convex/schema/assets.ts`` (only the
``equipment`` defineTable — ``materials`` and ``inventory`` are inventory
management concerns and live outside OCS's control-plane scope).

Equipment is a durable asset (HVAC unit, lighting fixture, irrigation
controller, sensor enclosure, etc.) that lives in the facility
hierarchy. Both the optional :class:`~app.models.room.Room` and
:class:`~app.models.location.Location` foreign keys use
``ondelete="SET NULL"`` — a physical unit can outlive any individual
room or location, so a parent delete should null the link rather than
cascade-deleting the asset record.

Wire-level alignment lives in :mod:`app.schemas.equipment` — Pydantic
serialises ``org_id`` as ``orgId`` and the ``datetime`` audit timestamps
as Convex's epoch-millisecond integers, so JSON emitted by OCS is
byte-compatible with a Convex ``equipment`` document.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Equipment(Base):
    """A durable asset assigned to a :class:`Room` and/or :class:`Location`.

    Attributes:
        id: Opaque string id (UUID4 with dashes) — Convex doc-id style.
        org_id: Owning organisation; wire alias ``orgId``. Required for
            multi-tenant isolation, indexed.
        name: Display name.
        code: Unique-per-org equipment code (asset tag / serial label).
        type: Asset family. One of ``hvac``, ``lighting``, ``irrigation``,
            ``extraction``, ``processing``, ``monitoring``, ``other``.
        manufacturer / model / serial_number: Optional vendor metadata.
        purchase_date / warranty_expires: Optional epoch-ms timestamps on
            the Convex wire; stored as ``timestamptz`` here.
        room_id: Optional parent room; ``SET NULL`` on parent delete.
        location_id: Optional parent location; ``SET NULL`` on parent
            delete.
        status: Lifecycle state. One of ``operational``, ``maintenance``,
            ``repair``, ``retired``.
        last_maintenance / next_maintenance: Optional service timestamps.
        maintenance_interval: Optional service cadence in days.
        maintenance_notes: Optional free-text service log.
        notes: Optional free-text notes.
        tags: Optional list of tag strings (``jsonb`` for Postgres parity
            with Convex's ``v.array(v.string())``).
        is_active: Soft-delete flag; ``True`` by default.
        created_at / updated_at: Audit timestamps.
    """

    __tablename__ = "equipment"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    # Convex union: hvac | lighting | irrigation | extraction | processing
    # | monitoring | other. Documented here; validated by the Pydantic
    # ``Literal`` on the schema layer.
    type: Mapped[str] = mapped_column(String(32), nullable=False)

    manufacturer: Mapped[str | None] = mapped_column(String(256), nullable=True)
    model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    serial_number: Mapped[str | None] = mapped_column(String(256), nullable=True)
    purchase_date: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    warranty_expires: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    room_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("rooms.id", ondelete="SET NULL"),
        nullable=True,
    )
    location_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Convex union: operational | maintenance | repair | retired.
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    last_maintenance: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_maintenance: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    maintenance_interval: Mapped[int | None] = mapped_column(Integer, nullable=True)
    maintenance_notes: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    notes: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
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
        Index("ix_equipment_org", "org_id"),
        Index("ix_equipment_org_type", "org_id", "type"),
        Index("ix_equipment_org_room", "org_id", "room_id"),
        Index("ix_equipment_org_status", "org_id", "status"),
        Index("ix_equipment_org_active", "org_id", "is_active"),
        # Convex defines a searchIndex("search_name") for fuzzy text
        # search; Postgres equivalent here is a plain B-tree on ``name``
        # because full-text search isn't needed for the alignment job.
        Index("ix_equipment_name", "name"),
    )
