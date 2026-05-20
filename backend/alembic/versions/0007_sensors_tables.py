"""sensors tier — sensors / sensor_readings / sensor_integrations (Convex-aligned)

Revision ID: 0007_sensors_tables
Revises: 0006_sites_tables
Create Date: 2026-05-20

Second pass of the AiGrowApp schema alignment. Mirrors the three
tables defined in ``stewnight/AiGrowApp:convex/schema/sensors.ts``
(``sensors``, ``sensorReadings``, ``sensorIntegrations``). Every row
carries ``org_id`` for multi-tenant isolation and a Convex-style
opaque string primary key.

Foreign keys:

* ``sensors.room_id`` / ``sensors.location_id`` — references the rooms
  and locations tables added in pass 1; ``ON DELETE SET NULL`` so a
  sensor row outlives the spot it was mounted in.
* ``sensors.integration_id`` — references the ``sensor_integrations``
  table in this same migration; ``ON DELETE SET NULL``.
* ``sensor_readings.sensor_id`` — ``ON DELETE CASCADE`` so deleting a
  sensor drops its history with it.
* ``sensor_readings.room_id`` — ``ON DELETE SET NULL``.
* ``sensors.batch_id`` / ``sensor_readings.batch_id`` — bare string
  columns (no FK constraint) until the ``batches`` table lands in a
  later pass.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_sensors_tables"
down_revision: str | Sequence[str] | None = "0006_sites_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- sensor_integrations -----------------------------------------
    # Created first so sensors.integration_id can FK to it.
    op.execute(
        """
        CREATE TABLE sensor_integrations (
            id              varchar(36)  PRIMARY KEY,
            org_id          varchar(64)  NOT NULL,
            name            varchar(256) NOT NULL,
            type            varchar(32)  NOT NULL,
            connection_url  varchar(1024),
            api_key         varchar(1024),
            username        varchar(256),
            password        varchar(1024),
            mqtt_topic      varchar(512),
            status          varchar(32)  NOT NULL,
            last_connected  bigint,
            last_error      varchar(2048),
            sync_enabled    boolean      NOT NULL DEFAULT true,
            sync_interval   integer      NOT NULL,
            last_sync       bigint,
            notes           varchar(2048),
            created_at      timestamptz  NOT NULL DEFAULT now(),
            updated_at      timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_sensor_integrations_org ON sensor_integrations (org_id)"
    )
    op.execute(
        "CREATE INDEX ix_sensor_integrations_org_type "
        "ON sensor_integrations (org_id, type)"
    )
    op.execute(
        "CREATE INDEX ix_sensor_integrations_org_status "
        "ON sensor_integrations (org_id, status)"
    )

    # --- sensors -----------------------------------------------------
    op.execute(
        """
        CREATE TABLE sensors (
            id                  varchar(36)  PRIMARY KEY,
            org_id              varchar(64)  NOT NULL,
            name                varchar(256) NOT NULL,
            code                varchar(128) NOT NULL,
            type                varchar(32)  NOT NULL,
            room_id             varchar(36)
                REFERENCES rooms(id) ON DELETE SET NULL,
            location_id         varchar(36)
                REFERENCES locations(id) ON DELETE SET NULL,
            batch_id            varchar(36),
            manufacturer        varchar(256),
            model               varchar(256),
            serial_number       varchar(256),
            data_unit           varchar(32)  NOT NULL,
            min_value           double precision,
            max_value           double precision,
            accuracy            double precision,
            resolution          double precision,
            integration_id      varchar(36)
                REFERENCES sensor_integrations(id) ON DELETE SET NULL,
            external_id         varchar(256),
            poll_interval       integer,
            status              varchar(32)  NOT NULL,
            last_reading_time   integer,
            last_reading_value  double precision,
            battery_level       double precision,
            signal_strength     double precision,
            last_calibration    integer,
            next_calibration    integer,
            calibration_offset  double precision,
            calibration_notes   varchar(1024),
            alerts              jsonb,
            notes               varchar(2048),
            tags                jsonb,
            is_active           boolean      NOT NULL DEFAULT true,
            created_at          timestamptz  NOT NULL DEFAULT now(),
            updated_at          timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_sensors_org ON sensors (org_id)")
    op.execute(
        "CREATE INDEX ix_sensors_org_room ON sensors (org_id, room_id)"
    )
    op.execute("CREATE INDEX ix_sensors_org_type ON sensors (org_id, type)")
    op.execute(
        "CREATE INDEX ix_sensors_org_status ON sensors (org_id, status)"
    )
    op.execute(
        "CREATE INDEX ix_sensors_org_active ON sensors (org_id, is_active)"
    )

    # --- sensor_readings ---------------------------------------------
    op.execute(
        """
        CREATE TABLE sensor_readings (
            id           varchar(36)  PRIMARY KEY,
            org_id       varchar(64)  NOT NULL,
            sensor_id    varchar(36)  NOT NULL
                REFERENCES sensors(id) ON DELETE CASCADE,
            timestamp    bigint       NOT NULL,
            value        double precision NOT NULL,
            unit         varchar(32)  NOT NULL,
            room_id      varchar(36)
                REFERENCES rooms(id) ON DELETE SET NULL,
            batch_id     varchar(36),
            quality      varchar(16),
            is_anomaly   boolean,
            hour         bigint       NOT NULL,
            day          bigint       NOT NULL,
            source       varchar(64),
            notes        varchar(2048),
            created_at   timestamptz  NOT NULL DEFAULT now(),
            updated_at   timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_sensor_readings_org ON sensor_readings (org_id)"
    )
    op.execute(
        "CREATE INDEX ix_sensor_readings_org_sensor "
        "ON sensor_readings (org_id, sensor_id)"
    )
    op.execute(
        "CREATE INDEX ix_sensor_readings_org_sensor_time "
        "ON sensor_readings (org_id, sensor_id, timestamp)"
    )
    op.execute(
        "CREATE INDEX ix_sensor_readings_org_room_time "
        "ON sensor_readings (org_id, room_id, timestamp)"
    )
    op.execute(
        "CREATE INDEX ix_sensor_readings_org_batch_time "
        "ON sensor_readings (org_id, batch_id, timestamp)"
    )
    op.execute(
        "CREATE INDEX ix_sensor_readings_org_hour "
        "ON sensor_readings (org_id, hour)"
    )
    op.execute(
        "CREATE INDEX ix_sensor_readings_org_day "
        "ON sensor_readings (org_id, day)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sensor_readings")
    op.execute("DROP TABLE IF EXISTS sensors")
    op.execute("DROP TABLE IF EXISTS sensor_integrations")
