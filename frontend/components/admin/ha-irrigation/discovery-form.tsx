"use client";

import * as React from "react";
import { AlertTriangle, Plus } from "lucide-react";

import type {
  HaArea,
  HaEntity,
  HaIrrigationRegisterRoomLevel,
  HaIrrigationRegisterZone,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  EntityPicker,
  MultiEntityPicker,
} from "@/components/admin/entity-picker";
import { ZoneRow } from "@/components/admin/ha-irrigation/zone-row";

/**
 * The HA-IS discovery → review form.
 *
 * The page passes in the *current* draft (room name, zones,
 * room-level role picks, warnings) and a single `onPatch` setter; the
 * form is otherwise purely presentational. Each picker dispatches a
 * patch that the page's `useState` reducer merges and the form
 * re-renders. There is no per-pick API call — the operator submits the
 * whole confirmed mapping with the "Register with OCS" button at the
 * page level.
 *
 * Section layout:
 *
 * * Yellow warning banner — visible only when the discovery returned
 *   one or more warnings (oddities the operator should review).
 * * Room name — single text input pre-populated from
 *   `suggestedRoom.name`.
 * * Room-level controls card — one row per HA-IS role: pump,
 *   mainline valve, steering intent, EC targets (multi), anomaly
 *   binary sensor, phase select, RootSense report sensor.
 * * Zones card — one {@link ZoneRow} per detected zone, plus an
 *   "Add zone" button that appends a new draft zone with the next
 *   `zoneIndex`.
 */

export interface DiscoveryFormProps {
  /** The current room-name draft. */
  roomName: string;
  onRoomNameChange: (next: string) => void;
  /** Zone drafts (mutable). */
  zones: HaIrrigationRegisterZone[];
  onZonesChange: (next: HaIrrigationRegisterZone[]) => void;
  /** Room-level role drafts. */
  roomLevel: HaIrrigationRegisterRoomLevel;
  onRoomLevelChange: (
    patch: Partial<HaIrrigationRegisterRoomLevel>,
  ) => void;
  /** Warnings from the discovery payload — rendered as a yellow banner. */
  warnings: string[];
  entities: HaEntity[];
  areas: HaArea[];
  entityById: Map<string, HaEntity>;
  /** Pre-seed each picker's area filter with the room's HA area. */
  defaultAreaId?: string | null;
  /** Lock every input while the register mutation is in flight. */
  disabled?: boolean;
}

/** Compact section header — matches the room editor's role-section style. */
function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="mb-2 text-2xs font-semibold uppercase tracking-wide text-muted-foreground">
      {children}
    </h3>
  );
}

/** Tiny label rendered above each role picker. */
function RoleLabel({ children }: { children: React.ReactNode }) {
  return (
    <label className="mb-1 block text-xs text-muted-foreground">
      {children}
    </label>
  );
}

export function DiscoveryForm({
  roomName,
  onRoomNameChange,
  zones,
  onZonesChange,
  roomLevel,
  onRoomLevelChange,
  warnings,
  entities,
  areas,
  entityById,
  defaultAreaId,
  disabled = false,
}: DiscoveryFormProps) {
  /** Patch one zone by zoneIndex; the rest pass through unchanged. */
  const patchZone = React.useCallback(
    (zoneIndex: number, patch: Partial<HaIrrigationRegisterZone>) => {
      onZonesChange(
        zones.map((z) =>
          z.zoneIndex === zoneIndex ? { ...z, ...patch } : z,
        ),
      );
    },
    [zones, onZonesChange],
  );

  const removeZone = React.useCallback(
    (zoneIndex: number) => {
      onZonesChange(zones.filter((z) => z.zoneIndex !== zoneIndex));
    },
    [zones, onZonesChange],
  );

  /** Append a new zone with the next zone index (max existing + 1). */
  const addZone = React.useCallback(() => {
    const nextIndex =
      zones.length === 0
        ? 1
        : Math.max(...zones.map((z) => z.zoneIndex)) + 1;
    const next: HaIrrigationRegisterZone = {
      zoneIndex: nextIndex,
      locationLabel: `Zone ${nextIndex}`,
      vwcSensors: [],
      ecSensors: [],
      valves: [],
    };
    onZonesChange([...zones, next]);
  }, [zones, onZonesChange]);

  return (
    <div className="space-y-4" data-testid="ha-irrigation-discovery-form">
      {warnings.length > 0 ? (
        <Card
          className="border-impaired/40 bg-impaired/10"
          data-testid="ha-irrigation-warnings"
        >
          <CardContent className="space-y-1.5 p-3">
            <div className="flex items-center gap-2 text-xs font-semibold text-impaired">
              <AlertTriangle className="h-3.5 w-3.5" />
              Discovery surfaced {warnings.length} warning
              {warnings.length === 1 ? "" : "s"}
            </div>
            <ul className="ml-5 list-disc space-y-0.5 text-2xs text-impaired">
              {warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardContent className="space-y-3 p-4">
          <SectionLabel>Room</SectionLabel>
          <p className="text-2xs text-muted-foreground">
            The OCS room name the HA-IS install registers as. Defaults
            to the suggestion from discovery.
          </p>
          <div>
            <RoleLabel>Room name</RoleLabel>
            <Input
              value={roomName}
              onChange={(e) => onRoomNameChange(e.target.value)}
              placeholder="e.g. F1"
              disabled={disabled}
              data-testid="ha-room-name"
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="space-y-3 p-4">
          <SectionLabel>Room-level controls</SectionLabel>
          <p className="text-2xs text-muted-foreground">
            Single-entity roles (pump, mainline valve, steering intent
            slider, anomaly binary sensor, phase select, RootSense
            report sensor) and the EC-target multi-pick.
          </p>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            <div>
              <RoleLabel>Pump</RoleLabel>
              <EntityPicker
                value={roomLevel.pump ?? null}
                onChange={(id) =>
                  onRoomLevelChange({ pump: id ?? undefined })
                }
                entities={entities}
                areas={areas}
                entityById={entityById}
                defaultAreaId={defaultAreaId}
                disabled={disabled}
                data-testid="ha-room-pump"
              />
            </div>
            <div>
              <RoleLabel>Mainline valve</RoleLabel>
              <EntityPicker
                value={roomLevel.mainlineValve ?? null}
                onChange={(id) =>
                  onRoomLevelChange({ mainlineValve: id ?? undefined })
                }
                entities={entities}
                areas={areas}
                entityById={entityById}
                defaultAreaId={defaultAreaId}
                disabled={disabled}
                data-testid="ha-room-mainline-valve"
              />
            </div>
            <div>
              <RoleLabel>Steering intent (number entity)</RoleLabel>
              <EntityPicker
                value={roomLevel.steeringIntent ?? null}
                onChange={(id) =>
                  onRoomLevelChange({ steeringIntent: id ?? undefined })
                }
                entities={entities}
                areas={areas}
                entityById={entityById}
                defaultAreaId={defaultAreaId}
                disabled={disabled}
                data-testid="ha-room-steering-intent"
              />
            </div>
            <div>
              <RoleLabel>Anomaly binary sensor</RoleLabel>
              <EntityPicker
                value={roomLevel.anomalyBinarySensor ?? null}
                onChange={(id) =>
                  onRoomLevelChange({
                    anomalyBinarySensor: id ?? undefined,
                  })
                }
                entities={entities}
                areas={areas}
                entityById={entityById}
                defaultAreaId={defaultAreaId}
                disabled={disabled}
                data-testid="ha-room-anomaly"
              />
            </div>
            <div>
              <RoleLabel>Phase select</RoleLabel>
              <EntityPicker
                value={roomLevel.phaseSelect ?? null}
                onChange={(id) =>
                  onRoomLevelChange({ phaseSelect: id ?? undefined })
                }
                entities={entities}
                areas={areas}
                entityById={entityById}
                defaultAreaId={defaultAreaId}
                disabled={disabled}
                data-testid="ha-room-phase-select"
              />
            </div>
            <div>
              <RoleLabel>RootSense report sensor</RoleLabel>
              <EntityPicker
                value={roomLevel.rootsenseReportSensor ?? null}
                onChange={(id) =>
                  onRoomLevelChange({
                    rootsenseReportSensor: id ?? undefined,
                  })
                }
                entities={entities}
                areas={areas}
                entityById={entityById}
                defaultAreaId={defaultAreaId}
                disabled={disabled}
                data-testid="ha-room-rootsense"
              />
            </div>
          </div>
          <div className="border-t border-border pt-3">
            <RoleLabel>EC targets</RoleLabel>
            <MultiEntityPicker
              values={roomLevel.ecTargets}
              onChange={(next) => onRoomLevelChange({ ecTargets: next })}
              entities={entities}
              areas={areas}
              entityById={entityById}
              defaultAreaId={defaultAreaId}
              disabled={disabled}
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="flex items-center justify-between">
            <SectionLabel>Zones ({zones.length})</SectionLabel>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={addZone}
              disabled={disabled}
              data-testid="ha-add-zone"
            >
              <Plus className="h-3.5 w-3.5" />
              Add zone
            </Button>
          </div>
          {zones.length === 0 ? (
            <p className="rounded-md border border-dashed border-border px-3 py-6 text-center text-xs text-muted-foreground">
              No zones yet. Click &quot;Add zone&quot; to add one.
            </p>
          ) : (
            <div className="space-y-3">
              {zones.map((z) => (
                <ZoneRow
                  key={z.zoneIndex}
                  zone={z}
                  onChange={(patch) => patchZone(z.zoneIndex, patch)}
                  onRemove={() => removeZone(z.zoneIndex)}
                  entities={entities}
                  areas={areas}
                  entityById={entityById}
                  defaultAreaId={defaultAreaId}
                  disabled={disabled}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
