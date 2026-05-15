"""baseline schema

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-15

Creates the full Phase-1 schema in one migration:

* All 14+ tables (see ``app/models/``)
* The ``effective_target`` MATERIALIZED VIEW with its unique-by-PK index
* HMAC-chain BEFORE INSERT trigger on ``audit_event``
* UPDATE/DELETE blocker triggers on ``audit_event``
* Immutability triggers on ``recipe_revision`` (when status='approved')
  and on ``recipe_revision_param`` (when parent revision is approved)
* Static seed for ``roles``

The HMAC chain trigger reads its key from the Postgres session GUC
``audit.hmac_key_<key_id>`` (set by ``app/db.py::get_session``). A
transaction-scoped advisory lock serializes inserts so two concurrent
transactions can't both see the same "previous head" and break the
chain.
"""

from __future__ import annotations

from alembic import op

# Alembic identifiers
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


# --------------------------------------------------------------------- DDL
ENUM_TYPES = (
    "llm_call_outcome",
    "pending_status",
    "event_severity",
    "audit_event_type",
    "command_status",
    "adjustment_mode",
    "adjustment_source",
    "recipe_status",
    "role_name",
)


def upgrade() -> None:
    # ---------------------------------------------------------- extensions
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # --------------------------------------------------------------- enums
    op.execute("CREATE TYPE role_name AS ENUM ('operator','cultivator','qap','admin')")
    op.execute(
        "CREATE TYPE recipe_status AS ENUM "
        "('draft','pending_approval','approved','superseded')"
    )
    op.execute(
        "CREATE TYPE adjustment_source AS ENUM "
        "('ai_auto','ai_sfw','operator','cultivator')"
    )
    op.execute(
        "CREATE TYPE adjustment_mode AS ENUM "
        "('report_only','supervised_approval','bounded_auto_adjust')"
    )
    op.execute(
        "CREATE TYPE command_status AS ENUM "
        "('pending','applying','applied','failed','cancelled','superseded')"
    )
    op.execute(
        """
        CREATE TYPE audit_event_type AS ENUM (
            'info_event','controlled_adjustment','system_warning',
            'guardrail_rejection','formal_deviation','critical_incident',
            'recipe_revision_created','recipe_revision_approved',
            'runtime_adjustment_added','runtime_adjustment_reverted',
            'user_role_changed','rollout_advanced','deviation_acknowledged',
            'audit_export','hmac_key_rotated'
        )
        """
    )
    op.execute("CREATE TYPE event_severity AS ENUM ('info','warning','critical')")
    op.execute(
        "CREATE TYPE pending_status AS ENUM "
        "('open','approved','rejected','expired')"
    )
    op.execute(
        """
        CREATE TYPE llm_call_outcome AS ENUM (
            'parsed','schema_invalid','snapshot_stale','snapshot_unknown',
            'api_error','timeout'
        )
        """
    )

    # -------------------------------------------------------------- users
    op.execute(
        """
        CREATE TABLE users (
            id varchar(64) PRIMARY KEY,
            display_name varchar(128) NOT NULL,
            email varchar(256),
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # -------------------------------------------------------------- roles
    op.execute(
        """
        CREATE TABLE roles (
            name role_name PRIMARY KEY,
            description varchar(256)
        )
        """
    )
    op.execute(
        """
        INSERT INTO roles (name, description) VALUES
            ('operator',   'Read-only; acknowledges alerts'),
            ('cultivator', 'Drafts recipe revisions; approves SFW proposals'),
            ('qap',        'Approves recipe revisions; advances rollout; ack deviations'),
            ('admin',      'Assigns roles; sets equipment maps; sets guardrail bounds')
        """
    )

    # --------------------------------------------------------- user_roles
    op.execute(
        """
        CREATE TABLE user_roles (
            user_id varchar(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role_name role_name NOT NULL REFERENCES roles(name) ON DELETE CASCADE,
            granted_at timestamptz NOT NULL DEFAULT now(),
            granted_by varchar(64),
            PRIMARY KEY (user_id, role_name)
        )
        """
    )

    # ---------------------------------------------------- recipe_revision
    op.execute(
        """
        CREATE TABLE recipe_revision (
            id serial PRIMARY KEY,
            room_id varchar(64) NOT NULL,
            version int NOT NULL,
            name varchar(128) NOT NULL,
            cycle_day_count int NOT NULL DEFAULT 84,
            status recipe_status NOT NULL DEFAULT 'draft',
            created_by varchar(64) NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            approved_by varchar(64) REFERENCES users(id),
            approved_at timestamptz,
            metadata jsonb NOT NULL DEFAULT '{}',
            notes varchar(2048),
            CONSTRAINT uq_recipe_revision_room_version UNIQUE (room_id, version)
        )
        """
    )
    op.execute("CREATE INDEX ix_recipe_revision_room_id ON recipe_revision (room_id)")
    op.execute("CREATE INDEX ix_recipe_revision_status  ON recipe_revision (status)")

    # ------------------------------------------------- recipe_revision_param
    op.execute(
        """
        CREATE TABLE recipe_revision_param (
            id serial PRIMARY KEY,
            recipe_revision_id int NOT NULL
                REFERENCES recipe_revision(id) ON DELETE CASCADE,
            room_id varchar(64) NOT NULL,
            day_index int NOT NULL,
            param_name varchar(64) NOT NULL,
            value double precision NOT NULL,
            tolerance double precision,
            unit varchar(32),
            CONSTRAINT uq_recipe_param_rev_day_param
                UNIQUE (recipe_revision_id, day_index, param_name)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_recipe_revision_param_recipe_revision_id "
        "ON recipe_revision_param (recipe_revision_id)"
    )
    op.execute(
        "CREATE INDEX ix_recipe_revision_param_room_id "
        "ON recipe_revision_param (room_id)"
    )

    # -------------------------------------------------- runtime_adjustment
    op.execute(
        """
        CREATE TABLE runtime_adjustment (
            id serial PRIMARY KEY,
            room_id varchar(64) NOT NULL,
            day_index int NOT NULL,
            param_name varchar(64) NOT NULL,
            delta double precision NOT NULL,
            source adjustment_source NOT NULL,
            mode adjustment_mode NOT NULL,
            created_by varchar(64) NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            active boolean NOT NULL DEFAULT true,
            reverted_at timestamptz,
            reverted_by varchar(64) REFERENCES users(id),
            snapshot_id int,
            novel_proposal boolean NOT NULL DEFAULT false,
            reason_codes jsonb NOT NULL DEFAULT '[]'
        )
        """
    )
    for col in ("room_id", "day_index", "param_name", "expires_at", "active", "created_at"):
        op.execute(f"CREATE INDEX ix_runtime_adjustment_{col} ON runtime_adjustment ({col})")
    # Hot-path partial index
    op.execute(
        """
        CREATE INDEX ix_runtime_adjustment_active_room_day_param
            ON runtime_adjustment (room_id, day_index, param_name)
            WHERE active = true
        """
    )

    # ------------------------------------------- effective_target MATVIEW
    # active_recipes = the highest-version 'approved' revision per room
    op.execute(
        """
        CREATE MATERIALIZED VIEW effective_target AS
        WITH active_recipes AS (
            SELECT DISTINCT ON (room_id) id, room_id
            FROM recipe_revision
            WHERE status = 'approved'
            ORDER BY room_id, version DESC
        )
        SELECT
            rrp.room_id,
            rrp.day_index,
            rrp.param_name,
            (rrp.value + COALESCE(
                SUM(ra.delta) FILTER (WHERE ra.active AND ra.expires_at > now()),
                0
            ))::double precision AS value,
            rrp.tolerance,
            rrp.unit,
            rrp.recipe_revision_id
        FROM recipe_revision_param rrp
        JOIN active_recipes ar ON ar.id = rrp.recipe_revision_id
        LEFT JOIN runtime_adjustment ra
            ON ra.room_id = rrp.room_id
           AND ra.day_index = rrp.day_index
           AND ra.param_name = rrp.param_name
        GROUP BY
            rrp.room_id, rrp.day_index, rrp.param_name,
            rrp.value, rrp.tolerance, rrp.unit, rrp.recipe_revision_id
        WITH NO DATA
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX effective_target_pk_idx
            ON effective_target (room_id, day_index, param_name)
        """
    )

    # ----------------------------------------------------- command_batch
    op.execute(
        """
        CREATE TABLE command_batch (
            id serial PRIMARY KEY,
            room_id varchar(64) NOT NULL,
            purpose varchar(64) NOT NULL,
            idempotency_key varchar(128) NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX ix_command_batch_room_id ON command_batch (room_id)")

    # ----------------------------------------------------- command_queue
    op.execute(
        """
        CREATE TABLE command_queue (
            id serial PRIMARY KEY,
            batch_id int REFERENCES command_batch(id) ON DELETE SET NULL,
            domain varchar(64) NOT NULL,
            service varchar(64) NOT NULL,
            target_entity varchar(255) NOT NULL,
            service_data jsonb NOT NULL DEFAULT '{}',
            expected_value varchar(255),
            idempotency_key varchar(128) NOT NULL UNIQUE,
            status command_status NOT NULL DEFAULT 'pending',
            enqueued_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            completed_at timestamptz,
            attempts int NOT NULL DEFAULT 0,
            last_error varchar(2048),
            readback jsonb,
            enqueued_by varchar(64) NOT NULL,
            reason varchar(256)
        )
        """
    )
    for col in ("batch_id", "target_entity", "status", "enqueued_at"):
        op.execute(f"CREATE INDEX ix_command_queue_{col} ON command_queue ({col})")
    op.execute(
        "CREATE INDEX ix_command_queue_status_enqueued "
        "ON command_queue (status, enqueued_at)"
    )

    # ------------------------------------------------------- audit_event
    op.execute(
        """
        CREATE TABLE audit_event (
            id bigserial PRIMARY KEY,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            event_type audit_event_type NOT NULL,
            actor_id varchar(64),
            actor_role varchar(32),
            room_id varchar(64),
            params jsonb NOT NULL DEFAULT '{}',
            reason_codes jsonb NOT NULL DEFAULT '[]',
            summary varchar(1024),
            recipe_revision_id int,
            runtime_adjustment_id int,
            command_id int,
            snapshot_id int,
            llm_call_id int,
            key_id int NOT NULL,
            prev_event_hash bytea NOT NULL,
            hmac bytea NOT NULL
        )
        """
    )
    for col in ("occurred_at", "event_type", "actor_id", "room_id", "key_id"):
        op.execute(f"CREATE INDEX ix_audit_event_{col} ON audit_event ({col})")

    # HMAC chain trigger function
    op.execute(
        r"""
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
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_event_hmac_chain
            BEFORE INSERT ON audit_event
            FOR EACH ROW EXECUTE FUNCTION audit_event_hmac_chain()
        """
    )

    # Block UPDATE/DELETE on audit_event
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_event_block_modify() RETURNS trigger AS $func$
        BEGIN
            RAISE EXCEPTION
                'audit_event is append-only; UPDATE/DELETE forbidden (id=%)',
                COALESCE(OLD.id, -1);
        END;
        $func$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_event_no_update
            BEFORE UPDATE ON audit_event
            FOR EACH ROW EXECUTE FUNCTION audit_event_block_modify()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_event_no_delete
            BEFORE DELETE ON audit_event
            FOR EACH ROW EXECUTE FUNCTION audit_event_block_modify()
        """
    )

    # ----------------- recipe_revision immutability when 'approved'
    op.execute(
        """
        CREATE OR REPLACE FUNCTION recipe_revision_immutable_when_approved()
        RETURNS trigger AS $func$
        BEGIN
            IF OLD.status = 'approved' THEN
                IF NEW.status NOT IN ('approved', 'superseded') THEN
                    RAISE EXCEPTION
                        'recipe_revision id=% is approved; status can only move to superseded',
                        OLD.id;
                END IF;
                IF (
                    OLD.room_id, OLD.version, OLD.cycle_day_count, OLD.name, OLD.notes
                ) IS DISTINCT FROM (
                    NEW.room_id, NEW.version, NEW.cycle_day_count, NEW.name, NEW.notes
                ) THEN
                    RAISE EXCEPTION
                        'recipe_revision id=% is approved; mutable fields are frozen',
                        OLD.id;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $func$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_recipe_revision_immutable
            BEFORE UPDATE ON recipe_revision
            FOR EACH ROW EXECUTE FUNCTION recipe_revision_immutable_when_approved()
        """
    )

    # Block UPDATE/DELETE on recipe_revision_param when its parent is approved
    op.execute(
        """
        CREATE OR REPLACE FUNCTION recipe_revision_param_block_when_approved()
        RETURNS trigger AS $func$
        DECLARE
            parent_status recipe_status;
            ref_id int;
        BEGIN
            ref_id := COALESCE(NEW.recipe_revision_id, OLD.recipe_revision_id);
            SELECT status INTO parent_status FROM recipe_revision WHERE id = ref_id;
            IF parent_status = 'approved' THEN
                RAISE EXCEPTION
                    'recipe_revision_param of approved revision % is immutable',
                    ref_id;
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $func$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_recipe_revision_param_no_modify
            BEFORE UPDATE OR DELETE ON recipe_revision_param
            FOR EACH ROW EXECUTE FUNCTION recipe_revision_param_block_when_approved()
        """
    )

    # -------------------------------------------------------- daily_seal
    op.execute(
        """
        CREATE TABLE daily_seal (
            id serial PRIMARY KEY,
            seal_date date NOT NULL UNIQUE,
            first_event_id bigint NOT NULL,
            last_event_id bigint NOT NULL,
            last_event_hmac bytea NOT NULL,
            seal_hmac bytea NOT NULL,
            key_id int NOT NULL,
            sealed_at timestamptz NOT NULL DEFAULT now(),
            exported_at timestamptz,
            export_target varchar(512),
            export_locator varchar(512)
        )
        """
    )
    op.execute("CREATE INDEX ix_daily_seal_seal_date ON daily_seal (seal_date)")

    # --------------------------------------------------------- event_log
    op.execute(
        """
        CREATE TABLE event_log (
            id serial PRIMARY KEY,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            event_type audit_event_type NOT NULL,
            severity event_severity NOT NULL,
            room_id varchar(64),
            summary varchar(1024) NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}',
            reason_codes jsonb NOT NULL DEFAULT '[]',
            audit_event_id int,
            acknowledged_by varchar(64),
            acknowledged_at timestamptz
        )
        """
    )
    for col in ("occurred_at", "event_type", "severity", "room_id", "audit_event_id"):
        op.execute(f"CREATE INDEX ix_event_log_{col} ON event_log ({col})")
    op.execute(
        "CREATE INDEX ix_event_log_severity_occurred "
        "ON event_log (severity, occurred_at)"
    )

    # ---------------------------------------------------- pending_approval
    op.execute(
        """
        CREATE TABLE pending_approval (
            id serial PRIMARY KEY,
            room_id varchar(64) NOT NULL,
            proposal jsonb NOT NULL,
            snapshot_id int NOT NULL,
            llm_call_id int,
            summary varchar(1024) NOT NULL,
            status pending_status NOT NULL DEFAULT 'open',
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            decided_at timestamptz,
            decided_by varchar(64) REFERENCES users(id),
            decision_channel varchar(32),
            decision_chat_id varchar(64),
            decision_notes varchar(1024)
        )
        """
    )
    for col in ("room_id", "status", "expires_at"):
        op.execute(f"CREATE INDEX ix_pending_approval_{col} ON pending_approval ({col})")

    # --------------------------------------------------- telegram_user_map
    op.execute(
        """
        CREATE TABLE telegram_user_map (
            chat_id varchar(64) PRIMARY KEY,
            user_id varchar(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            label varchar(128),
            created_at timestamptz NOT NULL DEFAULT now(),
            created_by varchar(64) NOT NULL,
            last_verified_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_telegram_user_map_user_id "
        "ON telegram_user_map (user_id)"
    )

    # ---------------------------------------------------- cumulative_delta
    op.execute(
        """
        CREATE TABLE cumulative_delta (
            id serial PRIMARY KEY,
            room_id varchar(64) NOT NULL,
            param_name varchar(64) NOT NULL,
            sum_delta_24h double precision NOT NULL DEFAULT 0,
            sum_delta_7d double precision NOT NULL DEFAULT 0,
            last_updated_at timestamptz NOT NULL DEFAULT now(),
            cooldown_until timestamptz,
            cooldown_reason varchar(128),
            CONSTRAINT uq_cumulative_delta_room_param UNIQUE (room_id, param_name)
        )
        """
    )
    op.execute("CREATE INDEX ix_cumulative_delta_room_id ON cumulative_delta (room_id)")
    op.execute(
        "CREATE INDEX ix_cumulative_delta_param_name "
        "ON cumulative_delta (param_name)"
    )

    # ----------------------------------------------------- sensor_snapshot
    op.execute(
        """
        CREATE TABLE sensor_snapshot (
            id serial PRIMARY KEY,
            room_id varchar(64) NOT NULL,
            captured_at timestamptz NOT NULL DEFAULT now(),
            payload jsonb NOT NULL,
            recipe_revision_id int NOT NULL,
            cycle_day int NOT NULL,
            rollout_stage varchar(32) NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_sensor_snapshot_room_id ON sensor_snapshot (room_id)")
    op.execute(
        "CREATE INDEX ix_sensor_snapshot_captured_at ON sensor_snapshot (captured_at)"
    )

    # -------------------------------------------------------- llm_call_log
    op.execute(
        """
        CREATE TABLE llm_call_log (
            id serial PRIMARY KEY,
            called_at timestamptz NOT NULL DEFAULT now(),
            snapshot_id int,
            room_id varchar(64) NOT NULL,
            model varchar(128) NOT NULL,
            prompt jsonb NOT NULL,
            response_raw varchar(65536),
            response_parsed jsonb,
            outcome llm_call_outcome NOT NULL,
            validator_errors jsonb NOT NULL DEFAULT '[]',
            latency_ms int,
            prompt_tokens int,
            completion_tokens int,
            cost_usd double precision
        )
        """
    )
    for col in ("called_at", "snapshot_id", "room_id", "outcome"):
        op.execute(f"CREATE INDEX ix_llm_call_log_{col} ON llm_call_log ({col})")


def downgrade() -> None:
    # Drop in reverse dependency order
    for tbl in (
        "llm_call_log",
        "sensor_snapshot",
        "cumulative_delta",
        "telegram_user_map",
        "pending_approval",
        "event_log",
        "daily_seal",
    ):
        op.execute(f"DROP TABLE IF EXISTS {tbl}")

    op.execute("DROP TRIGGER IF EXISTS trg_audit_event_no_delete ON audit_event")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_event_no_update ON audit_event")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_event_hmac_chain ON audit_event")
    op.execute("DROP FUNCTION IF EXISTS audit_event_block_modify()")
    op.execute("DROP FUNCTION IF EXISTS audit_event_hmac_chain()")
    op.execute("DROP TABLE IF EXISTS audit_event")

    op.execute("DROP TABLE IF EXISTS command_queue")
    op.execute("DROP TABLE IF EXISTS command_batch")

    op.execute("DROP MATERIALIZED VIEW IF EXISTS effective_target")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_recipe_revision_param_no_modify "
        "ON recipe_revision_param"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS recipe_revision_param_block_when_approved()"
    )
    op.execute("DROP TABLE IF EXISTS recipe_revision_param")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_recipe_revision_immutable ON recipe_revision"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS recipe_revision_immutable_when_approved()"
    )
    op.execute("DROP TABLE IF EXISTS recipe_revision")

    op.execute("DROP TABLE IF EXISTS runtime_adjustment")
    op.execute("DROP TABLE IF EXISTS user_roles")
    op.execute("DROP TABLE IF EXISTS roles")
    op.execute("DROP TABLE IF EXISTS users")

    for typ in ENUM_TYPES:
        op.execute(f"DROP TYPE IF EXISTS {typ}")
