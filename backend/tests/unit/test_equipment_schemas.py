"""Unit tests for the equipment Pydantic schemas — Convex wire shape.

The Pydantic models in :mod:`app.schemas.equipment` are the contract
OCS exposes to AiGrowApp's integration layer for the ``equipment``
table: snake_case in Python, but ``model_dump(by_alias=True)`` produces
the camelCase + epoch-ms JSON that's byte-compatible with a Convex
document on the wire. These tests pin that down so a future refactor
can't silently break the alignment.

Coverage:
- camelCase round-trip on create and read
- type / status ``Literal`` validation rejects unknown values
- FK fields (``roomId`` / ``locationId``) accept either case
- timestamp fields (audit + maintenance + warranty) serialise as
  epoch milliseconds, with ``None`` passed through untouched
- ``extra="forbid"`` rejects extraneous keys on the create shape
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.schemas.equipment import EquipmentCreate, EquipmentRead
from pydantic import ValidationError

pytestmark = pytest.mark.unit


_T = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.UTC)
_T_MS = int(_T.timestamp() * 1000)


class TestEquipmentCreate:
    """The ``equipment`` create shape — camelCase aliases + Literal unions."""

    def test_camel_round_trip_with_full_payload(self) -> None:
        e = EquipmentCreate.model_validate(
            {
                "name": "Flower Room 1 HVAC",
                "code": "HVAC-F1-001",
                "type": "hvac",
                "manufacturer": "Acme",
                "model": "AC-5000",
                "serialNumber": "SN-12345",
                "purchaseDate": _T,
                "warrantyExpires": _T,
                "roomId": "room-1",
                "locationId": "loc-1",
                "status": "operational",
                "lastMaintenance": _T,
                "nextMaintenance": _T,
                "maintenanceInterval": 90,
                "maintenanceNotes": "Quarterly filter change",
                "notes": "Installed 2026-01",
                "tags": ["climate", "critical"],
                "isActive": True,
            }
        )
        # snake_case attribute access
        assert e.serial_number == "SN-12345"
        assert e.room_id == "room-1"
        assert e.location_id == "loc-1"
        assert e.maintenance_interval == 90
        assert e.is_active is True
        # camelCase wire output with epoch-ms timestamps
        wire = e.model_dump(by_alias=True)
        assert wire["serialNumber"] == "SN-12345"
        assert wire["roomId"] == "room-1"
        assert wire["locationId"] == "loc-1"
        assert wire["purchaseDate"] == _T_MS
        assert wire["warrantyExpires"] == _T_MS
        assert wire["lastMaintenance"] == _T_MS
        assert wire["nextMaintenance"] == _T_MS
        assert wire["isActive"] is True
        assert wire["tags"] == ["climate", "critical"]

    def test_accepts_snake_case_inputs(self) -> None:
        """``populate_by_name=True`` — Python-shape JSON loads cleanly."""
        e = EquipmentCreate.model_validate(
            {
                "name": "Light Bar 1",
                "code": "LED-001",
                "type": "lighting",
                "status": "operational",
                "room_id": "room-1",
                "location_id": "loc-1",
                "serial_number": "SN-1",
                "is_active": False,
            }
        )
        assert e.room_id == "room-1"
        assert e.location_id == "loc-1"
        assert e.serial_number == "SN-1"
        assert e.is_active is False

    def test_optional_timestamps_passed_through_as_none(self) -> None:
        """``None`` timestamps must not be coerced to ``0`` by the serializer."""
        e = EquipmentCreate(
            name="Pump 1",
            code="IRR-001",
            type="irrigation",
            status="operational",
        )
        wire = e.model_dump(by_alias=True)
        assert wire["purchaseDate"] is None
        assert wire["warrantyExpires"] is None
        assert wire["lastMaintenance"] is None
        assert wire["nextMaintenance"] is None
        assert wire["isActive"] is True  # default

    def test_rejects_unknown_type_literal(self) -> None:
        with pytest.raises(ValidationError, match="type"):
            EquipmentCreate.model_validate(
                {
                    "name": "Mystery",
                    "code": "X-1",
                    "type": "teleporter",
                    "status": "operational",
                }
            )

    def test_rejects_unknown_status_literal(self) -> None:
        with pytest.raises(ValidationError, match="status"):
            EquipmentCreate.model_validate(
                {
                    "name": "Mystery",
                    "code": "X-1",
                    "type": "other",
                    "status": "exploded",
                }
            )

    def test_create_payload_forbids_extra_keys(self) -> None:
        # extra="forbid" — the create shape never accepts id/orgId/extras.
        with pytest.raises(ValidationError, match="extra"):
            EquipmentCreate.model_validate(
                {
                    "name": "Bad",
                    "code": "X-1",
                    "type": "other",
                    "status": "operational",
                    "id": "eq-1",
                }
            )

    def test_accepts_all_seven_type_literals(self) -> None:
        for kind in (
            "hvac",
            "lighting",
            "irrigation",
            "extraction",
            "processing",
            "monitoring",
            "other",
        ):
            e = EquipmentCreate(
                name="x", code="x", type=kind, status="operational"
            )
            assert e.type == kind

    def test_accepts_all_four_status_literals(self) -> None:
        for state in ("operational", "maintenance", "repair", "retired"):
            e = EquipmentCreate(
                name="x", code="x", type="other", status=state
            )
            assert e.status == state


class TestEquipmentRead:
    """The ``equipment`` read shape — orgId + audit ms timestamps."""

    def test_read_serialises_to_convex_wire_format(self) -> None:
        e = EquipmentRead.model_validate(
            {
                "id": "eq-1",
                "orgId": "open-crop-steering",
                "name": "Flower Room 1 HVAC",
                "code": "HVAC-F1-001",
                "type": "hvac",
                "status": "operational",
                "roomId": "room-1",
                "locationId": "loc-1",
                "isActive": True,
                "createdAt": _T,
                "updatedAt": _T,
            }
        )
        wire = e.model_dump(by_alias=True)
        assert wire["id"] == "eq-1"
        assert wire["orgId"] == "open-crop-steering"
        assert wire["roomId"] == "room-1"
        assert wire["locationId"] == "loc-1"
        assert wire["type"] == "hvac"
        assert wire["status"] == "operational"
        assert wire["isActive"] is True
        assert wire["createdAt"] == _T_MS
        assert wire["updatedAt"] == _T_MS

    def test_read_accepts_snake_case_inputs(self) -> None:
        e = EquipmentRead.model_validate(
            {
                "id": "eq-1",
                "org_id": "open-crop-steering",
                "name": "Pump 1",
                "code": "IRR-001",
                "type": "irrigation",
                "status": "operational",
                "is_active": True,
                "created_at": _T,
                "updated_at": _T,
            }
        )
        assert e.org_id == "open-crop-steering"
        assert e.is_active is True

    def test_read_with_null_fk_fields(self) -> None:
        """Both ``roomId`` and ``locationId`` are optional."""
        e = EquipmentRead.model_validate(
            {
                "id": "eq-1",
                "orgId": "org-1",
                "name": "Unassigned",
                "code": "X-1",
                "type": "other",
                "status": "operational",
                "isActive": True,
                "createdAt": _T,
                "updatedAt": _T,
            }
        )
        assert e.room_id is None
        assert e.location_id is None
        wire = e.model_dump(by_alias=True)
        assert wire["roomId"] is None
        assert wire["locationId"] is None
