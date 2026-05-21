"""HA-Irrigation-Strategy entity discovery — pure registry walk.

The HA-IS install creates a couple of hundred ``crop_steering_*``
entities at config-flow time. The OCS entity-mapper UI needs to know
which of those entities exist, how many irrigation zones the operator
configured, and which entity-id maps to which role (per-zone VWC / EC /
valve; room-level pump / mainline / steering intent / EC targets / phase
select / anomaly binary sensor / RootSense report sensor).

The work is split:

* :func:`discover` — the orchestrator. Pulls the HA registry + current
  states via the provided client, runs the grouping logic, attaches
  warnings, and returns a structured :class:`DiscoveryResult`.
* :func:`group_entities` — the pure-data slice. Takes the raw HA payload
  (a list of entity-registry rows + a list of state rows) and returns
  the same result. Unit tests target this so no live HA is needed.

Both halves are read-only. Discovery never touches the HA write surface,
never touches the OCS database, and is safe to run repeatedly — calling
it twice with the same HA payload returns byte-identical results.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Constants — the entity-id prefixes / patterns we recognise as HA-IS
# ---------------------------------------------------------------------------


#: Domain prefixes used by every HA-IS entity. ``crop_steering_*`` slugs
#: live under these five domains.
CROP_STEERING_DOMAINS: tuple[str, ...] = (
    "sensor",
    "switch",
    "number",
    "binary_sensor",
    "select",
)

#: The HA-IS slug prefix every native entity carries.
CROP_STEERING_PREFIX = "crop_steering_"

#: Raw front/back sensor patterns HA-IS publishes alongside the averaged
#: ``sensor.crop_steering_vwc_zone_{N}`` entities. The integration sometimes
#: runs both the averaged crop_steering entity AND the raw front/back pair,
#: so these are surfaced as candidate VWC/EC sensors too.
_RAW_VWC_PATTERN = re.compile(r"^sensor\.vwc_zone_(\d+)_(front|back)$")
_RAW_EC_PATTERN = re.compile(r"^sensor\.ec_zone_(\d+)_(front|back)$")

#: Per-zone HA-IS entity patterns. Each capture group gives the zone index.
_CROP_VWC_PATTERN = re.compile(r"^sensor\.crop_steering_vwc_zone_(\d+)$")
_CROP_EC_PATTERN = re.compile(r"^sensor\.crop_steering_ec_zone_(\d+)$")
_CROP_VALVE_PATTERN = re.compile(r"^switch\.crop_steering_zone_(\d+)_valve$")
#: Any HA-IS entity whose slug embeds a ``_zone_{N}`` segment. Used to infer
#: zone count when none of the canonical-named entities are present (e.g. a
#: user-renamed install) — every match lifts the detected zone count.
_ANY_ZONE_INDEX_PATTERN = re.compile(r"_zone_(\d+)(?:[_\.]|$)")

#: Room-level entity ids — fixed strings the operator does not normally
#: rename, so a direct equality lookup is enough.
_ROOM_LEVEL: dict[str, str] = {
    "pump": "switch.crop_steering_pump",
    "mainlineValve": "switch.crop_steering_main_valve",
    "steeringIntent": "number.crop_steering_steering_intent",
    "anomalyBinarySensor": "binary_sensor.crop_steering_anomaly_active",
    "phaseSelect": "select.crop_steering_irrigation_phase",
    "rootsenseReportSensor": (
        "sensor.crop_steering_rootsense_report_latest"
    ),
}

#: EC-target ``number.crop_steering_*`` entities. Each maps one-to-one onto
#: the steered parameters defined in
#: ``docs/concepts/ha-irrigation-strategy-integration.md`` §7.
_EC_TARGET_ENTITY_IDS: tuple[str, ...] = (
    "number.crop_steering_ec_target_veg_p0",
    "number.crop_steering_ec_target_veg_p1",
    "number.crop_steering_ec_target_veg_p2",
    "number.crop_steering_ec_target_veg_p3",
    "number.crop_steering_ec_target_gen_p0",
    "number.crop_steering_ec_target_gen_p1",
    "number.crop_steering_ec_target_gen_p2",
    "number.crop_steering_ec_target_gen_p3",
    "number.crop_steering_ec_target_flush",
)


#: The synthetic external id of the suggested room. Used as
#: ``Room.purpose`` so a second discover->register cycle can detect that
#: HA-IS already has a room here and refuse to duplicate it.
HA_IRRIGATION_EXTERNAL_ID = "ha_irrigation_strategy"

#: The default suggested room name.
SUGGESTED_ROOM_NAME = "Crop Steering"


# ---------------------------------------------------------------------------
# Result dataclasses (mirrored 1:1 by the Pydantic schemas in the API layer)
# ---------------------------------------------------------------------------


@dataclass
class CandidateEntities:
    """The candidate entities for one zone, in role-buckets.

    The frontend uses each list as the default selection for a
    multi-entity-picker the operator can edit before saving.
    """

    vwc_sensors: list[str] = field(default_factory=list)
    ec_sensors: list[str] = field(default_factory=list)
    valves: list[str] = field(default_factory=list)


@dataclass
class ZoneCandidate:
    """One detected irrigation zone with its candidate entities."""

    zone_index: int
    suggested_location_label: str
    candidate_entities: CandidateEntities


@dataclass
class RoomLevelCandidates:
    """Room-level (non-zone-scoped) candidate entities.

    Each role is a list because the frontend lets the operator pick zero
    or more — even where exactly one entity is expected, the contract is
    "here are the entities we saw with this role".
    """

    pump: list[str] = field(default_factory=list)
    mainline_valve: list[str] = field(default_factory=list)
    steering_intent: list[str] = field(default_factory=list)
    ec_targets: list[str] = field(default_factory=list)
    anomaly_binary_sensor: list[str] = field(default_factory=list)
    phase_select: list[str] = field(default_factory=list)
    rootsense_report_sensor: list[str] = field(default_factory=list)


@dataclass
class SuggestedRoom:
    """The suggested room name + external system id."""

    name: str
    external_system_id: str


@dataclass
class DiscoveryResult:
    """The complete discovery output. Mirrored by the API response schema."""

    suggested_room: SuggestedRoom
    detected_zone_count: int
    zones: list[ZoneCandidate]
    room_level_candidates: RoomLevelCandidates
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HA client protocol — what discover() needs from any HA client
# ---------------------------------------------------------------------------


class HAClientProtocol(Protocol):
    """The subset of :class:`app.ha_client.HAClient` discovery uses.

    Defined as a :class:`typing.Protocol` so unit tests can pass a fake
    object without subclassing the real client. The real client
    satisfies this structurally.
    """

    async def list_registry(self) -> dict[str, list[dict[str, Any]]]:
        ...

    async def get_states(self) -> list[dict[str, Any]]:
        ...


# ---------------------------------------------------------------------------
# Pure grouping (unit-testable without a live HA)
# ---------------------------------------------------------------------------


def _is_crop_steering_entity(entity_id: str) -> bool:
    """Return True for HA-IS native entities or raw front/back sensors."""
    domain, _, slug = entity_id.partition(".")
    if domain in CROP_STEERING_DOMAINS and slug.startswith(CROP_STEERING_PREFIX):
        return True
    return bool(
        _RAW_VWC_PATTERN.match(entity_id)
        or _RAW_EC_PATTERN.match(entity_id)
    )


def _ensure_zone(
    zones: dict[int, ZoneCandidate], zone_index: int
) -> ZoneCandidate:
    """Return the :class:`ZoneCandidate` for *zone_index*, creating if absent."""
    if zone_index not in zones:
        zones[zone_index] = ZoneCandidate(
            zone_index=zone_index,
            suggested_location_label=f"Zone {zone_index}",
            candidate_entities=CandidateEntities(),
        )
    return zones[zone_index]


def _try_match_zone_entity(
    eid: str,
    zones: dict[int, ZoneCandidate],
    detected_zone_indices: set[int],
) -> bool:
    """Dispatch one entity into a per-zone bucket.

    Returns ``True`` if the entity matched one of the canonical per-zone
    patterns (VWC / EC / valve, native or raw front-back). The caller
    skips other dispatchers when this returns True.
    """
    # Map of pattern → bucket name on :class:`CandidateEntities`.
    candidates: tuple[tuple[re.Pattern[str], str], ...] = (
        (_CROP_VWC_PATTERN, "vwc_sensors"),
        (_RAW_VWC_PATTERN, "vwc_sensors"),
        (_CROP_EC_PATTERN, "ec_sensors"),
        (_RAW_EC_PATTERN, "ec_sensors"),
        (_CROP_VALVE_PATTERN, "valves"),
    )
    for pattern, bucket in candidates:
        if (m := pattern.match(eid)) is not None:
            idx = int(m.group(1))
            detected_zone_indices.add(idx)
            zone = _ensure_zone(zones, idx)
            getattr(zone.candidate_entities, bucket).append(eid)
            return True
    return False


def _try_match_room_level(eid: str, room: RoomLevelCandidates) -> bool:
    """Dispatch one entity into the room-level bucket map.

    Returns ``True`` if the entity matched one of the room-level fixed
    string entity-ids or one of the EC-target entities.
    """
    role_to_bucket: tuple[tuple[str, str], ...] = (
        ("pump", "pump"),
        ("mainlineValve", "mainline_valve"),
        ("steeringIntent", "steering_intent"),
        ("anomalyBinarySensor", "anomaly_binary_sensor"),
        ("phaseSelect", "phase_select"),
        ("rootsenseReportSensor", "rootsense_report_sensor"),
    )
    for key, bucket in role_to_bucket:
        if eid == _ROOM_LEVEL[key]:
            getattr(room, bucket).append(eid)
            return True
    if eid in _EC_TARGET_ENTITY_IDS:
        room.ec_targets.append(eid)
        return True
    return False


def group_entities(entity_ids: list[str]) -> DiscoveryResult:
    """Group raw HA entity-ids into the discovery result.

    Pure data slice — no HA client, no DB. Given a flat list of entity
    ids (any subset of the HA registry), returns the result the API
    will emit. Idempotent: calling twice with the same input returns
    equal output.

    Args:
        entity_ids: Every entity id known to HA. Non-crop-steering
            entries are ignored.

    Returns:
        A :class:`DiscoveryResult` with zones sorted by index and the
        warnings list populated for any detected oddity (missing zone
        valve, missing RootSense report sensor, etc.).
    """
    # De-dup + filter to the HA-IS surface, preserving caller's order so
    # candidate lists stay stable across calls with the same payload.
    seen: set[str] = set()
    filtered: list[str] = []
    for eid in entity_ids:
        if eid in seen or not _is_crop_steering_entity(eid):
            continue
        seen.add(eid)
        filtered.append(eid)

    zones: dict[int, ZoneCandidate] = {}
    room = RoomLevelCandidates()
    detected_zone_indices: set[int] = set()

    for eid in filtered:
        if _try_match_zone_entity(eid, zones, detected_zone_indices):
            continue
        if _try_match_room_level(eid, room):
            continue
        # Generic zone-suffix fallback — lifts the detected zone count
        # when none of the canonical patterns matched (e.g. a renamed
        # install with only ``..._zone_4_*`` diagnostic entities).
        if (m := _ANY_ZONE_INDEX_PATTERN.search(eid)) is not None:
            with contextlib.suppress(ValueError):
                detected_zone_indices.add(int(m.group(1)))

    # Materialise the empty zones any generic-suffix fallback created,
    # so detected_zone_count matches len(zones) and the frontend always
    # gets a slot per detected zone (even one with no candidates yet).
    for idx in detected_zone_indices:
        _ensure_zone(zones, idx)

    # Sort EC targets so output order is stable regardless of HA
    # registry order. Other lists keep registry order because the
    # frontend wants them in "what HA gave us" order.
    room.ec_targets = sorted(set(room.ec_targets))

    sorted_zones = [zones[idx] for idx in sorted(zones)]
    warnings = _build_warnings(sorted_zones, room)
    return DiscoveryResult(
        suggested_room=SuggestedRoom(
            name=SUGGESTED_ROOM_NAME,
            external_system_id=HA_IRRIGATION_EXTERNAL_ID,
        ),
        detected_zone_count=len(sorted_zones),
        zones=sorted_zones,
        room_level_candidates=room,
        warnings=warnings,
    )


def _build_warnings(
    zones: list[ZoneCandidate], room: RoomLevelCandidates
) -> list[str]:
    """Build the warnings list — one human-readable string per oddity."""
    warnings: list[str] = []
    for zone in zones:
        if not zone.candidate_entities.valves:
            warnings.append(
                f"Zone {zone.zone_index} has no valve entity detected — "
                "confirm hardware mapping before saving."
            )
        if not zone.candidate_entities.vwc_sensors:
            warnings.append(
                f"Zone {zone.zone_index} has no VWC sensor detected — "
                "confirm hardware mapping before saving."
            )
        if not zone.candidate_entities.ec_sensors:
            warnings.append(
                f"Zone {zone.zone_index} has no EC sensor detected — "
                "confirm hardware mapping before saving."
            )
    if not room.rootsense_report_sensor:
        warnings.append(
            "RootSense report sensor not found "
            "(sensor.crop_steering_rootsense_report_latest) — enable "
            "switch.crop_steering_intelligence_llm_report_enabled in HA "
            "so OCS can consume the substrate snapshot."
        )
    if not room.steering_intent:
        warnings.append(
            "Steering-intent number entity not found "
            "(number.crop_steering_steering_intent) — OCS cannot steer "
            "without it."
        )
    if not room.pump:
        warnings.append(
            "Irrigation pump switch not found "
            "(switch.crop_steering_pump) — confirm the HA-IS install is "
            "configured before continuing."
        )
    return warnings


# ---------------------------------------------------------------------------
# Orchestrator — pulls the live HA payload and runs the grouping logic
# ---------------------------------------------------------------------------


async def discover(ha_client: HAClientProtocol) -> DiscoveryResult:
    """Walk HA's entity registry and group crop_steering entities by role.

    Two HA reads run sequentially (the WebSocket registry + the REST
    states list). Both are read-only. ``get_states`` is consulted so the
    grouping logic sees any ``crop_steering_*`` entity HA knows about,
    even those without a registry row (template sensors, integrations
    that publish state but skip registry registration).

    Args:
        ha_client: Any object that implements :class:`HAClientProtocol` —
            in production this is :class:`app.ha_client.HAClient`; in
            tests it's a fake returning canned payloads.

    Returns:
        A :class:`DiscoveryResult`. Idempotent: a second call against
        the same HA payload returns equal output.
    """
    registry = await ha_client.list_registry()
    states = await ha_client.get_states()

    entity_ids: list[str] = []
    seen: set[str] = set()
    for row in registry.get("entities", []):
        eid = row.get("entity_id")
        if isinstance(eid, str) and eid not in seen:
            entity_ids.append(eid)
            seen.add(eid)
    for row in states:
        eid = row.get("entity_id")
        if isinstance(eid, str) and eid not in seen:
            entity_ids.append(eid)
            seen.add(eid)

    return group_entities(entity_ids)
