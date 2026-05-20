"""Sensor entity — Convex-mirror of AiGrowApp's ``sensors`` table.

Environmental and equipment sensors with alerts, calibration and
external-integration links — mirrored from
``stewnight/AiGrowApp:convex/schema/sensors.ts``. Column set, naming
and indexes match the Convex shape so a future sync layer between OCS
and AiGrowApp can match rows by ``id`` / ``org_id`` without renaming a
single field.

Wire-level alignment lives in :mod:`app.schemas.sensors` — the
Pydantic ``SensorRead`` model serialises ``org_id`` as ``orgId`` and
the ``datetime`` timestamps as Convex's epoch-millisecond integers, so
JSON emitted by OCS is byte-compatible with a Convex ``sensors`` doc.
The nested ``alerts`` column is stored as JSONB and shaped against the
``SensorAlert`` Pydantic model.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Sensor(Base):
    """A physical or virtual sensor producing time-series readings.

    Attributes:
        id: Opaque string id (UUID4) — Convex doc-id style.
        org_id: Owning organisation; wire alias ``orgId``. Indexed for
            multi-tenant isolation.
        name: Display name.
        code: Unique sensor code (per org, not enforced here).
        type: One of {temperature, humidity, co2, ph, ec, vpd, light,
            pressure, moisture, flow, level, motion, air_quality}.
        room_id: Optional parent room. FK with ``ON DELETE SET NULL``.
        location_id: Optional parent location. FK with
            ``ON DELETE SET NULL``.
        batch_id: Optional batch the sensor is currently assigned to.
            Bare string — the ``batches`` table is added in pass 4 of
            the schema-alignment work, so no FK constraint yet.
        manufacturer / model / serial_number: Technical metadata.
        data_unit: Display unit (``°C``, ``%``, ``ppm``, ``pH``, …).
        min_value / max_value / accuracy / resolution: Technical specs.
        integration_id: Optional FK to a :class:`SensorIntegration`.
            ``ON DELETE SET NULL`` — losing the integration row leaves
            the sensor record intact.
        external_id: Identifier in the integration's source system
            (Home Assistant entity_id, MQTT topic, etc.).
        poll_interval: Seconds between polls (when applicable).
        status: One of {active, inactive, maintenance, error,
            calibrating}.
        last_reading_time / last_reading_value: Cached last reading for
            cheap status reads.
        battery_level / signal_strength: For battery / wireless
            sensors.
        last_calibration / next_calibration / calibration_offset /
            calibration_notes: Calibration tracking.
        alerts: Optional list of :class:`~app.schemas.sensors.SensorAlert`
            JSON blobs (stored as JSONB).
        notes / tags: Free-text metadata; ``tags`` is JSONB.
        is_active: Soft-active flag (separate from ``status``).
        created_at / updated_at: Audit timestamps.
    """

    __tablename__ = "sensors"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    # Literal set: temperature, humidity, co2, ph, ec, vpd, light,
    # pressure, moisture, flow, level, motion, air_quality
    type: Mapped[str] = mapped_column(String(32), nullable=False)

    room_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("rooms.id", ondelete="SET NULL"),
        nullable=True,
    )
    location_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True,
    )
    # deferred FK — references batches.id once pass 4 lands.
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    manufacturer: Mapped[str | None] = mapped_column(String(256), nullable=True)
    model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    serial_number: Mapped[str | None] = mapped_column(String(256), nullable=True)
    data_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    min_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolution: Mapped[float | None] = mapped_column(Float, nullable=True)

    integration_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("sensor_integrations.id", ondelete="SET NULL"),
        nullable=True,
    )
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    poll_interval: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Literal set: active, inactive, maintenance, error, calibrating
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_reading_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_reading_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    battery_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_strength: Mapped[float | None] = mapped_column(Float, nullable=True)

    last_calibration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_calibration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    calibration_offset: Mapped[float | None] = mapped_column(Float, nullable=True)
    calibration_notes: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    alerts: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB, nullable=True)

    notes: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

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
        Index("ix_sensors_org", "org_id"),
        Index("ix_sensors_org_room", "org_id", "room_id"),
        Index("ix_sensors_org_type", "org_id", "type"),
        Index("ix_sensors_org_status", "org_id", "status"),
        Index("ix_sensors_org_active", "org_id", "is_active"),
    )
