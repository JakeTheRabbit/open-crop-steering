"""Pydantic schemas for the sites tier — Convex-mirror.

These shapes mirror ``stewnight/AiGrowApp:convex/schema/sites.ts``
exactly. JSON output (via ``model_dump(by_alias=True)``) uses camelCase
to match Convex documents on the wire; Python attribute access uses
snake_case to match the SQL ORM and the rest of the OCS codebase.

Both shapes are accepted on input (``populate_by_name=True``), so an
incoming JSON body can use either camelCase (AiGrowApp wire format) or
snake_case (OCS internal). Timestamps serialise as epoch
milliseconds — the same wire format Convex uses for ``v.number()``
timestamp fields.

A future OCS↔AiGrowApp sync layer therefore translates ``orgId`` ↔
``org_id`` and the datetime/epoch-ms timestamp purely via these
schemas — no per-field renaming on the sync hot path.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, field_serializer


def _to_epoch_ms(value: dt.datetime) -> int:
    """Convert a timezone-aware datetime to epoch milliseconds.

    Convex's ``v.number()`` timestamp fields are epoch milliseconds —
    this is the canonical conversion used on every Convex-mirrored read
    schema in OCS, so the wire format matches Convex exactly.
    """
    return int(value.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Buildings
# ---------------------------------------------------------------------------


class BuildingBase(BaseModel):
    """Fields shared by the create and read shapes of a building."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    address: str | None = Field(default=None, max_length=512)
    stories: list[str] | None = None
    width: float | None = None
    height: float | None = None
    length: float | None = None


class BuildingCreate(BuildingBase):
    """Payload for creating a :class:`~app.models.building.Building`.

    ``org_id`` is server-stamped from settings — clients never supply it.
    """


class BuildingRead(BuildingBase):
    """A persisted building, wire-compatible with Convex's ``buildings`` doc."""

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
# Rooms
# ---------------------------------------------------------------------------


class RoomBase(BaseModel):
    """Fields shared by the create and read shapes of a room."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    purpose: str | None = Field(default=None, max_length=256)
    story: str | None = Field(default=None, max_length=64)
    position_x: float | None = Field(default=None, alias="positionX")
    position_y: float | None = Field(default=None, alias="positionY")
    width: float | None = None
    height: float | None = None
    length: float | None = None
    area: float | None = None
    type: str | None = Field(default=None, max_length=64)


class RoomCreate(RoomBase):
    """Payload for creating a :class:`~app.models.room.Room`."""

    building_id: str = Field(alias="buildingId")


class RoomRead(RoomBase):
    """A persisted room, wire-compatible with Convex's ``rooms`` doc."""

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )

    id: str
    org_id: str = Field(alias="orgId")
    building_id: str = Field(alias="buildingId")
    created_at: dt.datetime = Field(alias="createdAt")
    updated_at: dt.datetime = Field(alias="updatedAt")

    @field_serializer("created_at", "updated_at")
    def _ser_timestamps(self, value: dt.datetime) -> int:
        return _to_epoch_ms(value)


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


class LocationBase(BaseModel):
    """Fields shared by the create and read shapes of a location."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    label: str = Field(min_length=1, max_length=256)
    path: str | None = Field(default=None, max_length=512)
    capacity: float | None = None
    story: str | None = Field(default=None, max_length=64)
    position_x: float | None = Field(default=None, alias="positionX")
    position_y: float | None = Field(default=None, alias="positionY")
    width: float | None = None
    height: float | None = None
    length: float | None = None
    area: float | None = None
    type: str | None = Field(default=None, max_length=64)


class LocationCreate(LocationBase):
    """Payload for creating a :class:`~app.models.location.Location`."""

    room_id: str = Field(alias="roomId")


class LocationRead(LocationBase):
    """A persisted location, wire-compatible with Convex's ``locations`` doc."""

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )

    id: str
    org_id: str = Field(alias="orgId")
    room_id: str = Field(alias="roomId")
    created_at: dt.datetime = Field(alias="createdAt")
    updated_at: dt.datetime = Field(alias="updatedAt")

    @field_serializer("created_at", "updated_at")
    def _ser_timestamps(self, value: dt.datetime) -> int:
        return _to_epoch_ms(value)
