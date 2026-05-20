"""Pydantic schemas for the equipment tier — Convex-mirror.

These shapes mirror the ``equipment`` table in
``stewnight/AiGrowApp:convex/schema/assets.ts`` exactly. JSON output
(via ``model_dump(by_alias=True)``) uses camelCase to match Convex
documents on the wire; Python attribute access uses snake_case to match
the SQL ORM and the rest of the OCS codebase.

Both shapes are accepted on input (``populate_by_name=True``), so an
incoming JSON body can use either camelCase (AiGrowApp wire format) or
snake_case (OCS internal). Timestamp fields (audit + maintenance +
warranty + purchase date) serialise as epoch milliseconds — the same
wire format Convex uses for ``v.number()`` timestamp fields.

The ``type`` and ``status`` unions are pinned with :class:`typing.Literal`
so an invalid value fails at the Pydantic layer (matching what Convex's
``v.union(v.literal(...), ...)`` enforces server-side).
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

EquipmentType = Literal[
    "hvac",
    "lighting",
    "irrigation",
    "extraction",
    "processing",
    "monitoring",
    "other",
]

EquipmentStatus = Literal[
    "operational",
    "maintenance",
    "repair",
    "retired",
]


def _to_epoch_ms(value: dt.datetime) -> int:
    """Convert a timezone-aware datetime to epoch milliseconds.

    Convex's ``v.number()`` timestamp fields are epoch milliseconds —
    this is the canonical conversion used on every Convex-mirrored read
    schema in OCS, so the wire format matches Convex exactly.
    """
    return int(value.timestamp() * 1000)


class EquipmentBase(BaseModel):
    """Fields shared by the create and read shapes of an equipment row."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=128)
    type: EquipmentType

    manufacturer: str | None = Field(default=None, max_length=256)
    model: str | None = Field(default=None, max_length=256)
    serial_number: str | None = Field(
        default=None, max_length=256, alias="serialNumber"
    )
    purchase_date: dt.datetime | None = Field(default=None, alias="purchaseDate")
    warranty_expires: dt.datetime | None = Field(
        default=None, alias="warrantyExpires"
    )

    room_id: str | None = Field(default=None, alias="roomId")
    location_id: str | None = Field(default=None, alias="locationId")

    integration_id: str | None = Field(default=None, alias="integrationId")
    external_id: str | None = Field(
        default=None, max_length=256, alias="externalId"
    )

    status: EquipmentStatus

    last_maintenance: dt.datetime | None = Field(
        default=None, alias="lastMaintenance"
    )
    next_maintenance: dt.datetime | None = Field(
        default=None, alias="nextMaintenance"
    )
    maintenance_interval: int | None = Field(
        default=None, alias="maintenanceInterval"
    )
    maintenance_notes: str | None = Field(
        default=None, max_length=2048, alias="maintenanceNotes"
    )

    notes: str | None = Field(default=None, max_length=2048)
    tags: list[str] | None = None
    is_active: bool = Field(default=True, alias="isActive")

    @field_serializer(
        "purchase_date",
        "warranty_expires",
        "last_maintenance",
        "next_maintenance",
        when_used="unless-none",
    )
    def _ser_optional_timestamps(self, value: dt.datetime) -> int:
        return _to_epoch_ms(value)


class EquipmentCreate(EquipmentBase):
    """Payload for creating an :class:`~app.models.equipment.Equipment`.

    ``org_id`` is server-stamped from settings — clients never supply it.
    """


class EquipmentRead(EquipmentBase):
    """A persisted equipment row, wire-compatible with a Convex ``equipment`` doc."""

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
