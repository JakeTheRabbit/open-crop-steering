"""Room-configuration wizard validation API (Phase 11/12).

When an operator adds a room or edits its ``equipment_map``, the wizard
POSTs the proposed config here and this router validates it against the
equipment-coupling rules from ``cultivation_knowledge.md`` Section 6. The
validation has two tiers (the knowledge base's own distinction):

* **Hard refusals** — a structural gap that makes the config unsafe to
  *run at all*. The config cannot be saved. Example: a PPFD setpoint is
  enabled but no light entities are mapped, so the executor has nothing
  to drive.
* **Fail-soft warnings** — the config is runnable but has a known
  efficiency / risk cost the operator should see. The config saves with
  a warning banner. Example: a dehumidifier and an AC are both mapped
  but no reheat coil — they will fight on VPD (EC-004).

The split mirrors :class:`~app.core.coupling_rules.CouplingViolation`'s
``hard`` flag — the same knowledge base drives the Phase-9 proposal
validator and this Phase-11 config validator, so a room that the wizard
accepts will not surprise the supervisor later.

This endpoint is **pure validation**: it touches no database and has no
side effects. Persisting the config is a separate admin action; the
wizard calls this first and only offers "save" when there are no hard
refusals.

``coupling_notes`` are recorded back into the room's config so the AI
supervisor knows which hardware constraints apply (e.g. "no reheat ->
Class B proposals must respect the dehu-heat budget").
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/config-wizard", tags=["config-wizard"])


# ---------------------------------------------------------------------------
# Request / response models.
# ---------------------------------------------------------------------------


class ZoneConfig(BaseModel):
    """One irrigation zone's sensor + actuator map.

    Attributes:
        zone_id: Zone identifier.
        valve_entity: Per-zone irrigation valve entity, or ``None``.
        pump_entity: Per-zone (or shared) pump entity, or ``None``.
        vwc_sensor: Per-zone substrate VWC sensor entity, or ``None``.
        ec_sensor: Per-zone substrate EC sensor entity, or ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    zone_id: str = Field(min_length=1, max_length=64)
    valve_entity: str | None = Field(default=None, max_length=255)
    pump_entity: str | None = Field(default=None, max_length=255)
    vwc_sensor: str | None = Field(default=None, max_length=255)
    ec_sensor: str | None = Field(default=None, max_length=255)


class TankConfig(BaseModel):
    """One nutrient tank's sensor + doser map.

    Attributes:
        tank_id: Tank identifier.
        ph_sensor: Tank pH sensor entity, or ``None``.
        ec_sensor: Tank EC sensor entity, or ``None``.
        doser_entities: Doser-pump entities mapped for this tank.
    """

    model_config = ConfigDict(extra="forbid")

    tank_id: str = Field(min_length=1, max_length=64)
    ph_sensor: str | None = Field(default=None, max_length=255)
    ec_sensor: str | None = Field(default=None, max_length=255)
    doser_entities: list[str] = Field(default_factory=list)


class RoomEquipmentMap(BaseModel):
    """A proposed room equipment map for the wizard to validate.

    Every control surface has an explicit ``*_control_enabled`` flag —
    validation only fires for a coupling the operator has actually
    enabled. A room with only environmental control enabled is not
    refused for missing irrigation valves.

    Attributes:
        room_id: Room identifier.
        env_control_enabled: Environmental setpoint control (temp / RH /
            CO2 / VPD) is enabled.
        ppfd_control_enabled: A PPFD setpoint is enabled.
        irrigation_control_enabled: Per-zone irrigation control is
            enabled.
        tank_control_enabled: Tank pH/EC chemistry control is enabled.
        co2_control_enabled: A CO2 setpoint is enabled.
        temp_sensor: Room air-temperature sensor entity, or ``None``.
        leaf_temp_sensor: Leaf-temperature sensor entity, or ``None``.
        rh_sensor: Room relative-humidity sensor entity, or ``None``.
        co2_sensor: Room CO2 sensor entity, or ``None``.
        under_canopy_rh_probe: Under-canopy RH probe entity, or ``None``.
        cooling_capacity_entity: A cooling-headroom source (a
            ``climate.*`` or a fan-stage entity), or ``None``.
        light_entities: Grow-light entities — dimmable lights / circuits.
        ac_entities: Air-conditioner / climate entities.
        dehumidifier_entities: Dehumidifier entities.
        reheat_entities: Reheat-coil entities.
        exhaust_entities: Exhaust-fan entities.
        co2_solenoid_entities: CO2 injection solenoid entities.
        zones: Per-zone irrigation configs.
        tanks: Per-tank chemistry configs.
    """

    model_config = ConfigDict(extra="forbid")

    room_id: str = Field(min_length=1, max_length=64)

    env_control_enabled: bool = False
    ppfd_control_enabled: bool = False
    irrigation_control_enabled: bool = False
    tank_control_enabled: bool = False
    co2_control_enabled: bool = False

    # Sensors + the headroom source — one reference entity per room.
    temp_sensor: str | None = Field(default=None, max_length=255)
    leaf_temp_sensor: str | None = Field(default=None, max_length=255)
    rh_sensor: str | None = Field(default=None, max_length=255)
    co2_sensor: str | None = Field(default=None, max_length=255)
    under_canopy_rh_probe: str | None = Field(default=None, max_length=255)
    cooling_capacity_entity: str | None = Field(default=None, max_length=255)

    # Actuators — a room routinely has several of each (two AC units,
    # multiple grow-light circuits, etc.), and they are climate /
    # humidifier / dimmable-light entities, not bare on/off switches.
    # Each role is therefore a LIST of entity ids.
    light_entities: list[str] = Field(default_factory=list)
    ac_entities: list[str] = Field(default_factory=list)
    dehumidifier_entities: list[str] = Field(default_factory=list)
    reheat_entities: list[str] = Field(default_factory=list)
    exhaust_entities: list[str] = Field(default_factory=list)
    co2_solenoid_entities: list[str] = Field(default_factory=list)

    zones: list[ZoneConfig] = Field(default_factory=list)
    tanks: list[TankConfig] = Field(default_factory=list)


class WizardFinding(BaseModel):
    """One validation finding — a hard refusal or a fail-soft warning.

    Attributes:
        code: A stable machine code (e.g. ``"ppfd_without_lights"``
            or an ``EC-*`` id).
        message: Human-readable explanation for the wizard UI.
        hard: ``True`` for a hard refusal, ``False`` for a fail-soft
            warning.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    hard: bool


class WizardValidationResult(BaseModel):
    """The wizard validation response.

    Attributes:
        ok: ``True`` iff there are no hard refusals — the config may be
            saved (possibly with warnings).
        hard_refusals: Findings that block the save.
        warnings: Fail-soft findings — saved with a banner.
        coupling_notes: Hardware-constraint notes to record into the
            room config for the AI supervisor's durable context.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    hard_refusals: list[WizardFinding] = Field(default_factory=list)
    warnings: list[WizardFinding] = Field(default_factory=list)
    coupling_notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Validation logic.
# ---------------------------------------------------------------------------


def _check_irrigation_hard(cfg: RoomEquipmentMap) -> list[WizardFinding]:
    """Hard refusals for irrigation control (``cultivation_knowledge.md`` S6).

    Irrigation control needs a per-zone valve **and** pump on every zone,
    and a per-zone VWC **and** EC sensor (S6.1 / S6.4). A zone missing any
    of those is a hard refusal.
    """
    if not cfg.irrigation_control_enabled:
        return []
    findings: list[WizardFinding] = []
    if not cfg.zones:
        findings.append(
            WizardFinding(
                code="irrigation_without_zones",
                hard=True,
                message=(
                    "Irrigation control is enabled but no zones are "
                    "configured — at least one zone with a valve, pump, VWC "
                    "and EC sensor is required (S6.4)."
                ),
            )
        )
    for zone in cfg.zones:
        if not (zone.valve_entity and zone.pump_entity):
            findings.append(
                WizardFinding(
                    code="irrigation_zone_missing_valve_or_pump",
                    hard=True,
                    message=(
                        f"Zone {zone.zone_id!r}: irrigation control is "
                        "enabled but the zone is missing a valve and/or pump "
                        "entity — both are required per zone (S6.4)."
                    ),
                )
            )
        if not (zone.vwc_sensor and zone.ec_sensor):
            findings.append(
                WizardFinding(
                    code="irrigation_zone_missing_vwc_or_ec_sensor",
                    hard=True,
                    message=(
                        f"Zone {zone.zone_id!r}: irrigation control requires "
                        "per-zone VWC and EC sensors; one or both are missing "
                        "(S6.1)."
                    ),
                )
            )
    return findings


def _check_tank_hard(cfg: RoomEquipmentMap) -> list[WizardFinding]:
    """Hard refusals for tank chemistry control (``cultivation_knowledge.md`` S6).

    Tank pH/EC control needs at least one doser pump per tank and both a
    pH and an EC sensor per tank (S6.1 / S6.4).
    """
    if not cfg.tank_control_enabled:
        return []
    findings: list[WizardFinding] = []
    if not cfg.tanks:
        findings.append(
            WizardFinding(
                code="tank_control_without_tanks",
                hard=True,
                message=(
                    "Tank chemistry control is enabled but no tanks are "
                    "configured — each tank needs doser pumps and pH/EC "
                    "sensors (S6.4)."
                ),
            )
        )
    for tank in cfg.tanks:
        if not tank.doser_entities:
            findings.append(
                WizardFinding(
                    code="tank_without_doser_pumps",
                    hard=True,
                    message=(
                        f"Tank {tank.tank_id!r}: pH/EC control is enabled but "
                        "no doser pump entities are mapped — at least one is "
                        "required (S6.4)."
                    ),
                )
            )
        if not (tank.ph_sensor and tank.ec_sensor):
            findings.append(
                WizardFinding(
                    code="tank_missing_ph_or_ec_sensor",
                    hard=True,
                    message=(
                        f"Tank {tank.tank_id!r}: tank chemistry control "
                        "requires both a pH and an EC sensor; one or both are "
                        "missing (S6.1)."
                    ),
                )
            )
    return findings


def _check_env_co2_hard(cfg: RoomEquipmentMap) -> list[WizardFinding]:
    """Hard refusals for environmental + CO2 control (``cultivation_knowledge.md`` S6).

    Environmental control needs the room temp / leaf-temp / RH / CO2
    sensor minimum (S6.1); CO2 setpoint control needs a CO2 sensor for
    readback and a CO2 solenoid to drive (S6.2).
    """
    findings: list[WizardFinding] = []
    if cfg.env_control_enabled:
        missing = [
            label
            for label, present in (
                ("temp", bool(cfg.temp_sensor)),
                ("leaf_temp", bool(cfg.leaf_temp_sensor)),
                ("RH", bool(cfg.rh_sensor)),
                ("CO2", bool(cfg.co2_sensor)),
            )
            if not present
        ]
        if missing:
            findings.append(
                WizardFinding(
                    code="env_control_missing_sensors",
                    hard=True,
                    message=(
                        "Environmental control is enabled but the room is "
                        f"missing required sensor(s): {', '.join(missing)}. "
                        "Per-room temp, leaf_temp, RH and CO2 sensors are the "
                        "minimum for environmental control (S6.1)."
                    ),
                )
            )
    if cfg.co2_control_enabled and not cfg.co2_sensor:
        findings.append(
            WizardFinding(
                code="co2_control_without_sensor",
                hard=True,
                message=(
                    "CO2 setpoint control is enabled but no CO2 sensor is "
                    "mapped — there is no readback for the setpoint (S6.2)."
                ),
            )
        )
    if cfg.co2_control_enabled and not cfg.co2_solenoid_entities:
        findings.append(
            WizardFinding(
                code="co2_control_without_solenoid",
                hard=True,
                message=(
                    "CO2 setpoint control is enabled but no CO2 injection "
                    "solenoid is mapped — there is no actuator to drive "
                    "(S6.2)."
                ),
            )
        )
    return findings


def _check_hard_refusals(cfg: RoomEquipmentMap) -> list[WizardFinding]:
    """Collect every hard-refusal finding for a proposed config.

    Hard refusals (``cultivation_knowledge.md`` Section 6.1 / 6.4) — each
    is a structural gap that makes an *enabled* control surface unsafe to
    run, so the config cannot be saved:

    * PPFD control without any light entities (nothing to drive).
    * Irrigation gaps — see :func:`_check_irrigation_hard`.
    * Tank chemistry gaps — see :func:`_check_tank_hard`.
    * Environmental / CO2 sensor + actuator gaps — see
      :func:`_check_env_co2_hard`.

    Args:
        cfg: The proposed room equipment map.

    Returns:
        Every hard-refusal :class:`WizardFinding` that fired.
    """
    findings: list[WizardFinding] = []
    if cfg.ppfd_control_enabled and not cfg.light_entities:
        findings.append(
            WizardFinding(
                code="ppfd_without_lights",
                hard=True,
                message=(
                    "PPFD setpoint control is enabled but no light entities "
                    "are mapped. The executor has nothing to drive without "
                    "them (cultivation_knowledge.md S6.4)."
                ),
            )
        )
    findings.extend(_check_irrigation_hard(cfg))
    findings.extend(_check_tank_hard(cfg))
    findings.extend(_check_env_co2_hard(cfg))
    return findings


def _check_warnings(cfg: RoomEquipmentMap) -> list[WizardFinding]:
    """Collect the fail-soft warning findings for a proposed config.

    Fail-soft warnings (``cultivation_knowledge.md`` Section 6.3) — the
    config is runnable but carries a known cost:

    * Dehumidifier + AC both mapped but no reheat entity (EC-004) — the
      two units fight each other on VPD.
    * An exhaust entity mapped with CO2 control enabled (EC-005) — the
      exhaust vents the enrichment; poor ROI.
    * No under-canopy RH probe (EC-009) — humidity pockets under a dense
      flower canopy are invisible to the room sensor.
    * PPFD control with no cooling-headroom source (EC-001) — a PPFD
      increase cannot be sized against available cooling.

    Args:
        cfg: The proposed room equipment map.

    Returns:
        Every fail-soft :class:`WizardFinding` that fired.
    """
    findings: list[WizardFinding] = []

    # --- EC-004 — dehu + AC, no reheat ---------------------------------
    if cfg.dehumidifier_entities and cfg.ac_entities and not cfg.reheat_entities:
        findings.append(
            WizardFinding(
                code="EC-004",
                hard=False,
                message=(
                    "A dehumidifier and an AC are both mapped but no reheat "
                    "entity is declared. A standalone dehumidifier converts "
                    "latent moisture to sensible heat; without reheat the AC "
                    "over-cools to dehumidify and the two fight on VPD "
                    "(EC-004 Dehum<->Reheat)."
                ),
            )
        )

    # --- EC-005 — exhaust + CO2 enrichment -----------------------------
    if cfg.exhaust_entities and cfg.co2_control_enabled:
        findings.append(
            WizardFinding(
                code="EC-005",
                hard=False,
                message=(
                    "An exhaust fan is mapped and CO2 control is enabled — "
                    "the exhaust vents enrichment as fast as it is injected; "
                    "enrichment ROI suffers (EC-005 CO2<->Exhaust)."
                ),
            )
        )

    # --- EC-009 — no under-canopy RH probe -----------------------------
    if cfg.env_control_enabled and not cfg.under_canopy_rh_probe:
        findings.append(
            WizardFinding(
                code="EC-009",
                hard=False,
                message=(
                    "No under-canopy RH probe is declared. For dense flower "
                    "the room RH sensor cannot see humidity pockets under "
                    "the canopy — a botrytis risk (EC-009 Airflow<->Disease)."
                ),
            )
        )

    # --- EC-001 — PPFD range with no AC-headroom source ----------------
    if cfg.ppfd_control_enabled and not cfg.cooling_capacity_entity:
        findings.append(
            WizardFinding(
                code="EC-001",
                hard=False,
                message=(
                    "PPFD setpoint control is enabled but no cooling-"
                    "capacity / AC-headroom source (a climate.* or fan-stage "
                    "entity) is declared. A PPFD increase cannot be sized "
                    "against available cooling headroom (EC-001 PPFD<->HVAC)."
                ),
            )
        )

    return findings


def _coupling_notes(cfg: RoomEquipmentMap) -> list[str]:
    """Derive the hardware-constraint notes to persist into the room config.

    These are recorded into the room's stored config so the AI
    supervisor's durable context knows which constraints apply — they are
    not pass/fail, just facts the model should reason with.

    Args:
        cfg: The proposed room equipment map.

    Returns:
        A list of short constraint notes (possibly empty).
    """
    notes: list[str] = []
    if cfg.dehumidifier_entities and cfg.ac_entities and not cfg.reheat_entities:
        notes.append(
            "no reheat coil — Class B proposals must respect the "
            "dehu-sensible-heat budget (EC-004)"
        )
    if cfg.exhaust_entities and cfg.co2_control_enabled:
        notes.append(
            "exhaust + CO2 enrichment — suppress the exhaust before "
            "raising CO2 (EC-005)"
        )
    if not cfg.under_canopy_rh_probe:
        notes.append(
            "no under-canopy RH probe — room RH is not canopy-"
            "representative for dense flower (EC-009)"
        )
    if cfg.ppfd_control_enabled and not cfg.cooling_capacity_entity:
        notes.append(
            "no cooling-headroom source — PPFD increases need an HVAC "
            "re-sizing review (EC-001)"
        )
    if cfg.exhaust_entities:
        # An exhaust draws fresh air — flag the biosecurity constraint so
        # the supervisor knows intake filtration matters (EC-015).
        notes.append(
            "exhaust mapped — confirm the fresh-air intake is filtered "
            "(EC-015 biosecurity)"
        )
    return notes


@router.post("/validate", response_model=WizardValidationResult)
async def validate_room_config(
    config: RoomEquipmentMap,
) -> WizardValidationResult:
    """Validate a proposed room equipment map against the coupling rules.

    Runs the hard-refusal and fail-soft-warning checks from
    ``cultivation_knowledge.md`` Section 6 and derives the coupling notes
    to persist into the room config. Pure validation — no database
    access, no side effects.

    Args:
        config: The proposed :class:`RoomEquipmentMap`.

    Returns:
        A :class:`WizardValidationResult`. ``ok`` is ``True`` only when
        there are no hard refusals; warnings do not block a save.
    """
    hard_refusals = _check_hard_refusals(config)
    warnings = _check_warnings(config)
    coupling_notes = _coupling_notes(config)
    ok = not hard_refusals

    log.info(
        "config_wizard_validated",
        room_id=config.room_id,
        ok=ok,
        hard_refusals=len(hard_refusals),
        warnings=len(warnings),
    )
    return WizardValidationResult(
        ok=ok,
        hard_refusals=hard_refusals,
        warnings=warnings,
        coupling_notes=coupling_notes,
    )
