"""Unit tests for the sites Pydantic schemas — Convex wire shape.

The Pydantic models in :mod:`app.schemas.sites` are the contract OCS
exposes to AiGrowApp's integration layer: snake_case in Python, but
``model_dump(by_alias=True)`` produces the camelCase + epoch-ms JSON
that's byte-compatible with a Convex document on the wire. These
tests pin that down so a future refactor can't silently break the
alignment.
"""

from __future__ import annotations

import datetime as dt

import pytest
from app.schemas.sites import (
    BuildingCreate,
    BuildingRead,
    LocationCreate,
    LocationRead,
    RoomCreate,
    RoomRead,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit


_T = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.UTC)
_T_MS = int(_T.timestamp() * 1000)


class TestBuildingShape:
    """The ``buildings`` Convex shape — orgId, stories, ms timestamps."""

    def test_read_serialises_to_convex_wire_format(self) -> None:
        b = BuildingRead.model_validate(
            {
                "id": "bld-1",
                "orgId": "open-crop-steering",
                "name": "Main",
                "address": "1 Test St",
                "stories": ["G", "1"],
                "width": 10.0,
                "height": 4.0,
                "length": 20.0,
                "createdAt": _T,
                "updatedAt": _T,
            }
        )
        wire = b.model_dump(by_alias=True)
        assert wire == {
            "id": "bld-1",
            "orgId": "open-crop-steering",
            "name": "Main",
            "address": "1 Test St",
            "stories": ["G", "1"],
            "width": 10.0,
            "height": 4.0,
            "length": 20.0,
            "createdAt": _T_MS,
            "updatedAt": _T_MS,
        }

    def test_create_payload_omits_id_and_org_id(self) -> None:
        c = BuildingCreate(name="Main", address="1 Test St")
        # extra="forbid" — the create shape never accepts id/orgId.
        with pytest.raises(ValidationError, match="extra"):
            BuildingCreate.model_validate({"name": "Main", "id": "bld-1"})
        assert c.name == "Main"

    def test_read_accepts_snake_case_inputs(self) -> None:
        """``populate_by_name=True`` — Python-shape JSON loads cleanly."""
        b = BuildingRead.model_validate(
            {
                "id": "bld-1",
                "org_id": "open-crop-steering",
                "name": "Main",
                "created_at": _T,
                "updated_at": _T,
            }
        )
        assert b.org_id == "open-crop-steering"


class TestRoomShape:
    """The ``rooms`` Convex shape — buildingId FK + positionX/Y."""

    def test_create_uses_camel_aliases_on_input_and_output(self) -> None:
        r = RoomCreate.model_validate(
            {
                "name": "Flower 1",
                "buildingId": "bld-1",
                "positionX": 1.5,
                "positionY": 2.5,
                "area": 12.0,
            }
        )
        assert r.building_id == "bld-1"
        assert r.position_x == 1.5
        wire = r.model_dump(by_alias=True)
        assert wire["buildingId"] == "bld-1"
        assert wire["positionX"] == 1.5
        assert wire["positionY"] == 2.5

    def test_read_full_round_trip(self) -> None:
        r = RoomRead.model_validate(
            {
                "id": "room-1",
                "orgId": "open-crop-steering",
                "buildingId": "bld-1",
                "name": "Flower 1",
                "purpose": "flowering",
                "positionX": 1.5,
                "positionY": 2.5,
                "width": 5.0,
                "length": 6.0,
                "area": 30.0,
                "type": "growing",
                "createdAt": _T,
                "updatedAt": _T,
            }
        )
        wire = r.model_dump(by_alias=True)
        assert wire["createdAt"] == _T_MS
        assert wire["buildingId"] == "bld-1"
        assert wire["positionX"] == 1.5


class TestLocationShape:
    """The ``locations`` Convex shape — roomId FK + capacity / path."""

    def test_create_camel_round_trip(self) -> None:
        loc = LocationCreate.model_validate(
            {
                "label": "Row A",
                "roomId": "room-1",
                "path": "R1/Row A",
                "capacity": 24,
                "positionX": 0.5,
                "positionY": 1.0,
            }
        )
        assert loc.room_id == "room-1"
        wire = loc.model_dump(by_alias=True)
        assert wire["roomId"] == "room-1"
        assert wire["positionX"] == 0.5
        assert wire["capacity"] == 24

    def test_read_serialises_timestamps_as_epoch_ms(self) -> None:
        loc = LocationRead.model_validate(
            {
                "id": "loc-1",
                "orgId": "open-crop-steering",
                "roomId": "room-1",
                "label": "Row A",
                "createdAt": _T,
                "updatedAt": _T,
            }
        )
        wire = loc.model_dump(by_alias=True)
        assert wire["createdAt"] == _T_MS
        assert wire["updatedAt"] == _T_MS
