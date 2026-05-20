"""Pydantic schemas for the sensors tier — Convex-mirror.

These shapes mirror ``stewnight/AiGrowApp:convex/schema/sensors.ts``
exactly. JSON output (via ``model_dump(by_alias=True)``) uses
camelCase to match Convex documents on the wire; Python attribute
access uses snake_case to match the SQL ORM and the rest of the OCS
codebase.

Both shapes are accepted on input (``populate_by_name=True``), so an
incoming JSON body can use either camelCase (AiGrowApp wire format)
or snake_case (OCS internal). Timestamps serialise as epoch
milliseconds — the same wire format Convex uses for ``v.number()``
timestamp fields.

The nested :class:`SensorAlert` validator is the Pydantic image of the
``sensorAlert`` Convex validator; it is what's stored inside a
:class:`Sensor`'s JSONB ``alerts`` column.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


def _to_epoch_ms(value: dt.datetime) -> int:
    """Convert a timezone-aware datetime to epoch milliseconds.

    Convex's ``v.number()`` timestamp fields are epoch milliseconds —
    this is the canonical conversion used on every Convex-mirrored
    read schema in OCS, so the wire format matches Convex exactly.
    """
    return int(value.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Sensor alert validator (nested inside Sensor.alerts)
# ---------------------------------------------------------------------------


class SensorAlert(BaseModel):
    """A single alert rule attached to a :class:`Sensor`.

    Mirrors the ``sensorAlert`` Convex validator one-for-one. Used as
    a list element of :attr:`SensorBase.alerts`, where it is persisted
    inside the ``sensors.alerts`` JSONB column.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str
    name: str
    enabled: bool
    metric: str
    condition: Literal["above", "below", "equals", "between", "outside"]
    threshold: float
    threshold_max: float | None = Field(default=None, alias="thresholdMax")
    duration: int | None = None
    severity: Literal["info", "warning", "critical"]
    notification_channels: list[str] = Field(alias="notificationChannels")


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------


SensorType = Literal[
    "temperature",
    "humidity",
    "co2",
    "ph",
    "ec",
    "vpd",
    "light",
    "pressure",
    "moisture",
    "flow",
    "level",
    "motion",
    "air_quality",
]

SensorStatus = Literal[
    "active", "inactive", "maintenance", "error", "calibrating"
]


class SensorBase(BaseModel):
    """Fields shared by the create and read shapes of a sensor."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=128)
    type: SensorType

    room_id: str | None = Field(default=None, alias="roomId")
    location_id: str | None = Field(default=None, alias="locationId")
    batch_id: str | None = Field(default=None, alias="batchId")

    manufacturer: str | None = Field(default=None, max_length=256)
    model: str | None = Field(default=None, max_length=256)
    serial_number: str | None = Field(
        default=None, max_length=256, alias="serialNumber"
    )
    data_unit: str = Field(min_length=1, max_length=32, alias="dataUnit")
    min_value: float | None = Field(default=None, alias="minValue")
    max_value: float | None = Field(default=None, alias="maxValue")
    accuracy: float | None = None
    resolution: float | None = None

    integration_id: str | None = Field(default=None, alias="integrationId")
    external_id: str | None = Field(
        default=None, max_length=256, alias="externalId"
    )
    poll_interval: int | None = Field(default=None, alias="pollInterval")

    status: SensorStatus
    last_reading_time: int | None = Field(default=None, alias="lastReadingTime")
    last_reading_value: float | None = Field(
        default=None, alias="lastReadingValue"
    )
    battery_level: float | None = Field(default=None, alias="batteryLevel")
    signal_strength: float | None = Field(default=None, alias="signalStrength")

    last_calibration: int | None = Field(default=None, alias="lastCalibration")
    next_calibration: int | None = Field(default=None, alias="nextCalibration")
    calibration_offset: float | None = Field(
        default=None, alias="calibrationOffset"
    )
    calibration_notes: str | None = Field(
        default=None, max_length=1024, alias="calibrationNotes"
    )

    alerts: list[SensorAlert] | None = None

    notes: str | None = Field(default=None, max_length=2048)
    tags: list[str] | None = None
    is_active: bool = Field(alias="isActive")


class SensorCreate(SensorBase):
    """Payload for creating a :class:`~app.models.sensor.Sensor`.

    ``org_id`` is server-stamped from settings — clients never supply
    it.
    """


class SensorRead(SensorBase):
    """A persisted sensor, wire-compatible with Convex's ``sensors`` doc."""

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )

    id: str
    org_id: str = Field(alias="orgId")
    created_at: dt.datetime = Field(alias="createdAt")
    updated_at: dt.datetime = Field(alias="updatedAt")

    @field_serializer("created_at", "updated_at")
    def _ser_timestamps(self, value: dt.datetime) -> int:
        return _to_epoch_ms(value)


# ---------------------------------------------------------------------------
# Sensor readings
# ---------------------------------------------------------------------------


SensorReadingQuality = Literal["good", "fair", "poor", "error"]


class SensorReadingBase(BaseModel):
    """Fields shared by the create and read shapes of a sensor reading."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    sensor_id: str = Field(alias="sensorId")
    timestamp: int
    value: float
    unit: str = Field(min_length=1, max_length=32)

    room_id: str | None = Field(default=None, alias="roomId")
    batch_id: str | None = Field(default=None, alias="batchId")

    quality: SensorReadingQuality | None = None
    is_anomaly: bool | None = Field(default=None, alias="isAnomaly")

    hour: int
    day: int

    source: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2048)


class SensorReadingCreate(SensorReadingBase):
    """Payload for creating a :class:`~app.models.sensor_reading.SensorReading`."""


class SensorReadingRead(SensorReadingBase):
    """A persisted reading, wire-compatible with Convex's ``sensorReadings`` doc."""

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )

    id: str
    org_id: str = Field(alias="orgId")
    created_at: dt.datetime = Field(alias="createdAt")
    updated_at: dt.datetime = Field(alias="updatedAt")

    @field_serializer("created_at", "updated_at")
    def _ser_timestamps(self, value: dt.datetime) -> int:
        return _to_epoch_ms(value)


# ---------------------------------------------------------------------------
# Sensor integrations
# ---------------------------------------------------------------------------


SensorIntegrationType = Literal[
    "home_assistant",
    "arduino",
    "raspberry_pi",
    "mqtt",
    "http_webhook",
    "modbus",
    "custom",
]

SensorIntegrationStatus = Literal[
    "connected", "disconnected", "error", "configuring"
]


class SensorIntegrationBase(BaseModel):
    """Fields shared by the create and read shapes of a sensor integration."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    type: SensorIntegrationType

    connection_url: str | None = Field(
        default=None, max_length=1024, alias="connectionUrl"
    )
    api_key: str | None = Field(default=None, max_length=1024, alias="apiKey")
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=1024)
    mqtt_topic: str | None = Field(
        default=None, max_length=512, alias="mqttTopic"
    )

    status: SensorIntegrationStatus
    last_connected: int | None = Field(default=None, alias="lastConnected")
    last_error: str | None = Field(
        default=None, max_length=2048, alias="lastError"
    )

    sync_enabled: bool = Field(alias="syncEnabled")
    sync_interval: int = Field(alias="syncInterval")
    last_sync: int | None = Field(default=None, alias="lastSync")

    notes: str | None = Field(default=None, max_length=2048)


class SensorIntegrationCreate(SensorIntegrationBase):
    """Payload for creating a :class:`~app.models.sensor_integration.SensorIntegration`."""


class SensorIntegrationRead(SensorIntegrationBase):
    """A persisted integration, wire-compatible with Convex's ``sensorIntegrations`` doc."""

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )

    id: str
    org_id: str = Field(alias="orgId")
    created_at: dt.datetime = Field(alias="createdAt")
    updated_at: dt.datetime = Field(alias="updatedAt")

    @field_serializer("created_at", "updated_at")
    def _ser_timestamps(self, value: dt.datetime) -> int:
        return _to_epoch_ms(value)
