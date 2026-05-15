"""Per-call LLM trace.

Stores the full prompt + raw response + parser/validator outcome so we
can audit AI behaviour, cost, latency, and degradation patterns. The
``outcome`` enum tells us at a glance whether the call produced a
usable proposal or was rejected for a structural reason
(snapshot stale, schema invalid, etc.).
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    func,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class LLMCallOutcome(enum.StrEnum):
    parsed = "parsed"
    schema_invalid = "schema_invalid"
    snapshot_stale = "snapshot_stale"
    snapshot_unknown = "snapshot_unknown"
    api_error = "api_error"
    timeout = "timeout"


class LLMCallLog(Base):
    __tablename__ = "llm_call_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    called_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )

    snapshot_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
    room_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)

    prompt: Mapped[dict] = mapped_column(JSONB, nullable=False)
    response_raw: Mapped[str | None] = mapped_column(String(65536), nullable=True)
    response_parsed: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    outcome: Mapped[LLMCallOutcome] = mapped_column(
        SqlEnum(
            LLMCallOutcome,
            name="llm_call_outcome",
            native_enum=True,
            create_type=False,
        ),
        index=True,
        nullable=False,
    )
    validator_errors: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )

    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
