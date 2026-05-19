"""Operational event log — fast queries for dashboards / Telegram digests.

This is *not* the legal record (that's ``audit_event``). Every row here
links back to its corresponding ``audit_event`` row so the operator can
drill into the full audit row from a dashboard. The split exists so
operational queries don't drag the chained audit table around.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.audit_event import AuditEventType


class EventSeverity(enum.StrEnum):
    info = "info"
    warning = "warning"
    critical = "critical"


class EventLogEntry(Base):
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )
    event_type: Mapped[AuditEventType] = mapped_column(
        SqlEnum(
            AuditEventType,
            name="audit_event_type",
            native_enum=True,
            create_type=False,
        ),
        index=True,
        nullable=False,
    )
    severity: Mapped[EventSeverity] = mapped_column(
        SqlEnum(
            EventSeverity, name="event_severity", native_enum=True, create_type=False
        ),
        index=True,
        nullable=False,
    )

    room_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    summary: Mapped[str] = mapped_column(String(1024), nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    reason_codes: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )

    audit_event_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )

    # notified_at — set by the alerts worker once it has dispatched a
    # notification for this row. This is WORKER bookkeeping and is kept
    # strictly separate from acknowledged_* (a human QAP acknowledgement):
    # the rollout-advance gate keys off acknowledged_at, so the worker
    # must never touch it.
    notified_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    acknowledged_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    acknowledged_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_event_log_severity_occurred", "severity", "occurred_at"),
    )
