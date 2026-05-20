"""equipment.external_id + equipment.integration_id (entity-picker rewire, pass 5b)

Revision ID: 0011_equipment_external_id
Revises: 0010_org_id_backfill
Create Date: 2026-05-20

Pass 5b of the OCS↔AiGrowApp alignment work — rewires the room-config
entity picker onto the new relational tables. Each HA actuator the
operator picks becomes an :class:`~app.models.equipment.Equipment`
row whose ``external_id`` carries the HA entity id and whose
``integration_id`` points at the singleton ``home_assistant``
``sensor_integrations`` row.

Both columns are nullable: pre-existing equipment rows (and rows
created outside the picker flow) have no HA origin and are valid
without these fields. The composite ``(org_id, external_id)`` index
covers the picker's "does this room already have a record for this
HA entity?" lookup; ``(org_id, integration_id)`` mirrors the existing
sensor index so per-integration scans stay cheap.

Idempotent — every ``ADD COLUMN`` / ``CREATE INDEX`` uses
``IF NOT EXISTS`` so a re-run after partial failure is a no-op.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_equipment_external_id"
down_revision: str | Sequence[str] | None = "0010_org_id_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE equipment
            ADD COLUMN IF NOT EXISTS external_id varchar(256)
        """
    )
    op.execute(
        """
        ALTER TABLE equipment
            ADD COLUMN IF NOT EXISTS integration_id varchar(36)
                REFERENCES sensor_integrations(id) ON DELETE SET NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_equipment_org_external "
        "ON equipment (org_id, external_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_equipment_org_integration "
        "ON equipment (org_id, integration_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_equipment_org_integration")
    op.execute("DROP INDEX IF EXISTS ix_equipment_org_external")
    op.execute("ALTER TABLE equipment DROP COLUMN IF EXISTS integration_id")
    op.execute("ALTER TABLE equipment DROP COLUMN IF EXISTS external_id")
