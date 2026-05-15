"""room_runtime mutable per-room state

Revision ID: 0002_room_runtime
Revises: 0001_baseline
Create Date: 2026-05-16

Adds the ``room_runtime`` table — the one mutable per-room surface the
architecture needs (rollout stage, cycle-start anchor, last health
state, operator mute/pause switches). See ``app/models/room_runtime.py``
for the column semantics.

The table is intentionally tiny and has no triggers: it holds Class-E
admin state plus operational bookkeeping, not chained audit data.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Alembic identifiers
revision: str = "0002_room_runtime"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE room_runtime (
            room_id varchar(64) PRIMARY KEY,
            rollout_stage varchar(32) NOT NULL DEFAULT 'report_only',
            cycle_start_date date,
            current_state varchar(16) NOT NULL DEFAULT 'healthy',
            muted boolean NOT NULL DEFAULT false,
            last_tick_at timestamptz,
            paused boolean NOT NULL DEFAULT false
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS room_runtime")
