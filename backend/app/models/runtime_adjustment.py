"""Runtime overlays — bounded, time-limited deltas the AI is allowed to add.

The effective target the executor actually writes to HA is
``recipe value + sum(active overlay deltas)``. Overlays expire (default:
at lights-off) and revert to the pure recipe value.

Three sources are tracked separately so audit can distinguish AI-driven
nudges from human edits.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AdjustmentSource(enum.StrEnum):
    ai_auto = "ai_auto"
    ai_sfw = "ai_sfw"
    operator = "operator"
    cultivator = "cultivator"


class AdjustmentMode(enum.StrEnum):
    """Mirrors the rollout mode at the time the overlay was added."""

    report_only = "report_only"
    supervised_approval = "supervised_approval"
    bounded_auto_adjust = "bounded_auto_adjust"


class RuntimeAdjustment(Base):
    __tablename__ = "runtime_adjustment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    day_index: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    param_name: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    delta: Mapped[float] = mapped_column(Float, nullable=False)

    source: Mapped[AdjustmentSource] = mapped_column(
        SqlEnum(
            AdjustmentSource,
            name="adjustment_source",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
    )
    mode: Mapped[AdjustmentMode] = mapped_column(
        SqlEnum(
            AdjustmentMode,
            name="adjustment_mode",
            native_enum=True,
            create_type=False,
        ),
        nullable=False,
    )

    created_by: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )

    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", index=True, nullable=False
    )
    reverted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reverted_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    novel_proposal: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    reason_codes: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
