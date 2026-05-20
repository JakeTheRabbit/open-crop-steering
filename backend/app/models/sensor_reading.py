"""SensorReading entity — Convex-mirror of AiGrowApp's ``sensorReadings``.

High-write time-series table: one row per reading per sensor.
Mirrored from ``stewnight/AiGrowApp:convex/schema/sensors.ts``. The
``hour`` and ``day`` columns are pre-bucketed timestamps to make
dashboard aggregations cheap — both have org-scoped indexes.

The Convex source uses ``v.id("batches")`` for ``batchId`` even though
``batches`` is created in a later pass of the schema-alignment work,
so this column is a bare string with no FK constraint until pass 4
lands.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SensorReading(Base):
    """A single time-series sample from a :class:`Sensor`.

    Attributes:
        id: Opaque string id (UUID4) — Convex doc-id style.
        org_id: Owning organisation; wire alias ``orgId``. Indexed for
            multi-tenant isolation.
        sensor_id: Parent sensor; FK with ``ON DELETE CASCADE`` — when
            a sensor is deleted its history goes with it.
        timestamp: Epoch milliseconds when the reading was taken.
        value: The reading itself.
        unit: Reading unit (mirrors :attr:`Sensor.data_unit` at sample
            time, in case it changes later).
        room_id: Optional denormalised room id (for query perf). FK
            with ``ON DELETE SET NULL``.
        batch_id: Optional batch id when the sensor was assigned to a
            batch. Bare string — ``batches`` table comes in pass 4.
        quality: Optional one of {good, fair, poor, error}.
        is_anomaly: Optional anomaly flag.
        hour: Reading timestamp truncated to the hour (epoch ms).
            Indexed.
        day: Reading timestamp truncated to the day (epoch ms).
            Indexed.
        source: Optional ingest source (``manual``, ``integration``,
            ``import``).
        notes: Optional free-text.
        created_at / updated_at: Audit timestamps.
    """

    __tablename__ = "sensor_readings"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)

    sensor_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sensors.id", ondelete="CASCADE"),
        nullable=False,
    )
    timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)

    room_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("rooms.id", ondelete="SET NULL"),
        nullable=True,
    )
    # deferred FK — references batches.id once pass 4 lands.
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Literal set: good, fair, poor, error
    quality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_anomaly: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    hour: Mapped[int] = mapped_column(BigInteger, nullable=False)
    day: Mapped[int] = mapped_column(BigInteger, nullable=False)

    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
        Index("ix_sensor_readings_org", "org_id"),
        Index("ix_sensor_readings_org_sensor", "org_id", "sensor_id"),
        Index(
            "ix_sensor_readings_org_sensor_time",
            "org_id",
            "sensor_id",
            "timestamp",
        ),
        Index(
            "ix_sensor_readings_org_room_time",
            "org_id",
            "room_id",
            "timestamp",
        ),
        Index(
            "ix_sensor_readings_org_batch_time",
            "org_id",
            "batch_id",
            "timestamp",
        ),
        Index("ix_sensor_readings_org_hour", "org_id", "hour"),
        Index("ix_sensor_readings_org_day", "org_id", "day"),
    )
