"use client";

import * as React from "react";

import type {
  Equipment,
  EquipmentType,
  HaArea,
  HaEntity,
  Sensor,
  SensorType,
} from "@/lib/types";
import { Card, CardContent } from "@/components/ui/card";
import {
  type EquipmentRoleSpec,
  type RoleSpec,
  type SensorRoleSpec,
  SensorRolePicker,
} from "@/components/admin/sensor-role-picker";

/**
 * The per-room equipment editor — pass-5b rewrite.
 *
 * Pre-pass-5b this component owned a `RoomEquipmentMap` draft that
 * the page PUT back to `/api/rooms/{room_id}`. Pass 5b retired that
 * JSONB hack: each picked HA entity is now a first-class
 * {@link Sensor} or {@link Equipment} row keyed by `roomId`. The
 * editor renders one {@link SensorRolePicker} per role, grouping
 * the existing records by Convex `type` and the per-role tag the
 * picker emits.
 *
 * The component is purely presentational — it does not own a draft
 * and does not call the API directly. The {@link SensorRolePicker}
 * inside each row handles its own POST / DELETE / cache
 * invalidation, so the parent's job is just to slice the data and
 * render.
 */

/** Convex `sensors.type` + tag, the latter folded into the new record's `notes`. */
type SensorRole = {
  kind: "sensor";
  /** UI key for the role; not persisted directly. */
  id: string;
  label: string;
  spec: SensorRoleSpec;
};

type EquipmentRole = {
  kind: "equipment";
  id: string;
  label: string;
  spec: EquipmentRoleSpec;
};

type RoleDef = SensorRole | EquipmentRole;

const ENV_SENSORS: SensorRole[] = [
  {
    kind: "sensor",
    id: "temp_sensors",
    label: "Air temperature",
    spec: {
      kind: "sensor",
      type: "temperature",
      roleLabel: "air temperature",
      dataUnit: "°C",
    },
  },
  {
    kind: "sensor",
    id: "rh_sensors",
    label: "Relative humidity",
    spec: {
      kind: "sensor",
      type: "humidity",
      roleLabel: "relative humidity",
      dataUnit: "%",
    },
  },
  {
    kind: "sensor",
    id: "co2_sensors",
    label: "CO2",
    spec: {
      kind: "sensor",
      type: "co2",
      roleLabel: "CO2",
      dataUnit: "ppm",
    },
  },
  {
    kind: "sensor",
    id: "leaf_temp_sensors",
    label: "Leaf temperature",
    spec: {
      kind: "sensor",
      type: "temperature",
      roleLabel: "leaf temperature",
      dataUnit: "°C",
    },
  },
  {
    kind: "sensor",
    id: "under_canopy_rh_probes",
    label: "Under-canopy RH probe",
    spec: {
      kind: "sensor",
      type: "humidity",
      roleLabel: "under-canopy RH probe",
      dataUnit: "%",
    },
  },
];

const SUBSTRATE_LIGHT_SENSORS: SensorRole[] = [
  {
    kind: "sensor",
    id: "vwc_sensors",
    label: "Substrate VWC",
    spec: {
      kind: "sensor",
      type: "moisture",
      roleLabel: "substrate VWC",
      dataUnit: "%",
    },
  },
  {
    kind: "sensor",
    id: "ec_sensors",
    label: "Substrate EC (pwEC)",
    spec: {
      kind: "sensor",
      type: "ec",
      roleLabel: "substrate EC",
      dataUnit: "mS/cm",
    },
  },
  {
    kind: "sensor",
    id: "ppfd_sensors",
    label: "PPFD",
    spec: {
      kind: "sensor",
      type: "light",
      roleLabel: "PPFD",
      dataUnit: "µmol/m²/s",
    },
  },
  {
    kind: "sensor",
    id: "dli_sensors",
    label: "DLI",
    spec: {
      kind: "sensor",
      type: "light",
      roleLabel: "DLI",
      dataUnit: "mol/m²/day",
    },
  },
];

const AIR_QUALITY_SENSORS: SensorRole[] = [
  {
    kind: "sensor",
    id: "pm1_sensors",
    label: "PM1.0",
    spec: {
      kind: "sensor",
      type: "air_quality",
      roleLabel: "PM1.0",
      dataUnit: "µg/m³",
    },
  },
  {
    kind: "sensor",
    id: "pm25_sensors",
    label: "PM2.5",
    spec: {
      kind: "sensor",
      type: "air_quality",
      roleLabel: "PM2.5",
      dataUnit: "µg/m³",
    },
  },
  {
    kind: "sensor",
    id: "pm4_sensors",
    label: "PM4.0",
    spec: {
      kind: "sensor",
      type: "air_quality",
      roleLabel: "PM4.0",
      dataUnit: "µg/m³",
    },
  },
  {
    kind: "sensor",
    id: "pm10_sensors",
    label: "PM10",
    spec: {
      kind: "sensor",
      type: "air_quality",
      roleLabel: "PM10",
      dataUnit: "µg/m³",
    },
  },
];

const ACTUATORS: EquipmentRole[] = [
  {
    kind: "equipment",
    id: "light_entities",
    label: "Grow lights",
    spec: { kind: "equipment", type: "lighting", roleLabel: "grow light" },
  },
  {
    kind: "equipment",
    id: "ac_entities",
    label: "Air conditioners",
    spec: { kind: "equipment", type: "hvac", roleLabel: "AC unit" },
  },
  {
    kind: "equipment",
    id: "dehumidifier_entities",
    label: "Dehumidifiers",
    spec: { kind: "equipment", type: "hvac", roleLabel: "dehumidifier" },
  },
  {
    kind: "equipment",
    id: "reheat_entities",
    label: "Reheat",
    spec: { kind: "equipment", type: "hvac", roleLabel: "reheat coil" },
  },
  {
    kind: "equipment",
    id: "exhaust_entities",
    label: "Exhaust fans",
    spec: { kind: "equipment", type: "hvac", roleLabel: "exhaust fan" },
  },
  {
    kind: "equipment",
    id: "co2_solenoid_entities",
    label: "CO2 solenoids",
    spec: { kind: "equipment", type: "hvac", roleLabel: "CO2 solenoid" },
  },
];

const IRRIGATION_SUPPLY: EquipmentRole[] = [
  {
    kind: "equipment",
    id: "irrigation_pump_entities",
    label: "Irrigation pump",
    spec: {
      kind: "equipment",
      type: "irrigation",
      roleLabel: "irrigation pump",
    },
  },
  {
    kind: "equipment",
    id: "mainline_valve_entities",
    label: "Mainline / manifold valves",
    spec: {
      kind: "equipment",
      type: "irrigation",
      roleLabel: "mainline valve",
    },
  },
];

const COOLING_CAPACITY: EquipmentRole = {
  kind: "equipment",
  id: "cooling_capacity_entity",
  label: "Cooling-capacity source",
  spec: {
    kind: "equipment",
    type: "monitoring",
    roleLabel: "cooling capacity source",
  },
};

export interface RoomEquipmentFormProps {
  /** The Convex `rooms.id` every record is scoped to. */
  roomId: string;
  /** Every sensor record already FK'd to `roomId`. */
  sensors: ReadonlyArray<Sensor>;
  /** Every equipment record already FK'd to `roomId`. */
  equipment: ReadonlyArray<Equipment>;
  entities: HaEntity[];
  areas: HaArea[];
  entityById: Map<string, HaEntity>;
  /** The room's HA area, used to pre-seed each picker's area filter. */
  defaultAreaId?: string | null;
  disabled?: boolean;
}

/** Section heading inside the form. */
function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="mb-2 text-2xs font-semibold uppercase tracking-wide text-muted-foreground">
      {children}
    </h3>
  );
}

/**
 * Slice the records list for one role.
 *
 * Sensor records of the same Convex `type` cover multiple roles
 * (e.g. `"humidity"` is both `rh_sensors` and `under_canopy_rh_probes`).
 * They are disambiguated by the `notes` tag the picker writes on
 * create — anything not matching the role's tag stays in the catch-all
 * primary role for that type.
 */
function recordsForRole<T extends Sensor | Equipment>(
  all: ReadonlyArray<T>,
  role: RoleDef,
  catchAllByType: Map<string, RoleDef>,
): T[] {
  const tag = role.spec.roleLabel;
  return all.filter((r) => {
    if (r.type !== role.spec.type) return false;
    const recordTag = r.notes ?? "";
    // The role that owns the type as its catch-all collects anything
    // whose notes don't pin it to a sibling role.
    const isCatchAll = catchAllByType.get(role.spec.type)?.id === role.id;
    if (isCatchAll) {
      // Drop records that point at a known sibling role.
      for (const candidate of catchAllByType.values()) {
        if (
          candidate.id !== role.id &&
          candidate.spec.type === role.spec.type &&
          recordTag === candidate.spec.roleLabel
        ) {
          return false;
        }
      }
      return true;
    }
    return recordTag === tag;
  });
}

/**
 * Build the type → primary-role index used to split records with
 * the same Convex `type` across multiple UI roles. The first role
 * declared for a given type wins.
 */
function buildPrimaryRoleIndex(roles: RoleDef[]): Map<string, RoleDef> {
  const seen = new Map<string, RoleDef>();
  for (const r of roles) {
    if (!seen.has(r.spec.type)) seen.set(r.spec.type, r);
  }
  return seen;
}

export function RoomEquipmentForm({
  roomId,
  sensors,
  equipment,
  entities,
  areas,
  entityById,
  defaultAreaId,
  disabled = false,
}: RoomEquipmentFormProps) {
  const allSensorRoles: SensorRole[] = React.useMemo(
    () => [...ENV_SENSORS, ...SUBSTRATE_LIGHT_SENSORS, ...AIR_QUALITY_SENSORS],
    [],
  );
  const allEquipmentRoles: EquipmentRole[] = React.useMemo(
    () => [...ACTUATORS, ...IRRIGATION_SUPPLY, COOLING_CAPACITY],
    [],
  );

  /** First-role-per-type index for the sensor side. */
  const sensorTypePrimary = React.useMemo(
    () => buildPrimaryRoleIndex(allSensorRoles),
    [allSensorRoles],
  );
  /** First-role-per-type index for the equipment side. */
  const equipmentTypePrimary = React.useMemo(
    () => buildPrimaryRoleIndex(allEquipmentRoles),
    [allEquipmentRoles],
  );

  const renderSensorRole = (role: SensorRole) => (
    <SensorRolePicker
      key={role.id}
      roomId={roomId}
      role={role.spec satisfies RoleSpec}
      label={role.label}
      records={recordsForRole(sensors, role, sensorTypePrimary)}
      entities={entities}
      areas={areas}
      entityById={entityById}
      defaultAreaId={defaultAreaId}
      disabled={disabled}
    />
  );

  const renderEquipmentRole = (role: EquipmentRole) => (
    <SensorRolePicker
      key={role.id}
      roomId={roomId}
      role={role.spec satisfies RoleSpec}
      label={role.label}
      records={recordsForRole(equipment, role, equipmentTypePrimary)}
      entities={entities}
      areas={areas}
      entityById={entityById}
      defaultAreaId={defaultAreaId}
      disabled={disabled}
    />
  );

  return (
    <div className="space-y-4" data-testid="room-equipment-form">
      <Card>
        <CardContent className="p-4">
          <SectionLabel>Environment sensors</SectionLabel>
          <p className="mb-3 text-2xs text-muted-foreground">
            Each role takes multiple HA entities — a room usually has
            several temp / RH / CO2 probes at different canopy heights.
            Picking an entity creates a sensor record; removing one
            deletes it.
          </p>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {ENV_SENSORS.map(renderSensorRole)}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-4">
          <SectionLabel>Substrate &amp; light sensors</SectionLabel>
          <p className="mb-3 text-2xs text-muted-foreground">
            Substrate moisture / pore-water EC and canopy light.
          </p>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {SUBSTRATE_LIGHT_SENSORS.map(renderSensorRole)}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-4">
          <SectionLabel>Air-quality sensors</SectionLabel>
          <p className="mb-3 text-2xs text-muted-foreground">
            Particulate matter — PM1.0 / PM2.5 / PM4.0 / PM10.
          </p>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {AIR_QUALITY_SENSORS.map(renderSensorRole)}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-4">
          <SectionLabel>Actuators &amp; equipment</SectionLabel>
          <p className="mb-3 text-2xs text-muted-foreground">
            Each role takes multiple entities — a room often has several
            AC units or grow-light circuits.
          </p>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {ACTUATORS.map(renderEquipmentRole)}
          </div>
          <div className="mt-3 grid grid-cols-1 gap-3 border-t border-border pt-3 lg:grid-cols-2">
            {renderEquipmentRole(COOLING_CAPACITY)}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-4">
          <SectionLabel>Irrigation supply</SectionLabel>
          <p className="mb-3 text-2xs text-muted-foreground">
            The pump(s) and mainline / manifold valve(s) that open for
            every shot.
          </p>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {IRRIGATION_SUPPLY.map(renderEquipmentRole)}
          </div>
          <p className="mt-3 border-t border-border pt-3 text-2xs text-muted-foreground">
            Per-zone valves and per-tank chemistry control are not yet
            modelled in the new rooms tier — they will land in a
            follow-up pass alongside the irrigation executor.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

// Re-export the role specs in case page-level code needs to inspect
// them (e.g. when computing what's "configured" in a room summary).
export type { EquipmentType, SensorType };
