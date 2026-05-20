"""SensorIntegration entity — Convex-mirror of AiGrowApp's ``sensorIntegrations``.

Defines an external data source (Home Assistant, MQTT, Arduino, …)
that a :class:`~app.models.sensor.Sensor` can be tied to. Mirrored
from ``stewnight/AiGrowApp:convex/schema/sensors.ts``.

Credential fields (``api_key``, ``password``) are stored as plain
strings here for schema parity with Convex; production deployments
encrypt them at the application layer before write.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SensorIntegration(Base):
    """An external integration that feeds :class:`~app.models.sensor.Sensor` rows.

    Attributes:
        id: Opaque string id (UUID4) — Convex doc-id style.
        org_id: Owning organisation; wire alias ``orgId``. Indexed for
            multi-tenant isolation.
        name: Display name.
        type: One of {home_assistant, arduino, raspberry_pi, mqtt,
            http_webhook, modbus, custom}.
        connection_url / api_key / username / password / mqtt_topic:
            Connection config. Secrets are stored encrypted at the
            application layer in production.
        status: One of {connected, disconnected, error, configuring}.
        last_connected: Epoch ms of last successful connection.
        last_error: Last connection error message.
        sync_enabled: Whether the integration is allowed to ingest.
        sync_interval: Seconds between sync attempts.
        last_sync: Epoch ms of last sync.
        notes: Optional free-text.
        created_at / updated_at: Audit timestamps.
    """

    __tablename__ = "sensor_integrations"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # Literal set: home_assistant, arduino, raspberry_pi, mqtt,
    # http_webhook, modbus, custom
    type: Mapped[str] = mapped_column(String(32), nullable=False)

    connection_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    api_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    username: Mapped[str | None] = mapped_column(String(256), nullable=True)
    password: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    mqtt_topic: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Literal set: connected, disconnected, error, configuring
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_connected: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    sync_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_interval: Mapped[int] = mapped_column(Integer, nullable=False)
    last_sync: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    notes: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_sensor_integrations_org", "org_id"),
        Index("ix_sensor_integrations_org_type", "org_id", "type"),
        Index("ix_sensor_integrations_org_status", "org_id", "status"),
    )
