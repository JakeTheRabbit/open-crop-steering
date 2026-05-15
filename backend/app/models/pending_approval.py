"""SFW (Supervised Approval) requests.

Created by the supervisor when an AI proposal needs a human in the loop.
Routed to Telegram + the UI; either side can decide; 90-min TTL.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PendingStatus(enum.StrEnum):
    open = "open"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"


class PendingApproval(Base):
    __tablename__ = "pending_approval"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    proposal: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot_id: Mapped[int] = mapped_column(Integer, nullable=False)
    llm_call_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    summary: Mapped[str] = mapped_column(String(1024), nullable=False)

    status: Mapped[PendingStatus] = mapped_column(
        SqlEnum(
            PendingStatus, name="pending_status", native_enum=True, create_type=False
        ),
        default=PendingStatus.open,
        server_default="open",
        index=True,
        nullable=False,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    decided_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    decision_channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_notes: Mapped[str | None] = mapped_column(String(1024), nullable=True)
