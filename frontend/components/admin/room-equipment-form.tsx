"use client";

import * as React from "react";
import { Plus, Trash2 } from "lucide-react";

import type {
  HaArea,
  HaEntity,
  RoomEquipmentMap,
  RoomTank,
  RoomZone,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import {
  EntityPicker,
  MultiEntityPicker,
} from "@/components/admin/entity-picker";

/**
 * The per-room equipment-map editor.
 *
 * Renders the five control-enable toggles, an {@link EntityPicker} for
 * every scalar role field, and dynamic add/remove lists for irrigation
 * zones and nutrient tanks. The component is fully controlled: the
 * parent owns the `RoomEquipmentMap` draft and an `onChange` patcher.
 */

/** A single-entity role field of a {@link RoomEquipmentMap}. */
type ScalarRoleKey =
  | "temp_sensor"
  | "leaf_temp_sensor"
  | "rh_sensor"
  | "co2_sensor"
  | "under_canopy_rh_probe"
  | "cooling_capacity_entity";

/** A multi-entity (list) role field of a {@link RoomEquipmentMap}. */
type MultiRoleKey =
  | "light_entities"
  | "ac_entities"
  | "dehumidifier_entities"
  | "reheat_entities"
  | "exhaust_entities"
  | "co2_solenoid_entities";

/** A control-enable boolean of a {@link RoomEquipmentMap}. */
type ToggleKey =
  | "env_control_enabled"
  | "ppfd_control_enabled"
  | "irrigation_control_enabled"
  | "tank_control_enabled"
  | "co2_control_enabled";

const TOGGLES: { key: ToggleKey; label: string; hint: string }[] = [
  {
    key: "env_control_enabled",
    label: "Environment control",
    hint: "Temp / RH / VPD steering",
  },
  {
    key: "ppfd_control_enabled",
    label: "PPFD control",
    hint: "Lighting intensity steering",
  },
  {
    key: "irrigation_control_enabled",
    label: "Irrigation control",
    hint: "Zone valve / pump steering",
  },
  {
    key: "tank_control_enabled",
    label: "Tank control",
    hint: "Nutrient tank pH / EC dosing",
  },
  {
    key: "co2_control_enabled",
    label: "CO2 control",
    hint: "CO2 solenoid steering",
  },
];

/** Single-entity role fields (sensors + the headroom source). */
const SENSOR_FIELDS: { key: ScalarRoleKey; label: string }[] = [
  { key: "temp_sensor", label: "Air temperature sensor" },
  { key: "rh_sensor", label: "Relative humidity sensor" },
  { key: "leaf_temp_sensor", label: "Leaf temperature sensor" },
  { key: "co2_sensor", label: "CO2 sensor" },
  { key: "under_canopy_rh_probe", label: "Under-canopy RH probe" },
  { key: "cooling_capacity_entity", label: "Cooling-capacity source" },
];

/** Multi-entity role fields (actuators — several entities each). */
const ACTUATOR_FIELDS: { key: MultiRoleKey; label: string }[] = [
  { key: "light_entities", label: "Grow lights" },
  { key: "ac_entities", label: "Air conditioners" },
  { key: "dehumidifier_entities", label: "Dehumidifiers" },
  { key: "reheat_entities", label: "Reheat" },
  { key: "exhaust_entities", label: "Exhaust fans" },
  { key: "co2_solenoid_entities", label: "CO2 solenoids" },
];

export interface RoomEquipmentFormProps {
  value: RoomEquipmentMap;
  onChange: (next: RoomEquipmentMap) => void;
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

export function RoomEquipmentForm({
  value,
  onChange,
  entities,
  areas,
  entityById,
  defaultAreaId,
  disabled = false,
}: RoomEquipmentFormProps) {
  const patch = (p: Partial<RoomEquipmentMap>) =>
    onChange({ ...value, ...p });

  // --- zones ----------------------------------------------------------
  const addZone = () => {
    const next: RoomZone = {
      zone_id: `zone${value.zones.length + 1}`,
      valve_entity: null,
      pump_entity: null,
      vwc_sensor: null,
      ec_sensor: null,
    };
    patch({ zones: [...value.zones, next] });
  };
  const updateZone = (i: number, p: Partial<RoomZone>) =>
    patch({
      zones: value.zones.map((z, idx) => (idx === i ? { ...z, ...p } : z)),
    });
  const removeZone = (i: number) =>
    patch({ zones: value.zones.filter((_, idx) => idx !== i) });

  // --- tanks ----------------------------------------------------------
  const addTank = () => {
    const next: RoomTank = {
      tank_id: `tank${value.tanks.length + 1}`,
      ph_sensor: null,
      ec_sensor: null,
      doser_entities: [],
    };
    patch({ tanks: [...value.tanks, next] });
  };
  const updateTank = (i: number, p: Partial<RoomTank>) =>
    patch({
      tanks: value.tanks.map((t, idx) => (idx === i ? { ...t, ...p } : t)),
    });
  const removeTank = (i: number) =>
    patch({ tanks: value.tanks.filter((_, idx) => idx !== i) });
  const addDoser = (i: number) => {
    const tank = value.tanks[i];
    if (!tank) return;
    updateTank(i, { doser_entities: [...tank.doser_entities, ""] });
  };
  const updateDoser = (ti: number, di: number, entityId: string | null) => {
    const tank = value.tanks[ti];
    if (!tank) return;
    updateTank(ti, {
      doser_entities: tank.doser_entities.map((d, idx) =>
        idx === di ? entityId ?? "" : d,
      ),
    });
  };
  const removeDoser = (ti: number, di: number) => {
    const tank = value.tanks[ti];
    if (!tank) return;
    updateTank(ti, {
      doser_entities: tank.doser_entities.filter((_, idx) => idx !== di),
    });
  };

  /** A labelled single-entity picker row. */
  const pickerRow = (key: ScalarRoleKey, label: string) => (
    <div key={key}>
      <label className="mb-1 block text-xs text-muted-foreground">
        {label}
      </label>
      <EntityPicker
        value={value[key]}
        onChange={(id) => patch({ [key]: id } as Partial<RoomEquipmentMap>)}
        entities={entities}
        areas={areas}
        entityById={entityById}
        defaultAreaId={defaultAreaId}
        disabled={disabled}
        data-testid={`picker-${key}`}
      />
    </div>
  );

  /** A labelled multi-entity picker row (actuators). */
  const multiPickerRow = (key: MultiRoleKey, label: string) => (
    <div key={key}>
      <label className="mb-1 block text-xs text-muted-foreground">
        {label}
      </label>
      <MultiEntityPicker
        values={value[key]}
        onChange={(ids) =>
          patch({ [key]: ids } as Partial<RoomEquipmentMap>)
        }
        entities={entities}
        areas={areas}
        entityById={entityById}
        defaultAreaId={defaultAreaId}
        disabled={disabled}
      />
    </div>
  );

  return (
    <div className="space-y-4">
      {/* control toggles */}
      <Card>
        <CardContent className="p-4">
          <SectionLabel>Control modes</SectionLabel>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {TOGGLES.map((t) => (
              <label
                key={t.key}
                className="flex items-start gap-2 rounded-md border border-border p-2.5 text-sm"
              >
                <input
                  type="checkbox"
                  checked={value[t.key]}
                  disabled={disabled}
                  onChange={(e) =>
                    patch({ [t.key]: e.target.checked } as Partial<RoomEquipmentMap>)
                  }
                  className="mt-0.5 accent-[hsl(var(--primary))]"
                />
                <span>
                  <span className="block font-medium">{t.label}</span>
                  <span className="block text-2xs text-muted-foreground">
                    {t.hint}
                  </span>
                </span>
              </label>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* sensors — one reference entity each */}
      <Card>
        <CardContent className="p-4">
          <SectionLabel>Sensors</SectionLabel>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {SENSOR_FIELDS.map((f) => pickerRow(f.key, f.label))}
          </div>
        </CardContent>
      </Card>

      {/* actuators — several entities each */}
      <Card>
        <CardContent className="p-4">
          <SectionLabel>Actuators &amp; equipment</SectionLabel>
          <p className="mb-3 text-2xs text-muted-foreground">
            Each role takes multiple entities — a room often has several
            AC units or grow-light circuits.
          </p>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {ACTUATOR_FIELDS.map((f) => multiPickerRow(f.key, f.label))}
          </div>
        </CardContent>
      </Card>

      {/* zones */}
      <Card>
        <CardContent className="p-4">
          <div className="mb-2 flex items-center justify-between">
            <SectionLabel>Irrigation zones</SectionLabel>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={disabled}
              onClick={addZone}
            >
              <Plus className="h-3.5 w-3.5" />
              Add zone
            </Button>
          </div>
          {value.zones.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No zones. Add one to assign valve, pump, VWC and EC entities.
            </p>
          ) : (
            <div className="space-y-3">
              {value.zones.map((z, i) => (
                <div
                  key={i}
                  className="rounded-md border border-border p-3"
                  data-testid="zone-row"
                >
                  <div className="mb-2 flex items-center gap-2">
                    <Input
                      value={z.zone_id}
                      disabled={disabled}
                      onChange={(e) =>
                        updateZone(i, { zone_id: e.target.value })
                      }
                      placeholder="zone id"
                      className="h-8 max-w-[12rem] font-mono text-xs"
                      aria-label={`Zone ${i + 1} id`}
                    />
                    <div className="flex-1" />
                    <Button
                      type="button"
                      size="icon"
                      variant="ghost"
                      disabled={disabled}
                      aria-label={`Remove zone ${z.zone_id}`}
                      className="h-8 w-8"
                      onClick={() => removeZone(i)}
                    >
                      <Trash2 className="h-3.5 w-3.5 text-critical" />
                    </Button>
                  </div>
                  <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                    {(
                      [
                        ["valve_entity", "Valve"],
                        ["pump_entity", "Pump"],
                        ["vwc_sensor", "VWC sensor"],
                        ["ec_sensor", "EC sensor"],
                      ] as const
                    ).map(([key, label]) => (
                      <div key={key}>
                        <label className="mb-1 block text-xs text-muted-foreground">
                          {label}
                        </label>
                        <EntityPicker
                          value={z[key]}
                          onChange={(id) => updateZone(i, { [key]: id })}
                          entities={entities}
                          areas={areas}
                          entityById={entityById}
                          defaultAreaId={defaultAreaId}
                          disabled={disabled}
                        />
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* tanks */}
      <Card>
        <CardContent className="p-4">
          <div className="mb-2 flex items-center justify-between">
            <SectionLabel>Nutrient tanks</SectionLabel>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={disabled}
              onClick={addTank}
            >
              <Plus className="h-3.5 w-3.5" />
              Add tank
            </Button>
          </div>
          {value.tanks.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No tanks. Add one to assign pH, EC and doser entities.
            </p>
          ) : (
            <div className="space-y-3">
              {value.tanks.map((t, i) => (
                <div
                  key={i}
                  className="rounded-md border border-border p-3"
                  data-testid="tank-row"
                >
                  <div className="mb-2 flex items-center gap-2">
                    <Input
                      value={t.tank_id}
                      disabled={disabled}
                      onChange={(e) =>
                        updateTank(i, { tank_id: e.target.value })
                      }
                      placeholder="tank id"
                      className="h-8 max-w-[12rem] font-mono text-xs"
                      aria-label={`Tank ${i + 1} id`}
                    />
                    <div className="flex-1" />
                    <Button
                      type="button"
                      size="icon"
                      variant="ghost"
                      disabled={disabled}
                      aria-label={`Remove tank ${t.tank_id}`}
                      className="h-8 w-8"
                      onClick={() => removeTank(i)}
                    >
                      <Trash2 className="h-3.5 w-3.5 text-critical" />
                    </Button>
                  </div>
                  <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                    {(
                      [
                        ["ph_sensor", "pH sensor"],
                        ["ec_sensor", "EC sensor"],
                      ] as const
                    ).map(([key, label]) => (
                      <div key={key}>
                        <label className="mb-1 block text-xs text-muted-foreground">
                          {label}
                        </label>
                        <EntityPicker
                          value={t[key]}
                          onChange={(id) => updateTank(i, { [key]: id })}
                          entities={entities}
                          areas={areas}
                          entityById={entityById}
                          defaultAreaId={defaultAreaId}
                          disabled={disabled}
                        />
                      </div>
                    ))}
                  </div>
                  <div className="mt-3">
                    <div className="mb-1 flex items-center justify-between">
                      <label className="text-xs text-muted-foreground">
                        Doser entities
                      </label>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        disabled={disabled}
                        className="h-7"
                        onClick={() => addDoser(i)}
                      >
                        <Plus className="h-3.5 w-3.5" />
                        Add doser
                      </Button>
                    </div>
                    {t.doser_entities.length === 0 ? (
                      <p className="text-2xs text-muted-foreground">
                        No dosers assigned.
                      </p>
                    ) : (
                      <div className="space-y-2">
                        {t.doser_entities.map((d, di) => (
                          <div
                            key={di}
                            className="flex items-center gap-1"
                          >
                            <div className="flex-1">
                              <EntityPicker
                                value={d || null}
                                onChange={(id) => updateDoser(i, di, id)}
                                entities={entities}
                                areas={areas}
                                entityById={entityById}
                                defaultAreaId={defaultAreaId}
                                disabled={disabled}
                                placeholder="Select a doser entity…"
                              />
                            </div>
                            <Button
                              type="button"
                              size="icon"
                              variant="ghost"
                              disabled={disabled}
                              aria-label={`Remove doser ${di + 1}`}
                              className="h-9 w-9 shrink-0"
                              onClick={() => removeDoser(i, di)}
                            >
                              <Trash2 className="h-3.5 w-3.5 text-critical" />
                            </Button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
