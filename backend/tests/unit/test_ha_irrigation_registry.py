"""Unit tests for :mod:`app.integrations.ha_irrigation.registry`.

Mirrors the ``test_sites_api.py`` fake-session pattern (in-memory
key-value store + Select walker) so the upsert logic exercises end-to-end
without Postgres / Docker. ``log_audit`` is monkeypatched to a no-op so
the audit trigger (real-Postgres only) is not exercised here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from app.api import sensors as sensors_api
from app.integrations.ha_irrigation import registry as registry_module
from app.models.building import Building
from app.models.equipment import Equipment
from app.models.location import Location
from app.models.room import Room
from app.models.sensor import Sensor
from app.models.sensor_integration import SensorIntegration

pytestmark = pytest.mark.unit


_FAKE_ORG = "open-crop-steering"


@dataclass
class _Settings:
    """Minimal stand-in matching :class:`registry.SettingsProtocol`."""

    ocs_org_id: str = _FAKE_ORG


# ---------------------------------------------------------------------------
# Fake session — same shape as the one in test_sites_api but with a smarter
# Select walker that honours simple equality filters on the queried model.
# ---------------------------------------------------------------------------


class _FakeScalars:
    """SQLAlchemy ScalarResult stand-in."""

    def __init__(self, rows: list[object]) -> None:
        self._rows = list(rows)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None


class _FakeResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = list(rows)

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(list(self._rows))


class _FakeSession:
    """In-memory async session — keyed on ``(type, id)``.

    The :meth:`execute` walker handles the equality-filter chain the
    registry module builds (``select(Model).where(col == val, ...)``).
    Each where clause is a SQLAlchemy ``BinaryExpression`` whose ``left``
    is a column and ``right`` is a bind-param literal — we read the
    column's key + the literal's value and apply them as Python equality
    checks against each row's attribute.
    """

    def __init__(self, prefilled: list[object] | None = None) -> None:
        self.store: dict[tuple[type, str], object] = {}
        for row in prefilled or []:
            self.store[(type(row), row.id)] = row  # type: ignore[attr-defined]
        self.flushes = 0
        self.commits = 0
        self.rollbacks = 0

    def add(self, obj: object) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = str(uuid.uuid4())  # type: ignore[attr-defined]
        self.store[(type(obj), obj.id)] = obj  # type: ignore[attr-defined]

    async def flush(self) -> None:
        self.flushes += 1

    async def refresh(self, obj: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def get(self, model: type, key: str) -> object | None:
        return self.store.get((model, key))

    async def delete(self, obj: object) -> None:
        self.store.pop((type(obj), obj.id), None)  # type: ignore[attr-defined]

    async def execute(self, stmt: Any) -> _FakeResult:
        try:
            entity = stmt.column_descriptions[0]["entity"]
        except (AttributeError, IndexError, KeyError, TypeError):
            return _FakeResult([])

        # Pull the entity-typed rows from the store, then apply the
        # where-clause equality filters (the only kind the registry
        # module uses).
        rows = [r for (t, _), r in self.store.items() if t is entity]
        clauses = list(getattr(stmt, "_where_criteria", ()))
        for clause in clauses:
            rows = [r for r in rows if _matches_binary_expr(r, clause)]
        return _FakeResult(rows)


def _matches_binary_expr(row: Any, expr: Any) -> bool:
    """Evaluate one SQLAlchemy BinaryExpression of the form ``col == value``."""
    try:
        col_name = expr.left.key
    except AttributeError:
        # Anything more complex than ``col == value`` is out of scope —
        # the registry module only uses simple equality filters, so we
        # bail loud rather than silently match-all.
        raise AssertionError(
            f"fake _matches_binary_expr cannot evaluate {expr!r}"
        ) from None
    expected = expr.right.value
    actual = getattr(row, col_name, None)
    return actual == expected


# ---------------------------------------------------------------------------
# Fixtures — autouse: stub log_audit, return a stable HA integration id
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _noop_log_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``log_audit`` so the real audit-row trigger never fires."""

    async def _stub(_session: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(registry_module, "log_audit", _stub)
    monkeypatch.setattr(sensors_api, "log_audit", _stub)


# ---------------------------------------------------------------------------
# Sample mapping payloads
# ---------------------------------------------------------------------------


def _mapping(
    *,
    building_id: str | None = None,
    zone_count: int = 3,
    include_room_level: bool = True,
) -> registry_module.MappingInput:
    """Build a representative MappingInput."""
    zones = [
        registry_module.ZoneInput(
            zone_index=n,
            location_label=f"Zone {n}",
            vwc_sensors=[f"sensor.crop_steering_vwc_zone_{n}"],
            ec_sensors=[f"sensor.crop_steering_ec_zone_{n}"],
            valves=[f"switch.crop_steering_zone_{n}_valve"],
        )
        for n in range(1, zone_count + 1)
    ]
    room_level = (
        registry_module.RoomLevelInput(
            pump="switch.crop_steering_pump",
            mainline_valve="switch.crop_steering_main_valve",
            steering_intent="number.crop_steering_steering_intent",
            ec_targets=[
                "number.crop_steering_ec_target_veg_p1",
                "number.crop_steering_ec_target_veg_p2",
            ],
            anomaly_binary_sensor="binary_sensor.crop_steering_anomaly_active",
            phase_select="select.crop_steering_irrigation_phase",
            rootsense_report_sensor=(
                "sensor.crop_steering_rootsense_report_latest"
            ),
        )
        if include_room_level
        else registry_module.RoomLevelInput()
    )
    return registry_module.MappingInput(
        room=registry_module.RoomInput(
            name="Crop Steering", building_id=building_id
        ),
        zones=zones,
        room_level=room_level,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRegisterMapping:
    """Behaviour of register_mapping — first run, idempotency, updates."""

    async def test_lazy_creates_facility_and_room(self) -> None:
        session = _FakeSession()
        result = await registry_module.register_mapping(
            session, mapping=_mapping(zone_count=1, include_room_level=False),
            settings=_Settings(),
        )
        # One building + one room landed.
        buildings = [
            r for (t, _), r in session.store.items() if t is Building
        ]
        rooms = [r for (t, _), r in session.store.items() if t is Room]
        assert len(buildings) == 1
        assert buildings[0].name == "Facility"
        assert buildings[0].org_id == _FAKE_ORG
        assert len(rooms) == 1
        assert rooms[0].name == "Crop Steering"
        assert rooms[0].building_id == buildings[0].id
        assert result.building_id == buildings[0].id
        assert result.room_id == rooms[0].id

    async def test_creates_one_location_per_zone_with_zone_code(self) -> None:
        session = _FakeSession()
        result = await registry_module.register_mapping(
            session, mapping=_mapping(zone_count=3), settings=_Settings(),
        )
        locations = [
            r for (t, _), r in session.store.items() if t is Location
        ]
        assert len(locations) == 3
        assert result.locations_created == 3
        codes = {loc.path for loc in locations}
        assert codes == {"ZONE_1", "ZONE_2", "ZONE_3"}
        labels = {loc.label for loc in locations}
        assert labels == {"Zone 1", "Zone 2", "Zone 3"}
        # Each location belongs to the room.
        room = next(r for (t, _), r in session.store.items() if t is Room)
        for loc in locations:
            assert loc.room_id == room.id
            assert loc.type == "zone"

    async def test_zone_sensors_created_with_correct_type_and_unit(self) -> None:
        session = _FakeSession()
        result = await registry_module.register_mapping(
            session, mapping=_mapping(zone_count=2, include_room_level=False),
            settings=_Settings(),
        )
        # 2 VWC + 2 EC sensors land for two zones, no room-level sensors.
        sensors = [r for (t, _), r in session.store.items() if t is Sensor]
        assert len(sensors) == 4
        assert result.sensors_created == 4
        by_external = {s.external_id: s for s in sensors}
        vwc = by_external["sensor.crop_steering_vwc_zone_1"]
        assert vwc.type == "moisture"
        assert vwc.data_unit == "%"
        assert vwc.code == "sensor.crop_steering_vwc_zone_1"
        ec = by_external["sensor.crop_steering_ec_zone_1"]
        assert ec.type == "ec"
        assert ec.data_unit == "mS/cm"
        # Every zone sensor sits in its zone's location.
        locations = [
            r for (t, _), r in session.store.items() if t is Location
        ]
        by_code = {loc.path: loc.id for loc in locations}
        assert vwc.location_id == by_code["ZONE_1"]
        assert ec.location_id == by_code["ZONE_1"]

    async def test_zone_valves_landed_as_irrigation_equipment(self) -> None:
        session = _FakeSession()
        await registry_module.register_mapping(
            session, mapping=_mapping(zone_count=2, include_room_level=False),
            settings=_Settings(),
        )
        eq = [r for (t, _), r in session.store.items() if t is Equipment]
        assert len(eq) == 2
        for e in eq:
            assert e.type == "irrigation"
            assert e.location_id is not None  # zone-scoped

    async def test_room_level_pump_and_mainline_no_location(self) -> None:
        session = _FakeSession()
        result = await registry_module.register_mapping(
            session, mapping=_mapping(zone_count=1, include_room_level=True),
            settings=_Settings(),
        )
        # Equipment: 1 zone valve + pump + mainline + steering_intent
        # + phase_select + 2 ec_targets = 7
        eq = [r for (t, _), r in session.store.items() if t is Equipment]
        assert len(eq) == 7
        assert result.equipment_created == 7
        by_external = {e.external_id: e for e in eq}
        pump = by_external["switch.crop_steering_pump"]
        assert pump.type == "irrigation"
        assert pump.location_id is None
        mainline = by_external["switch.crop_steering_main_valve"]
        assert mainline.type == "irrigation"
        assert mainline.location_id is None
        intent = by_external["number.crop_steering_steering_intent"]
        # Steering intent is a control target — lands under "other"
        # per integration spec §7 (the documented choice).
        assert intent.type == "other"
        assert intent.location_id is None
        phase_select = by_external["select.crop_steering_irrigation_phase"]
        assert phase_select.type == "other"
        ec_p1 = by_external["number.crop_steering_ec_target_veg_p1"]
        assert ec_p1.type == "other"

    async def test_room_level_anomaly_and_rootsense_sensors(self) -> None:
        session = _FakeSession()
        await registry_module.register_mapping(
            session, mapping=_mapping(zone_count=1, include_room_level=True),
            settings=_Settings(),
        )
        sensors = [r for (t, _), r in session.store.items() if t is Sensor]
        # 1 VWC + 1 EC (zone) + 2 room-level (anomaly + rootsense).
        assert len(sensors) == 4
        room_level = [s for s in sensors if s.location_id is None]
        assert len(room_level) == 2
        for s in room_level:
            assert s.type == "other"

    async def test_idempotent_second_run_no_new_rows(self) -> None:
        session = _FakeSession()
        mapping = _mapping(zone_count=3, include_room_level=True)
        first = await registry_module.register_mapping(
            session, mapping=mapping, settings=_Settings(),
        )
        sensors_after_first = len(
            [r for (t, _), r in session.store.items() if t is Sensor]
        )
        equipment_after_first = len(
            [r for (t, _), r in session.store.items() if t is Equipment]
        )
        locations_after_first = len(
            [r for (t, _), r in session.store.items() if t is Location]
        )
        rooms_after_first = len(
            [r for (t, _), r in session.store.items() if t is Room]
        )
        buildings_after_first = len(
            [r for (t, _), r in session.store.items() if t is Building]
        )

        second = await registry_module.register_mapping(
            session, mapping=mapping, settings=_Settings(),
        )
        assert first.room_id == second.room_id
        assert first.building_id == second.building_id
        # No new rows landed.
        assert sensors_after_first == len(
            [r for (t, _), r in session.store.items() if t is Sensor]
        )
        assert equipment_after_first == len(
            [r for (t, _), r in session.store.items() if t is Equipment]
        )
        assert locations_after_first == len(
            [r for (t, _), r in session.store.items() if t is Location]
        )
        assert rooms_after_first == len(
            [r for (t, _), r in session.store.items() if t is Room]
        )
        assert buildings_after_first == len(
            [r for (t, _), r in session.store.items() if t is Building]
        )
        # And the second call reports zero created.
        assert second.sensors_created == 0
        assert second.equipment_created == 0
        assert second.locations_created == 0

    async def test_changing_pump_entity_updates_in_place(self) -> None:
        """Re-register with a different pump entity — old entry unchanged, new one added; no duplicate."""
        session = _FakeSession()
        await registry_module.register_mapping(
            session, mapping=_mapping(zone_count=1, include_room_level=True),
            settings=_Settings(),
        )
        eq_before = [r for (t, _), r in session.store.items() if t is Equipment]
        pump_eq = next(
            e for e in eq_before if e.external_id == "switch.crop_steering_pump"
        )
        # Capture the existing pump row id so we can confirm a *different*
        # entity creates a NEW row (the old one stays — we don't delete).
        original_pump_id = pump_eq.id

        # Now re-register with a different pump.
        changed = registry_module.MappingInput(
            room=registry_module.RoomInput(name="Crop Steering"),
            zones=[
                registry_module.ZoneInput(
                    zone_index=1,
                    location_label="Zone 1",
                    vwc_sensors=["sensor.crop_steering_vwc_zone_1"],
                    ec_sensors=["sensor.crop_steering_ec_zone_1"],
                    valves=["switch.crop_steering_zone_1_valve"],
                )
            ],
            room_level=registry_module.RoomLevelInput(
                pump="switch.different_pump",  # CHANGED
                mainline_valve="switch.crop_steering_main_valve",
                steering_intent="number.crop_steering_steering_intent",
                ec_targets=[
                    "number.crop_steering_ec_target_veg_p1",
                    "number.crop_steering_ec_target_veg_p2",
                ],
                anomaly_binary_sensor=(
                    "binary_sensor.crop_steering_anomaly_active"
                ),
                phase_select="select.crop_steering_irrigation_phase",
                rootsense_report_sensor=(
                    "sensor.crop_steering_rootsense_report_latest"
                ),
            ),
        )
        await registry_module.register_mapping(
            session, mapping=changed, settings=_Settings(),
        )
        eq_after = [r for (t, _), r in session.store.items() if t is Equipment]
        pumps = [e for e in eq_after if "pump" in e.external_id]
        # Two pump rows now exist (original + new) — the original is
        # left intact because pruning is out of scope for P1.
        assert len(pumps) == 2
        ids = {e.id for e in pumps}
        assert original_pump_id in ids

    async def test_zone_reassignment_updates_existing_sensor(self) -> None:
        """A second registration that moves a sensor to a different zone updates in place."""
        session = _FakeSession()
        # First registration — sensor lives in zone 1.
        await registry_module.register_mapping(
            session,
            mapping=registry_module.MappingInput(
                room=registry_module.RoomInput(name="Crop Steering"),
                zones=[
                    registry_module.ZoneInput(
                        zone_index=1,
                        location_label="Zone 1",
                        vwc_sensors=["sensor.crop_steering_vwc_zone_1"],
                    )
                ],
            ),
            settings=_Settings(),
        )
        sensors = [r for (t, _), r in session.store.items() if t is Sensor]
        assert len(sensors) == 1
        original_sensor_id = sensors[0].id
        zone_1_location_id = sensors[0].location_id

        # Re-register with that same external_id moved to zone 2.
        await registry_module.register_mapping(
            session,
            mapping=registry_module.MappingInput(
                room=registry_module.RoomInput(name="Crop Steering"),
                zones=[
                    registry_module.ZoneInput(
                        zone_index=2,
                        location_label="Zone 2",
                        vwc_sensors=["sensor.crop_steering_vwc_zone_1"],
                    )
                ],
            ),
            settings=_Settings(),
        )
        sensors = [r for (t, _), r in session.store.items() if t is Sensor]
        # Still only one sensor row — updated in place.
        assert len(sensors) == 1
        assert sensors[0].id == original_sensor_id
        assert sensors[0].location_id != zone_1_location_id
        # The new zone 2 location exists.
        locations = [
            r for (t, _), r in session.store.items() if t is Location
        ]
        zone_2 = next(loc for loc in locations if loc.path == "ZONE_2")
        assert sensors[0].location_id == zone_2.id

    async def test_provided_building_id_reused(self) -> None:
        """Caller-supplied building id wins — no Facility lazy-create."""
        pre_existing = Building(
            id="bld-existing", org_id=_FAKE_ORG, name="Flower Building"
        )
        session = _FakeSession(prefilled=[pre_existing])
        result = await registry_module.register_mapping(
            session,
            mapping=_mapping(building_id="bld-existing", zone_count=1),
            settings=_Settings(),
        )
        buildings = [
            r for (t, _), r in session.store.items() if t is Building
        ]
        assert len(buildings) == 1  # no Facility created
        assert buildings[0].id == "bld-existing"
        assert result.building_id == "bld-existing"

    async def test_unknown_building_id_raises_valueerror(self) -> None:
        session = _FakeSession()
        with pytest.raises(ValueError, match="not found in tenant"):
            await registry_module.register_mapping(
                session,
                mapping=_mapping(building_id="bld-missing"),
                settings=_Settings(),
            )

    async def test_lazy_creates_ha_integration_singleton(self) -> None:
        session = _FakeSession()
        await registry_module.register_mapping(
            session, mapping=_mapping(zone_count=1), settings=_Settings(),
        )
        integrations = [
            r for (t, _), r in session.store.items()
            if t is SensorIntegration
        ]
        assert len(integrations) == 1
        assert integrations[0].type == "home_assistant"
        # All sensors + equipment reference that integration.
        sensors = [r for (t, _), r in session.store.items() if t is Sensor]
        for s in sensors:
            assert s.integration_id == integrations[0].id
        eq = [r for (t, _), r in session.store.items() if t is Equipment]
        for e in eq:
            assert e.integration_id == integrations[0].id

    async def test_records_carry_id_by_external_id_map(self) -> None:
        session = _FakeSession()
        result = await registry_module.register_mapping(
            session, mapping=_mapping(zone_count=2, include_room_level=True),
            settings=_Settings(),
        )
        # records.locations: one per zone, with zone_index + location_id
        assert {r.zone_index for r in result.records.locations} == {1, 2}
        for r in result.records.locations:
            assert r.location_id  # non-empty
        # records.sensors: scoped correctly
        for r in result.records.sensors:
            if r.external_id == "sensor.crop_steering_vwc_zone_1":
                assert r.scope == "zone"
                assert r.type == "moisture"
            elif r.external_id.startswith("binary_sensor"):
                assert r.scope == "room"
                assert r.type == "other"
        # records.equipment: pump is room-scoped, zone valve is zone-scoped
        pump_record = next(
            r for r in result.records.equipment
            if r.external_id == "switch.crop_steering_pump"
        )
        assert pump_record.scope == "room"
        zone_valve_record = next(
            r for r in result.records.equipment
            if r.external_id == "switch.crop_steering_zone_1_valve"
        )
        assert zone_valve_record.scope == "zone"
