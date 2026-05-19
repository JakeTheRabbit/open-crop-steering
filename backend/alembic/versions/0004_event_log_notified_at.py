"""event_log.notified_at — separate worker-dispatch marker from QAP ack

Revision ID: 0004_event_log_notified_at
Revises: 0003_audit_chain_head
Create Date: 2026-05-19

The alerts worker needs a "I have already sent a notification for this
row" marker so it does not re-notify. It was overloading
``acknowledged_at`` for that — but ``acknowledged_at`` is *also* what
``list_open_deviations`` and the rollout-advance gate read as "a QAP has
acknowledged this formal deviation".

The effect of the overload: a ``formal_deviation`` row was stamped
``acknowledged_at`` by the alerts worker seconds after it was raised, so
it immediately dropped off the open-deviations list and the rollout gate
saw zero open deviations — i.e. the "QAP must clear a deviation before
the AI gains autonomy" safety gate never actually blocked anything.

This migration adds ``event_log.notified_at`` for the worker's own
bookkeeping and repairs historical rows: any row the worker had stamped
(``acknowledged_by = 'alerts-worker'``) has that moved to
``notified_at`` and its spurious human-ack cleared.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_event_log_notified_at"
down_revision: str | Sequence[str] | None = "0003_audit_chain_head"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE event_log ADD COLUMN notified_at timestamptz")
    # Partial index — the worker scans for the not-yet-dispatched rows.
    op.execute(
        "CREATE INDEX ix_event_log_notified_at ON event_log (notified_at) "
        "WHERE notified_at IS NULL"
    )
    # Repair rows the alerts worker previously stamped: move its
    # dispatch marker to notified_at and clear the spurious human ack so
    # genuine QAP acknowledgements remain distinguishable.
    op.execute(
        """
        UPDATE event_log
        SET notified_at = acknowledged_at,
            acknowledged_at = NULL,
            acknowledged_by = NULL
        WHERE acknowledged_by = 'alerts-worker'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_event_log_notified_at")
    op.execute("ALTER TABLE event_log DROP COLUMN IF EXISTS notified_at")
