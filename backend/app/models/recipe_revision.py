"""Recipe revisions — immutable cultivation plans.

Two tables:

* ``recipe_revision`` — header (room, version, status, who/when).
* ``recipe_revision_param`` — one row per (day_index, param_name) for
  fast joins from the ``effective_target`` materialized view.

Once a revision flips to ``approved`` it is immutable — DB-level triggers
in the baseline migration enforce this. AI never writes here; only
human cultivators / QAPs can create + approve revisions.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class RecipeStatus(enum.StrEnum):
    draft = "draft"
    pending_approval = "pending_approval"
    approved = "approved"
    superseded = "superseded"


class RecipeRevision(Base):
    """A single immutable revision of a room's cultivation recipe."""

    __tablename__ = "recipe_revision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    cycle_day_count: Mapped[int] = mapped_column(
        Integer, default=84, server_default="84", nullable=False
    )

    status: Mapped[RecipeStatus] = mapped_column(
        SqlEnum(RecipeStatus, name="recipe_status", native_enum=True, create_type=False),
        default=RecipeStatus.draft,
        server_default="draft",
        index=True,
        nullable=False,
    )

    created_by: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    approved_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    approved_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ``metadata_`` because SQLAlchemy reserves ``metadata`` on Base
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, default=dict, server_default="{}", nullable=False
    )
    notes: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    params: Mapped[list[RecipeRevisionParam]] = relationship(
        back_populates="revision",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("room_id", "version", name="uq_recipe_revision_room_version"),
    )


class RecipeRevisionParam(Base):
    """One row per (revision, day_index, param_name).

    Storing the recipe normalized rather than as a JSONB blob lets us
    join the ``effective_target`` materialized view efficiently.
    """

    __tablename__ = "recipe_revision_param"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_revision_id: Mapped[int] = mapped_column(
        ForeignKey("recipe_revision.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    room_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    day_index: Mapped[int] = mapped_column(Integer, nullable=False)
    param_name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    tolerance: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)

    revision: Mapped[RecipeRevision] = relationship(back_populates="params")

    __table_args__ = (
        UniqueConstraint(
            "recipe_revision_id",
            "day_index",
            "param_name",
            name="uq_recipe_param_rev_day_param",
        ),
    )
