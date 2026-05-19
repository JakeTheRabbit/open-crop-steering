"""Room entity — Convex-mirror of AiGrowApp's ``rooms`` table.

The middle tier of the facility hierarchy
``buildings → rooms → locations`` adopted from
``stewnight/AiGrowApp:convex/schema/sites.ts``. A room belongs to one
building and may contain many locations (rows, benches, sensor
mounting points).

Distinct from :class:`app.models.room_runtime.RoomRuntime` — that table
holds OCS-specific mutable runtime state (rollout stage, current health,
equipment map, …). This table holds the room *entity* in
AiGrowApp-aligned shape. A later pass links the two with a foreign
key and migrates the existing ``room_runtime`` rows onto these.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Room(Base):
    """An indoor space within a :class:`Building`.

    Attributes:
        id: Opaque string id (UUID4) — Convex doc-id style.
        org_id: Owning organisation; wire alias ``orgId``.
        building_id: Parent building id; wire alias ``buildingId``.
        name: Display name (e.g. ``"Flower Room 1"``).
        purpose: Free-text purpose (e.g. ``"flowering"``, ``"propagation"``).
        story: Optional story label if the building has multiple stories.
        position_x / position_y: Optional layout coordinates in metres.
        width / height / length: Optional dimensions in metres.
        area: Optional auto-computed floor area in m².
        type: Optional room type (``"growing"``, ``"cleaning"``, …).
        created_at / updated_at: Audit timestamps.
    """

    __tablename__ = "rooms"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)
    building_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("buildings.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    purpose: Mapped[str | None] = mapped_column(String(256), nullable=True)
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
        Index("ix_rooms_org", "org_id"),
        Index("ix_rooms_org_building", "org_id", "building_id"),
    )
