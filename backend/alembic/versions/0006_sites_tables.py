"""sites tier — buildings / rooms / locations (Convex-aligned)

Revision ID: 0006_sites_tables
Revises: 0005_room_equipment_map
Create Date: 2026-05-20

First pass of the AiGrowApp schema alignment. Mirrors the three tables
defined in ``stewnight/AiGrowApp:convex/schema/sites.ts`` (``buildings``,
``rooms``, ``locations``) — the facility hierarchy that everything else
references. Every row carries ``org_id`` for multi-tenant isolation and
a Convex-style opaque string primary key.

This migration is purely additive: the existing ``room_runtime`` table
is untouched and the supervisor / executor continue to read from it.
A later pass will link ``room_runtime`` to the new ``rooms`` table
(via ``room_runtime.room_id`` FK) and migrate F1 / F2 / Veg's data
across.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_sites_tables"
down_revision: str | Sequence[str] | None = "0005_room_equipment_map"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- buildings ----------------------------------------------------
    op.execute(
        """
        CREATE TABLE buildings (
            id          varchar(36)  PRIMARY KEY,
            org_id      varchar(64)  NOT NULL,
            name        varchar(256) NOT NULL,
            address     varchar(512),
            stories     jsonb,
            width       double precision,
            height      double precision,
            length      double precision,
            created_at  timestamptz  NOT NULL DEFAULT now(),
            updated_at  timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_buildings_org ON buildings (org_id)")

    # --- rooms --------------------------------------------------------
    op.execute(
        """
        CREATE TABLE rooms (
            id           varchar(36)  PRIMARY KEY,
            org_id       varchar(64)  NOT NULL,
            building_id  varchar(36)  NOT NULL
                REFERENCES buildings(id) ON DELETE CASCADE,
            name         varchar(256) NOT NULL,
            purpose      varchar(256),
            story        varchar(64),
            position_x   double precision,
            position_y   double precision,
            width        double precision,
            height       double precision,
            length       double precision,
            area         double precision,
            type         varchar(64),
            created_at   timestamptz  NOT NULL DEFAULT now(),
            updated_at   timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_rooms_org ON rooms (org_id)")
    op.execute(
        "CREATE INDEX ix_rooms_org_building ON rooms (org_id, building_id)"
    )

    # --- locations ----------------------------------------------------
    op.execute(
        """
        CREATE TABLE locations (
            id          varchar(36)  PRIMARY KEY,
            org_id      varchar(64)  NOT NULL,
            room_id     varchar(36)  NOT NULL
                REFERENCES rooms(id) ON DELETE CASCADE,
            label       varchar(256) NOT NULL,
            path        varchar(512),
            capacity    double precision,
            story       varchar(64),
            position_x  double precision,
            position_y  double precision,
            width       double precision,
            height      double precision,
            length      double precision,
            area        double precision,
            type        varchar(64),
            created_at  timestamptz  NOT NULL DEFAULT now(),
            updated_at  timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_locations_org ON locations (org_id)")
    op.execute(
        "CREATE INDEX ix_locations_org_room ON locations (org_id, room_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS locations")
    op.execute("DROP TABLE IF EXISTS rooms")
    op.execute("DROP TABLE IF EXISTS buildings")
