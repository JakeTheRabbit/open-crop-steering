"""room_runtime.equipment_map — the persisted room store

Revision ID: 0005_room_equipment_map
Revises: 0004_event_log_notified_at
Create Date: 2026-05-19

v0.1 deferred the "room store" — the mapping of which HA entity is a
room's temp sensor, lights switch, per-zone VWC probe, etc. The
supervisor's ``_rooms_provider`` returned an empty list because there
was nowhere to read room configuration from.

This adds two columns to ``room_runtime`` so a room row carries its
operator-assigned equipment map:

* ``display_name`` — the operator-facing room name (the HA area name).
* ``equipment_map`` — a serialized ``RoomEquipmentMap`` (see
  ``app/api/config_wizard.py``): the entity assignments produced by the
  GUI entity picker. ``{}`` until an admin configures the room.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_room_equipment_map"
down_revision: str | Sequence[str] | None = "0004_event_log_notified_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE room_runtime ADD COLUMN display_name varchar(128)")
    op.execute(
        "ALTER TABLE room_runtime "
        "ADD COLUMN equipment_map jsonb NOT NULL DEFAULT '{}'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE room_runtime DROP COLUMN IF EXISTS equipment_map")
    op.execute("ALTER TABLE room_runtime DROP COLUMN IF EXISTS display_name")
