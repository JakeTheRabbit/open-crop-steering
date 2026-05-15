"""Rolling cumulative delta + cool-down state per (room, param).

The guardrail validator consults this on every proposed adjustment:

* If the rolling 24h or 7d sum would exceed ``max_cumulative_delta_*``
  for that param class, the proposal is rejected.
* If we're inside a ``cooldown_until`` window for the param (typically
  set after a recent guardrail rejection), the proposal is deferred.

Maintained by ``core/guardrails.py`` on every applied
``runtime_adjustment``.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CumulativeDelta(Base):
    __tablename__ = "cumulative_delta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    param_name: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    sum_delta_24h: Mapped[float] = mapped_column(
        Float, default=0.0, server_default="0", nullable=False
    )
    sum_delta_7d: Mapped[float] = mapped_column(
        Float, default=0.0, server_default="0", nullable=False
    )
    last_updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    cooldown_until: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cooldown_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        UniqueConstraint("room_id", "param_name", name="uq_cumulative_delta_room_param"),
    )
