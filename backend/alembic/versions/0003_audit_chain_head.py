"""audit chain head pointer — fix concurrent-insert chain fork

Revision ID: 0003_audit_chain_head
Revises: 0002_room_runtime
Create Date: 2026-05-16

Root-cause fix for a tamper-evidence defect in the ``audit_event`` HMAC
chain.

The baseline trigger picked the previous chain row with
``ORDER BY id DESC LIMIT 1`` — i.e. ``MAX(id)``. But ``id`` is assigned
from a ``bigserial`` default *before* the trigger fires and *before* it
takes the ``audit_event_chain`` advisory lock. Two genuinely-concurrent
inserts can therefore acquire the lock in the opposite order to their
``id`` assignment: the chain is built in lock order, so the row with the
*higher* id can end up *earlier* in the chain.

After such a divergence ``MAX(id)`` is no longer the chain tip, and the
*next* insert — which still chains onto ``MAX(id)`` — links onto a
non-tip row. That row then has two successors: the chain **forks**, and
the lower-id row of the diverged pair is orphaned off the verifiable
chain permanently. (Reproduced against Postgres 16; see
``tests/integration/test_audit_chain.py``.)

The fix removes the dependence on ``id`` ordering entirely. A singleton
``audit_chain_head`` row holds the current chain tip's HMAC. The trigger
reads it (under the same advisory lock) instead of ``MAX(id)``, and
advances it to the row it just signed. The tip is now tracked
explicitly, so id order is irrelevant and the chain can no longer fork.

``audit_chain_head`` is trigger-maintained infrastructure: it has no ORM
model and must not be written by application code. It is *not* a
verification trust anchor — ``verify_chain`` walks ``audit_event`` from
genesis and never consults this table.

This migration does not — and cannot — repair chains already forked by
the old trigger (``audit_event`` is append-only). It initialises the
head pointer to the current main-line tip (the highest-id row that has
no successor), so the chain continues cleanly from this point on; any
pre-existing orphan rows remain orphaned and are reported by
``verify_chain``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Alembic identifiers
revision: str = "0003_audit_chain_head"
down_revision: str | Sequence[str] | None = "0002_room_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# New trigger body: chain onto the explicit head pointer, not MAX(id).
_HEAD_POINTER_TRIGGER = r"""
CREATE OR REPLACE FUNCTION audit_event_hmac_chain() RETURNS trigger AS $func$
DECLARE
    prev_hash bytea;
    hmac_key bytea;
    payload bytea;
    key_setting text;
BEGIN
    -- Serialize chain inserts within this txn: only one INSERT may read
    -- and advance the head pointer at a time.
    PERFORM pg_advisory_xact_lock(hashtext('audit_event_chain')::bigint);

    -- The previous row's HMAC comes from the singleton head-pointer row,
    -- NOT from MAX(id). `id` is assigned by the bigserial default before
    -- this trigger (and before the lock above), so id order does not
    -- reliably match chain order; chaining onto MAX(id) would link a row
    -- onto a non-tip row after any concurrent-insert divergence and fork
    -- the chain.
    SELECT head_hmac INTO prev_hash FROM audit_chain_head WHERE singleton;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'audit_chain_head row missing; audit chain head pointer destroyed';
    END IF;
    NEW.prev_event_hash := prev_hash;

    BEGIN
        key_setting := current_setting('audit.hmac_key_' || NEW.key_id::text, false);
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION
            'audit.hmac_key_% not configured for this session (set via SET LOCAL or set_config())',
            NEW.key_id;
    END;
    IF key_setting IS NULL OR key_setting = '' THEN
        RAISE EXCEPTION 'audit.hmac_key_% is empty', NEW.key_id;
    END IF;
    hmac_key := decode(key_setting, 'hex');

    -- Order-stable canonical payload, fields separated by US (\037).
    -- UNCHANGED from the baseline trigger — app.core.seal._ROW_HMAC_SQL
    -- and tests/integration/test_audit_chain.py recompute this exact
    -- byte layout and must stay in lockstep.
    payload := prev_hash
        || convert_to(NEW.key_id::text,                            'UTF8') || E'\037'::bytea
        || convert_to((extract(epoch from NEW.occurred_at))::text, 'UTF8') || E'\037'::bytea
        || convert_to(coalesce(NEW.actor_id, ''),                  'UTF8') || E'\037'::bytea
        || convert_to(NEW.event_type::text,                        'UTF8') || E'\037'::bytea
        || convert_to(coalesce(NEW.room_id, ''),                   'UTF8') || E'\037'::bytea
        || convert_to(NEW.params::text,                            'UTF8') || E'\037'::bytea
        || convert_to(NEW.reason_codes::text,                      'UTF8');

    NEW.hmac := hmac(payload, hmac_key, 'sha256');

    -- Advance the chain head pointer to the row we just signed.
    UPDATE audit_chain_head
       SET head_hmac = NEW.hmac,
           head_event_id = NEW.id,
           updated_at = now()
     WHERE singleton;

    RETURN NEW;
END;
$func$ LANGUAGE plpgsql
"""


# Baseline trigger body (chains onto MAX(id)) — restored on downgrade.
_MAX_ID_TRIGGER = r"""
CREATE OR REPLACE FUNCTION audit_event_hmac_chain() RETURNS trigger AS $func$
DECLARE
    prev_hash bytea;
    hmac_key bytea;
    payload bytea;
    key_setting text;
BEGIN
    -- Serialize chain inserts within this txn so concurrent INSERTs
    -- never both observe the same "previous head" and corrupt the chain.
    PERFORM pg_advisory_xact_lock(hashtext('audit_event_chain')::bigint);

    SELECT a.hmac INTO prev_hash
    FROM audit_event a
    ORDER BY a.id DESC
    LIMIT 1;
    IF prev_hash IS NULL THEN
        prev_hash := decode(repeat('0', 64), 'hex');
    END IF;
    NEW.prev_event_hash := prev_hash;

    BEGIN
        key_setting := current_setting('audit.hmac_key_' || NEW.key_id::text, false);
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION
            'audit.hmac_key_% not configured for this session (set via SET LOCAL or set_config())',
            NEW.key_id;
    END;
    IF key_setting IS NULL OR key_setting = '' THEN
        RAISE EXCEPTION 'audit.hmac_key_% is empty', NEW.key_id;
    END IF;
    hmac_key := decode(key_setting, 'hex');

    -- Order-stable canonical payload, fields separated by US (\037)
    payload := prev_hash
        || convert_to(NEW.key_id::text,                            'UTF8') || E'\037'::bytea
        || convert_to((extract(epoch from NEW.occurred_at))::text, 'UTF8') || E'\037'::bytea
        || convert_to(coalesce(NEW.actor_id, ''),                  'UTF8') || E'\037'::bytea
        || convert_to(NEW.event_type::text,                        'UTF8') || E'\037'::bytea
        || convert_to(coalesce(NEW.room_id, ''),                   'UTF8') || E'\037'::bytea
        || convert_to(NEW.params::text,                            'UTF8') || E'\037'::bytea
        || convert_to(NEW.reason_codes::text,                      'UTF8');

    NEW.hmac := hmac(payload, hmac_key, 'sha256');
    RETURN NEW;
END;
$func$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    # Singleton head-pointer table. The PK + CHECK on `singleton` allow
    # exactly one row to exist.
    op.execute(
        """
        CREATE TABLE audit_chain_head (
            singleton boolean PRIMARY KEY DEFAULT true,
            head_hmac bytea NOT NULL,
            head_event_id bigint,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_audit_chain_head_singleton CHECK (singleton)
        )
        """
    )

    # Seed the head pointer from the current chain tip. The tip is the
    # row no other row links back to; if the chain was already forked by
    # the old trigger there may be several such rows, so pick the
    # highest-id one (the main line always runs through the higher ids —
    # divergence orphans the *lower*-id row of a pair). An empty table
    # seeds the all-zero genesis hash so the first insert starts a chain.
    op.execute(
        """
        INSERT INTO audit_chain_head (singleton, head_hmac, head_event_id)
        VALUES (
            true,
            COALESCE(
                (
                    SELECT a.hmac FROM audit_event a
                    WHERE NOT EXISTS (
                        SELECT 1 FROM audit_event b
                        WHERE b.prev_event_hash = a.hmac
                    )
                    ORDER BY a.id DESC
                    LIMIT 1
                ),
                decode(repeat('0', 64), 'hex')
            ),
            (
                SELECT a.id FROM audit_event a
                WHERE NOT EXISTS (
                    SELECT 1 FROM audit_event b
                    WHERE b.prev_event_hash = a.hmac
                )
                ORDER BY a.id DESC
                LIMIT 1
            )
        )
        """
    )

    # Swap the trigger body to read/advance the head pointer. The trigger
    # object trg_audit_event_hmac_chain already targets this function.
    op.execute(_HEAD_POINTER_TRIGGER)


def downgrade() -> None:
    # Restore the baseline MAX(id) trigger, then drop the head pointer.
    op.execute(_MAX_ID_TRIGGER)
    op.execute("DROP TABLE IF EXISTS audit_chain_head")
