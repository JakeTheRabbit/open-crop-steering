"""org_id backfill — every legacy OCS table gets ``org_id`` (Convex-aligned)

Revision ID: 0010_org_id_backfill
Revises: 0009_cultivation_tables
Create Date: 2026-05-20

Pass 7 of 7 of the AiGrowApp schema alignment. AiGrowApp is multi-tenant —
every row carries an ``orgId``. OCS is currently single-tenant. Earlier
passes (1-4) added ``org_id`` to all NEW tables (sites, sensors,
equipment, genetics / cultivation). This pass adds it to every pre-
existing OCS table so the whole schema is uniform and ready for the
later multi-tenant cut-over.

Each column is added as ``varchar(64) NOT NULL DEFAULT 'open-crop-steering'``:

* The server-side ``DEFAULT 'open-crop-steering'`` backfills every existing
  row in a single statement, so the ``NOT NULL`` constraint succeeds with
  no separate ``UPDATE`` and no temporary nullable phase.
* ``'open-crop-steering'`` matches the ``OCS_ORG_ID`` default already wired
  up in ``app/config.py`` (pass 1), so any future ORM-level writes pick up
  the same tenant id without code changes.

Each table also gets an ``ix_<name>_org`` index — single-column on
``org_id`` — so future ``WHERE org_id = :org`` filters are immediately
covered. Composite indexes can be added per-table in a later pass if
query patterns demand it; keeping this migration uniform makes it easy
to reason about.

**This migration is intentionally surgical: schema only.** No ORM model
files change in this pass, and no application code changes. Every
existing query continues to work unchanged — every row in every table has
the same ``org_id``, so ``SELECT … FROM <table>`` returns exactly what it
did before. A follow-up pass will:

  1. Add the ``org_id`` mapped column to each ORM model.
  2. Add ``WHERE org_id = :org`` to every query (or wire it through a
     session-level filter).
  3. Switch the default off once true multi-tenant writes start.

Keeping (1)/(2)/(3) out of this migration keeps the diff readable and
the rollback trivial.

**Notable skip — `effective_target`** is a materialized view (defined in
``0001_baseline``), not a regular table, so ``ALTER TABLE ADD COLUMN``
is not supported on it. The view's columns flow from its source SELECT;
once the source tables (``recipe_revision_param``, ``runtime_adjustment``)
carry ``org_id``, the view can be re-created in the follow-up pass to
project ``org_id`` through. Doing the view rebuild here would expand the
diff beyond schema-only.

All ``ADD COLUMN`` and ``CREATE INDEX`` statements use ``IF NOT EXISTS``
so the migration is idempotent — re-running after a partial failure (or
on a database where some columns were added out-of-band) is a no-op
rather than a hard error.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_org_id_backfill"
down_revision: str | Sequence[str] | None = "0009_cultivation_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Every legacy OCS table that does NOT already carry ``org_id`` from
# passes 1-4. Confirmed against each model file's ``__tablename__``.
#
# Note: ``effective_target`` is a MATERIALIZED VIEW (see
# ``0001_baseline``), not a regular table — ``ALTER TABLE ADD COLUMN``
# does not apply. The view inherits its columns from its source SELECT;
# once the source tables carry ``org_id`` (i.e. after this migration),
# the view can be re-created in a follow-up pass to project it through.
TABLES: tuple[str, ...] = (
    "audit_event",
    "command_batch",
    "command_queue",
    "cumulative_delta",
    "daily_seal",
    "event_log",
    "llm_call_log",
    "pending_approval",
    "recipe_revision",
    "recipe_revision_param",
    "room_runtime",
    "runtime_adjustment",
    "sensor_snapshot",
    "telegram_user_map",
    "users",
    "roles",
    "user_roles",
)


def upgrade() -> None:
    for table in TABLES:
        op.execute(
            f"""
            ALTER TABLE {table}
                ADD COLUMN IF NOT EXISTS org_id varchar(64) NOT NULL
                DEFAULT 'open-crop-steering'
            """
        )
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_org "
            f"ON {table} (org_id)"
        )


def downgrade() -> None:
    # Reverse order: drop the index first, then the column, for each
    # table in reverse of the upgrade sequence.
    for table in reversed(TABLES):
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_org")
        op.execute(
            f"ALTER TABLE {table} DROP COLUMN IF EXISTS org_id"
        )
