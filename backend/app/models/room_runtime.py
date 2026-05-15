"""Mutable per-room runtime state.

Every other room-shaped table in the schema is either immutable
(``recipe_revision``) or append-only (``audit_event``, ``event_log``,
``sensor_snapshot``). The control plane still needs a small piece of
*mutable* per-room state that the supervisor reads and writes every
tick — the room's rollout stage, the calendar anchor for its cultivation
cycle, its last-known health state, and a couple of operator switches
(``muted`` / ``paused``). That is what ``room_runtime`` holds.

It is a Class-E surface (plan locked decision #10): ``rollout_stage``,
``cycle_start_date``, ``muted`` and ``paused`` are admin-owned and the AI
never writes them. ``current_state`` and ``last_tick_at`` are written by
the deterministic monitor / supervisor as they run — operational
bookkeeping, not AI control.

The row is created lazily the first time a room is ticked (see
:func:`app.core.room_runtime.get_or_create`), so adding a room never
needs a migration.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

#: Default rollout stage for a freshly-created room — report-only for
#: every parameter class (plan: v0.1 ships report-only everywhere).
DEFAULT_ROLLOUT_STAGE = "report_only"


class RoomRuntime(Base):
    """Mutable runtime state for a single cultivation room.

    Attributes:
        room_id: Room identifier (primary key).
        rollout_stage: The room's current rollout stage name. Drives the
            per-class effective mode (see :mod:`app.core.rollout`).
            Admin-owned — the AI never writes it.
        cycle_start_date: Calendar date the current cultivation cycle
            began. ``None`` until an operator starts a cycle; the cycle
            day is derived from it (see
            :func:`app.core.room_runtime.cycle_day_for`).
        current_state: Last health state computed by the deterministic
            monitor — ``healthy`` / ``impaired`` / ``critical``. The
            supervisor only ticks the LLM for a ``healthy`` room.
        muted: Operator mute switch — suppresses warning/info alerts for
            the room. Critical alerts are never muted.
        last_tick_at: Timestamp of the last supervisor tick for the room.
        paused: Operator pause switch — the supervisor skips a paused
            room entirely (no snapshot, no LLM call).
    """

    __tablename__ = "room_runtime"

    room_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    rollout_stage: Mapped[str] = mapped_column(
        String(32),
        default=DEFAULT_ROLLOUT_STAGE,
        server_default=DEFAULT_ROLLOUT_STAGE,
        nullable=False,
    )
    cycle_start_date: Mapped[dt.date | None] = mapped_column(
        Date, nullable=True
    )
    current_state: Mapped[str] = mapped_column(
        String(16),
        default="healthy",
        server_default="healthy",
        nullable=False,
    )
    muted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    last_tick_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paused: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
