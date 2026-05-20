"""cultivation tier — genetics / batches / plants / grow_recipes (Convex-aligned)

Revision ID: 0009_cultivation_tables
Revises: 0008_equipment_table
Create Date: 2026-05-20

Fourth pass of the AiGrowApp schema alignment. Mirrors the four
cultivation tables defined in
``stewnight/AiGrowApp:convex/schema/grow.ts`` (``genetics``,
``batches``, ``plants``, ``growRecipes``) — the core "what is growing,
where, on which schedule" domain that the supervisor / executor will
eventually read from. ``testSamples`` is intentionally skipped; it
belongs to a later compliance pass.

This migration is purely additive: existing tables (``buildings``,
``rooms``, ``locations``, the runtime tier) are untouched. The new
``plants`` table FKs into ``locations`` (from pass 1) for
``current_location``; the cultivation entities FK to each other
within this migration.

Deviations from the Convex source schema:

* Convex's ``searchIndex`` calls on ``genetics`` (``search_genetics_name``,
  ``search_genetics_prefix``, ``search_genetics_lineage``) are
  approximated as plain btree indexes on Postgres — full-text search is
  out of scope for the alignment pass.
* ``plants.source_seed_inventory`` references AiGrowApp's ``inventory``
  table which does not exist in OCS, so the column is a bare ``varchar``
  with no FK.
* ``growRecipes.phases[].phaseTasks[].taskTemplate`` references
  ``task_templates`` which does not exist in OCS; values are kept as
  opaque ids inside the JSONB ``phases`` column.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_cultivation_tables"
down_revision: str | Sequence[str] | None = "0008_equipment_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- genetics -----------------------------------------------------
    op.execute(
        """
        CREATE TABLE genetics (
            id                      varchar(36)  PRIMARY KEY,
            org_id                  varchar(64)  NOT NULL,
            name                    varchar(256) NOT NULL,
            prefix                  varchar(64)  NOT NULL,
            type                    varchar(64)  NOT NULL,
            phenotype_details       varchar(1024),
            lineage                 varchar(512),
            status                  varchar(64)  NOT NULL,
            terpenes                jsonb,
            growth_characteristics  jsonb,
            created_by              varchar(64),
            updated_by              varchar(64),
            created_at              timestamptz  NOT NULL DEFAULT now(),
            updated_at              timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_genetics_org ON genetics (org_id)")
    op.execute(
        "CREATE INDEX ix_genetics_org_status ON genetics (org_id, status)"
    )
    op.execute("CREATE INDEX ix_genetics_org_type ON genetics (org_id, type)")
    op.execute(
        "CREATE INDEX ix_genetics_org_prefix ON genetics (org_id, prefix)"
    )
    op.execute("CREATE INDEX ix_genetics_name ON genetics (name)")
    op.execute("CREATE INDEX ix_genetics_lineage ON genetics (lineage)")

    # --- grow_recipes -------------------------------------------------
    # Created before batches so the batches.grow_recipe FK can resolve.
    op.execute(
        """
        CREATE TABLE grow_recipes (
            id                              varchar(36)  PRIMARY KEY,
            org_id                          varchar(64)  NOT NULL,
            name                            varchar(256) NOT NULL,
            genetics                        varchar(36)
                REFERENCES genetics(id) ON DELETE SET NULL,
            recipe_type                     jsonb        NOT NULL,
            description                     varchar(2048),
            version                         double precision,
            is_active                       boolean      NOT NULL,
            estimated_total_duration_days   double precision,
            phases                          jsonb        NOT NULL,
            created_by                      varchar(64),
            last_modified_by                varchar(64),
            created_at                      timestamptz  NOT NULL DEFAULT now(),
            updated_at                      timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_grow_recipes_org ON grow_recipes (org_id)")
    op.execute(
        "CREATE INDEX ix_grow_recipes_org_genetics "
        "ON grow_recipes (org_id, genetics)"
    )
    op.execute(
        "CREATE INDEX ix_grow_recipes_org_active "
        "ON grow_recipes (org_id, is_active)"
    )

    # --- batches ------------------------------------------------------
    op.execute(
        """
        CREATE TABLE batches (
            id                              varchar(36)  PRIMARY KEY,
            org_id                          varchar(64)  NOT NULL,
            batch_code                      varchar(64)  NOT NULL,
            name                            varchar(256),
            genetics                        varchar(36)  NOT NULL
                REFERENCES genetics(id) ON DELETE CASCADE,
            batch_type                      varchar(64)  NOT NULL,
            status                          varchar(64)  NOT NULL,
            current_phase                   double precision NOT NULL,
            phase_start_date                bigint       NOT NULL,
            start_date                      bigint,
            end_date                        bigint,
            target_plant_count              double precision NOT NULL,
            initial_plant_count             double precision NOT NULL,
            current_plant_count             double precision NOT NULL,
            expected_yield_grams            double precision,
            actual_yield_wet_grams          double precision,
            actual_yield_dry_trimmed_grams  double precision,
            grow_recipe                     varchar(36)
                REFERENCES grow_recipes(id) ON DELETE SET NULL,
            created_by                      varchar(64),
            updated_by                      varchar(64),
            created_at                      timestamptz  NOT NULL DEFAULT now(),
            updated_at                      timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_batches_org ON batches (org_id)")
    op.execute(
        "CREATE INDEX ix_batches_org_status ON batches (org_id, status)"
    )
    op.execute(
        "CREATE INDEX ix_batches_org_genetics ON batches (org_id, genetics)"
    )
    op.execute(
        "CREATE INDEX ix_batches_org_grow_recipe "
        "ON batches (org_id, grow_recipe)"
    )
    op.execute("CREATE INDEX ix_batches_org_name ON batches (org_id, name)")
    op.execute(
        "CREATE INDEX ix_batches_org_batch_code "
        "ON batches (org_id, batch_code)"
    )
    op.execute(
        "CREATE INDEX ix_batches_org_start_date "
        "ON batches (org_id, start_date)"
    )

    # --- plants -------------------------------------------------------
    # current_location → locations(id) from pass 1 (sites tier).
    # source_seed_inventory has NO FK — references AiGrowApp's
    # inventory table which OCS does not own.
    op.execute(
        """
        CREATE TABLE plants (
            id                      varchar(36)  PRIMARY KEY,
            org_id                  varchar(64)  NOT NULL,
            plant_id                varchar(64)  NOT NULL,
            current_batch           varchar(36)
                REFERENCES batches(id) ON DELETE SET NULL,
            genetics                varchar(36)  NOT NULL
                REFERENCES genetics(id) ON DELETE CASCADE,
            current_location        varchar(36)
                REFERENCES locations(id) ON DELETE SET NULL,
            status                  varchar(64)  NOT NULL,
            source_type             varchar(64),
            planted_date            bigint,
            is_mother_plant         boolean      NOT NULL,
            source_plant            varchar(36)
                REFERENCES plants(id) ON DELETE SET NULL,
            source_seed_inventory   varchar(36),
            harvest_date            bigint,
            harvest_weight          double precision,
            dry_weight              double precision,
            trimmed_weight          double precision,
            health_status           varchar(64),
            notes                   varchar(2048),
            created_by              varchar(64),
            last_modified_by        varchar(64),
            status_changed_at       bigint,
            created_at              timestamptz  NOT NULL DEFAULT now(),
            updated_at              timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_plants_org ON plants (org_id)")
    op.execute(
        "CREATE INDEX ix_plants_org_and_batch "
        "ON plants (org_id, current_batch)"
    )
    op.execute(
        "CREATE INDEX ix_plants_org_and_location "
        "ON plants (org_id, current_location)"
    )
    op.execute("CREATE INDEX ix_plants_org_status ON plants (org_id, status)")
    op.execute(
        "CREATE INDEX ix_plants_org_genetics ON plants (org_id, genetics)"
    )
    op.execute(
        "CREATE INDEX ix_plants_org_plant_id ON plants (org_id, plant_id)"
    )
    op.execute(
        "CREATE INDEX ix_plants_org_batch_plant_id "
        "ON plants (org_id, current_batch, plant_id)"
    )
    op.execute(
        "CREATE INDEX ix_plants_org_batch_planted_date "
        "ON plants (org_id, current_batch, planted_date)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS plants")
    op.execute("DROP TABLE IF EXISTS batches")
    op.execute("DROP TABLE IF EXISTS grow_recipes")
    op.execute("DROP TABLE IF EXISTS genetics")
