"""Unit tests for the sensors Pydantic schemas — Convex wire shape.

The Pydantic models in :mod:`app.schemas.sensors` are the contract
OCS exposes to AiGrowApp's integration layer: snake_case in Python,
but ``model_dump(by_alias=True)`` produces the camelCase + epoch-ms
JSON that's byte-compatible with a Convex document on the wire.
These tests pin that down so a future refactor can't silently break
the alignment.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.schemas.sensors import (
    SensorAlert,
    SensorCreate,
    SensorIntegrationCreate,
    SensorIntegrationRead,
    SensorRead,
    SensorReadingCreate,
    SensorReadingRead,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit


_T = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.UTC)
_T_MS = int(_T.timestamp() * 1000)


class TestSensorAlertShape:
    """The nested ``sensorAlert`` validator — camelCase round-trip."""

    def test_round_trip_camel_aliases(self) -> None:
        a = SensorAlert.model_validate(
            {
                "id": "alert-1",
                "name": "High temp",
                "enabled": True,
                "metric": "value",
                "condition": "above",
                "threshold": 30.0,
                "thresholdMax": None,
                "duration": 60,
                "severity": "warning",
                "notificationChannels": ["email", "sms"],
            }
        )
        assert a.threshold_max is None
        assert a.notification_channels == ["email", "sms"]
        wire = a.model_dump(by_alias=True)
        assert wire["thresholdMax"] is None
        assert wire["notificationChannels"] == ["email", "sms"]
        assert wire["condition"] == "above"
        assert wire["severity"] == "warning"

    def test_between_uses_threshold_max(self) -> None:
        a = SensorAlert.model_validate(
            {
                "id": "alert-2",
                "name": "VPD band",
                "enabled": True,
                "metric": "value",
                "condition": "between",
                "threshold": 0.8,
                "thresholdMax": 1.2,
                "severity": "info",
                "notificationChannels": ["push"],
            }
        )
        assert a.threshold == 0.8
        assert a.threshold_max == 1.2
        assert a.duration is None

    def test_invalid_condition_rejected(self) -> None:
        with pytest.raises(ValidationError, match="condition"):
            SensorAlert.model_validate(
                {
                    "id": "alert-3",
                    "name": "Bad",
                    "enabled": True,
                    "metric": "value",
                    "condition": "greater",  # not in the literal set
                    "threshold": 1.0,
                    "severity": "info",
                    "notificationChannels": [],
                }
            )

    def test_invalid_severity_rejected(self) -> None:
        with pytest.raises(ValidationError, match="severity"):
            SensorAlert.model_validate(
                {
                    "id": "alert-4",
                    "name": "Bad",
                    "enabled": True,
                    "metric": "value",
                    "condition": "above",
                    "threshold": 1.0,
                    "severity": "fatal",  # not in the literal set
                    "notificationChannels": [],
                }
            )


class TestSensorShape:
    """The ``sensors`` Convex shape — type literal, alerts JSONB, camelCase."""

    def _read_payload(self) -> dict[str, object]:
        return {
            "id": "sensor-1",
            "orgId": "open-crop-steering",
            "name": "F1 leaf temp",
            "code": "F1-LEAF-TEMP-1",
            "type": "temperature",
            "roomId": "room-1",
            "locationId": "loc-1",
            "batchId": "batch-1",
            "dataUnit": "°C",
            "status": "active",
            "isActive": True,
            "alerts": [
                {
                    "id": "alert-1",
                    "name": "High temp",
                    "enabled": True,
                    "metric": "value",
                    "condition": "above",
                    "threshold": 30.0,
                    "severity": "warning",
                    "notificationChannels": ["email"],
                }
            ],
            "tags": ["leaf", "F1"],
            "createdAt": _T,
            "updatedAt": _T,
        }

    def test_read_serialises_to_convex_wire_format(self) -> None:
        s = SensorRead.model_validate(self._read_payload())
        wire = s.model_dump(by_alias=True)
        assert wire["orgId"] == "open-crop-steering"
        assert wire["type"] == "temperature"
        assert wire["roomId"] == "room-1"
        assert wire["batchId"] == "batch-1"
        assert wire["dataUnit"] == "°C"
        assert wire["isActive"] is True
        assert wire["createdAt"] == _T_MS
        assert wire["updatedAt"] == _T_MS
        # alerts round-trip through SensorAlert child model.
        assert wire["alerts"][0]["notificationChannels"] == ["email"]
        assert wire["alerts"][0]["condition"] == "above"
        assert wire["tags"] == ["leaf", "F1"]

    def test_invalid_type_literal_rejected(self) -> None:
        payload = self._read_payload()
        payload["type"] = "thermometer"  # not in the Convex literal set
        with pytest.raises(ValidationError, match="type"):
            SensorRead.model_validate(payload)

    def test_invalid_status_literal_rejected(self) -> None:
        payload = self._read_payload()
        payload["status"] = "online"  # not in the Convex literal set
        with pytest.raises(ValidationError, match="status"):
            SensorRead.model_validate(payload)

    def test_create_accepts_snake_case_inputs(self) -> None:
        """``populate_by_name=True`` — Python-shape JSON loads cleanly."""
        c = SensorCreate.model_validate(
            {
                "name": "F1 leaf temp",
                "code": "F1-LEAF-TEMP-1",
                "type": "temperature",
                "data_unit": "°C",
                "status": "active",
                "is_active": True,
            }
        )
        assert c.data_unit == "°C"
        assert c.is_active is True
        # extra="forbid" — the create shape never accepts id/orgId.
        with pytest.raises(ValidationError, match="extra"):
            SensorCreate.model_validate(
                {
                    "name": "X",
                    "code": "X",
                    "type": "temperature",
                    "dataUnit": "°C",
                    "status": "active",
                    "isActive": True,
                    "id": "sensor-1",
                }
            )


class TestSensorReadingShape:
    """The ``sensorReadings`` Convex shape — camelCase wire, quality literal."""

    def test_create_camel_round_trip(self) -> None:
        r = SensorReadingCreate.model_validate(
            {
                "sensorId": "sensor-1",
                "timestamp": _T_MS,
                "value": 23.5,
                "unit": "°C",
                "roomId": "room-1",
                "batchId": "batch-1",
                "quality": "good",
                "isAnomaly": False,
                "hour": _T_MS - (_T_MS % 3_600_000),
                "day": _T_MS - (_T_MS % 86_400_000),
                "source": "integration",
            }
        )
        assert r.sensor_id == "sensor-1"
        assert r.is_anomaly is False
        wire = r.model_dump(by_alias=True)
        assert wire["sensorId"] == "sensor-1"
        assert wire["isAnomaly"] is False
        assert wire["quality"] == "good"
        assert wire["source"] == "integration"

    def test_read_serialises_timestamps_as_epoch_ms(self) -> None:
        r = SensorReadingRead.model_validate(
            {
                "id": "reading-1",
                "orgId": "open-crop-steering",
                "sensorId": "sensor-1",
                "timestamp": _T_MS,
                "value": 23.5,
                "unit": "°C",
                "hour": _T_MS - (_T_MS % 3_600_000),
                "day": _T_MS - (_T_MS % 86_400_000),
                "createdAt": _T,
                "updatedAt": _T,
            }
        )
        wire = r.model_dump(by_alias=True)
        assert wire["createdAt"] == _T_MS
        assert wire["updatedAt"] == _T_MS
        assert wire["timestamp"] == _T_MS

    def test_invalid_quality_rejected(self) -> None:
        with pytest.raises(ValidationError, match="quality"):
            SensorReadingCreate.model_validate(
                {
                    "sensorId": "sensor-1",
                    "timestamp": _T_MS,
                    "value": 23.5,
                    "unit": "°C",
                    "hour": 0,
                    "day": 0,
                    "quality": "okay",  # not in the literal set
                }
            )


class TestSensorIntegrationShape:
    """The ``sensorIntegrations`` Convex shape — credentials + status literal."""

    def test_create_credentials_shape(self) -> None:
        i = SensorIntegrationCreate.model_validate(
            {
                "name": "Home Assistant — F1",
                "type": "home_assistant",
                "connectionUrl": "http://homeassistant.local:8123",
                "apiKey": "secret",
                "mqttTopic": None,
                "status": "connected",
                "syncEnabled": True,
                "syncInterval": 30,
            }
        )
        assert i.connection_url == "http://homeassistant.local:8123"
        assert i.api_key == "secret"
        assert i.sync_enabled is True
        assert i.sync_interval == 30
        wire = i.model_dump(by_alias=True)
        assert wire["connectionUrl"] == "http://homeassistant.local:8123"
        assert wire["apiKey"] == "secret"
        assert wire["syncEnabled"] is True
        assert wire["syncInterval"] == 30
        assert wire["type"] == "home_assistant"

    def test_read_full_round_trip(self) -> None:
        i = SensorIntegrationRead.model_validate(
            {
                "id": "int-1",
                "orgId": "open-crop-steering",
                "name": "MQTT broker",
                "type": "mqtt",
                "mqttTopic": "sensors/#",
                "status": "connected",
                "lastConnected": _T_MS,
                "syncEnabled": True,
                "syncInterval": 60,
                "lastSync": _T_MS,
                "createdAt": _T,
                "updatedAt": _T,
            }
        )
        wire = i.model_dump(by_alias=True)
        assert wire["createdAt"] == _T_MS
        assert wire["lastConnected"] == _T_MS
        assert wire["lastSync"] == _T_MS
        assert wire["mqttTopic"] == "sensors/#"
        assert wire["type"] == "mqtt"

    def test_invalid_type_rejected(self) -> None:
        with pytest.raises(ValidationError, match="type"):
            SensorIntegrationCreate.model_validate(
                {
                    "name": "X",
                    "type": "rest",  # not in the literal set
                    "status": "connected",
                    "syncEnabled": True,
                    "syncInterval": 30,
                }
            )

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError, match="status"):
            SensorIntegrationCreate.model_validate(
                {
                    "name": "X",
                    "type": "mqtt",
                    "status": "online",  # not in the literal set
                    "syncEnabled": True,
                    "syncInterval": 30,
                }
            )
