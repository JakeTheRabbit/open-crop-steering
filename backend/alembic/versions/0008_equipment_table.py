"""equipment tier — equipment (Convex-aligned)

Revision ID: 0008_equipment_table
Revises: 0007_sensors_tables
Create Date: 2026-05-20

Third pass of the AiGrowApp schema alignment. Mirrors the ``equipment``
table from ``stewnight/AiGrowApp:convex/schema/assets.ts`` — durable
assets (HVAC, lighting, irrigation, extraction, processing,
monitoring) tied to a room or location.

Only ``equipment`` is ported here. ``materials`` and ``inventory`` from
the same Convex file are inventory-management concerns and live
outside OCS's control-plane scope.

Both FKs (``room_id`` → ``rooms.id``, ``location_id`` → ``locations.id``)
use ``ON DELETE SET NULL`` — a physical unit can outlive any individual
room or location, so a parent delete should null the link rather than
cascade-deleting the asset record.

Convex defines a ``searchIndex("search_name")`` for fuzzy text search;
Postgres equivalent here is a plain B-tree on ``name`` because
full-text search isn't needed for the alignment job.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_equipment_table"
down_revision: str | Sequence[str] | None = "0007_sensors_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE equipment (
            id                    varchar(36)  PRIMARY KEY,
            org_id                varchar(64)  NOT NULL,
            name                  varchar(256) NOT NULL,
            code                  varchar(128) NOT NULL,
            type                  varchar(32)  NOT NULL,
            manufacturer          varchar(256),
            model                 varchar(256),
            serial_number         varchar(256),
            purchase_date         timestamptz,
            warranty_expires      timestamptz,
            room_id               varchar(36)
                REFERENCES rooms(id) ON DELETE SET NULL,
            location_id           varchar(36)
                REFERENCES locations(id) ON DELETE SET NULL,
            status                varchar(32)  NOT NULL,
            last_maintenance      timestamptz,
            next_maintenance      timestamptz,
            maintenance_interval  integer,
            maintenance_notes     varchar(2048),
            notes                 varchar(2048),
            tags                  jsonb,
            is_active             boolean      NOT NULL DEFAULT true,
            created_at            timestamptz  NOT NULL DEFAULT now(),
            updated_at            timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN equipment.type IS
            'Convex union: hvac | lighting | irrigation | extraction '
            '| processing | monitoring | other'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN equipment.status IS
            'Convex union: operational | maintenance | repair | retired'
        """
    )
    op.execute("CREATE INDEX ix_equipment_org ON equipment (org_id)")
    op.execute(
        "CREATE INDEX ix_equipment_org_type ON equipment (org_id, type)"
    )
    op.execute(
        "CREATE INDEX ix_equipment_org_room ON equipment (org_id, room_id)"
    )
    op.execute(
        "CREATE INDEX ix_equipment_org_status ON equipment (org_id, status)"
    )
    op.execute(
        "CREATE INDEX ix_equipment_org_active ON equipment (org_id, is_active)"
    )
    # Convex defines searchIndex("search_name"); Postgres equivalent is
    # a plain B-tree because the alignment doesn't need full-text.
    op.execute("CREATE INDEX ix_equipment_name ON equipment (name)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS equipment")
