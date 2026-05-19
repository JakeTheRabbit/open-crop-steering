"""Location entity — Convex-mirror of AiGrowApp's ``locations`` table.

The leaf tier of the facility hierarchy
``buildings → rooms → locations`` adopted from
``stewnight/AiGrowApp:convex/schema/sites.ts``. A location is a specific
spot inside a :class:`Room` — a grow row, a bench, a sensor mount, a
storage shelf, etc.

Field shape mirrors the Convex table exactly so a future sync layer
matches rows by ``id`` / ``org_id`` without renaming a single field.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Location(Base):
    """A specific spot inside a :class:`Room`.

    Attributes:
        id: Opaque string id (UUID4).
        org_id: Owning organisation; wire alias ``orgId``.
        room_id: Parent room id; wire alias ``roomId``.
        label: Human-readable label (e.g. ``"Row A"``, ``"Bench 3"``).
        path: Optional hierarchical path (e.g. ``"R1/Row A/Bench 3"``).
        capacity: Optional capacity (e.g. plant count).
        story: Optional story label.
        position_x / position_y: Optional in-room coordinates in metres.
        width / height / length: Optional dimensions in metres.
        area: Optional auto-computed footprint in m².
        type: Optional location type (``"growing"``, ``"storage"``, …).
        created_at / updated_at: Audit timestamps.
    """

    __tablename__ = "locations"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)
    room_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("rooms.id", ondelete="CASCADE"),
        nullable=False,
    )

    label: Mapped[str] = mapped_column(String(256), nullable=False)
    path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    capacity: Mapped[float | None] = mapped_column(Float, nullable=True)
    story: Mapped[str | None] = mapped_column(String(64), nullable=True)

    position_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[float | None] = mapped_column(Float, nullable=True)
    height: Mapped[float | None] = mapped_column(Float, nullable=True)
    length: Mapped[float | None] = mapped_column(Float, nullable=True)
    area: Mapped[float | None] = mapped_column(Float, nullable=True)
    type: Mapped[str | None] = mapped_column(String(64), nullable=True)

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
        Index("ix_locations_org", "org_id"),
        Index("ix_locations_org_room", "org_id", "room_id"),
    )
