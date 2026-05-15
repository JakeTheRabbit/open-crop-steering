"""Cached snapshot of the room state at supervisor-tick time.

The LLM proposal echoes back ``snapshot_id``; the validator rejects
proposals whose echoed snapshot_id is unknown or stale (i.e. the room
state has materially changed since). This closes the "AI proposes for
state A but state has shifted to state B by the time we'd apply it"
race.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SensorSnapshot(Base):
    __tablename__ = "sensor_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    captured_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )

    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    recipe_revision_id: Mapped[int] = mapped_column(Integer, nullable=False)
    cycle_day: Mapped[int] = mapped_column(Integer, nullable=False)
    rollout_stage: Mapped[str] = mapped_column(String(32), nullable=False)
