"""grow_recipe_day_overrides — sparse per-day overrides on grow_recipes phases

Revision ID: 0012_grow_recipe_day_overrides
Revises: 0011_equipment_external_id
Create Date: 2026-05-20

Pass 6 of the OCS↔AiGrowApp schema alignment. AiGrowApp's
``growRecipes.phases`` array (added in pass 4 / migration
``0009_cultivation_tables``) is the source of truth for *what targets
apply during a phase*. OCS extends it with **per-day overrides** so a
grower can keep the fine-grained per-day setpoint control they had in
the legacy ``recipe_revision_param`` flow while still fitting every day
into a meaningful blocked phase.

Storage is **sparse**: only the (recipe, day, param) cells that actually
deviate from the phase default get a row here. A recipe with zero
overrides has zero rows in this table — the resolver
(:mod:`app.core.recipe_resolver`) simply returns phase defaults for
every cell in that case.

The unique key ``(recipe_id, day, param_name)`` enforces
one-override-per-cell. The bulk-replace endpoint in
:mod:`app.api.cultivation` applies a full new override set atomically by
DELETE-then-INSERT inside a single transaction — the simplest correct
approach against this constraint, and the per-recipe row count is small
enough (per-day-per-param overrides for a 12-week recipe with ~11 params
is at most ~924 rows) that bulk INSERT is fine.

This migration is purely additive: existing tables (the legacy
``recipe_revision`` / ``recipe_revision_param`` pair and the
``effective_target`` materialized view) are untouched. The supervisor
continues to read from ``effective_target`` today; a future pass will
cut it over to read from ``grow_recipes`` + ``grow_recipe_day_overrides``
instead, at which point this table becomes the source of truth.

All ``CREATE TABLE`` / ``CREATE INDEX`` statements use ``IF NOT EXISTS``
so the migration is idempotent — re-running after a partial failure (or
on a database where the table was created out-of-band) is a no-op rather
than a hard error.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_grow_recipe_day_overrides"
down_revision: str | Sequence[str] | None = "0011_equipment_external_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS grow_recipe_day_overrides (
            id            varchar(36)       PRIMARY KEY,
            org_id        varchar(64)       NOT NULL
                          DEFAULT 'open-crop-steering',
            recipe_id     varchar(36)       NOT NULL
                          REFERENCES grow_recipes(id) ON DELETE CASCADE,
            day           integer           NOT NULL,
            param_name    varchar(64)       NOT NULL,
            value         double precision  NOT NULL,
            tolerance     double precision,
            unit          varchar(32),
            created_at    timestamptz       NOT NULL DEFAULT now(),
            updated_at    timestamptz       NOT NULL DEFAULT now(),
            CONSTRAINT uq_grow_recipe_day_override_recipe_day_param
                UNIQUE (recipe_id, day, param_name)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_grow_recipe_day_overrides_org "
        "ON grow_recipe_day_overrides (org_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_grow_recipe_day_overrides_recipe "
        "ON grow_recipe_day_overrides (recipe_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_grow_recipe_day_overrides_recipe_day "
        "ON grow_recipe_day_overrides (recipe_id, day)"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_grow_recipe_day_overrides_recipe_day"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_grow_recipe_day_overrides_recipe"
    )
    op.execute("DROP INDEX IF EXISTS ix_grow_recipe_day_overrides_org")
    op.execute("DROP TABLE IF EXISTS grow_recipe_day_overrides")
