"""Command queue (outbox pattern) for HA service calls.

Every change to HA goes through this table:

1. Producer (executor / SFW approval / manual) inserts row(s) with status
   ``pending`` and a stable ``idempotency_key``.
2. Executor worker (advisory-locked) consumes rows, calls ``call_service``
   on HA, then verifies the entity actually became what we wrote
   (readback). Result + readback evidence go back into the row.
3. Failures retry with exponential backoff; after N attempts an
   ``audit_event`` of type ``critical_incident`` is emitted.

Batches let us group a "lights-on apply" of many setpoints under a
single idempotency key so a duplicated trigger doesn't re-fire half the
batch.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class CommandStatus(enum.StrEnum):
    pending = "pending"
    applying = "applying"
    applied = "applied"
    failed = "failed"
    cancelled = "cancelled"
    superseded = "superseded"


class CommandBatch(Base):
    """A group of commands meant to be applied as a unit."""

    __tablename__ = "command_batch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    commands: Mapped[list[CommandQueueEntry]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class CommandQueueEntry(Base):
    __tablename__ = "command_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_batch.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    domain: Mapped[str] = mapped_column(String(64), nullable=False)
    service: Mapped[str] = mapped_column(String(64), nullable=False)
    target_entity: Mapped[str] = mapped_column(
        String(255), index=True, nullable=False
    )
    service_data: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    expected_value: Mapped[str | None] = mapped_column(String(255), nullable=True)

    idempotency_key: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )

    status: Mapped[CommandStatus] = mapped_column(
        SqlEnum(
            CommandStatus, name="command_status", native_enum=True, create_type=False
        ),
        default=CommandStatus.pending,
        server_default="pending",
        index=True,
        nullable=False,
    )
    enqueued_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    readback: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    enqueued_by: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)

    batch: Mapped[CommandBatch | None] = relationship(back_populates="commands")

    __table_args__ = (
        Index("ix_command_queue_status_enqueued", "status", "enqueued_at"),
    )
