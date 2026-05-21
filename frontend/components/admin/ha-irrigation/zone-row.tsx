"use client";

import * as React from "react";
import { Trash2 } from "lucide-react";

import type { HaArea, HaEntity, HaIrrigationRegisterZone } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { MultiEntityPicker } from "@/components/admin/entity-picker";

/**
 * One zone (HA-IS location) row in the discovery form.
 *
 * Mirrors the per-row layout of the OCS room editor: a small header
 * with the zone index + label input + a remove button, then three
 * stacked {@link MultiEntityPicker}s for the VWC / EC / valve roles.
 *
 * The component is purely presentational — it owns no state. The
 * parent ({@link DiscoveryForm}) holds the draft zones list and
 * patches a single zone through {@link onChange}; the user-confirmed
 * draft is what eventually ships in the register POST body.
 */
export interface ZoneRowProps {
  /** The current zone draft (index, label, picked entities). */
  zone: HaIrrigationRegisterZone;
  /** Patch the zone in-place — partial fields are merged by the parent. */
  onChange: (patch: Partial<HaIrrigationRegisterZone>) => void;
  /** Remove this row from the form. */
  onRemove: () => void;
  entities: HaEntity[];
  areas: HaArea[];
  entityById: Map<string, HaEntity>;
  defaultAreaId?: string | null;
  disabled?: boolean;
}

/** Tiny section label, matches the OCS room editor's role labels. */
function RoleLabel({ children }: { children: React.ReactNode }) {
  return (
    <label className="mb-1 block text-xs text-muted-foreground">
      {children}
    </label>
  );
}

export function ZoneRow({
  zone,
  onChange,
  onRemove,
  entities,
  areas,
  entityById,
  defaultAreaId,
  disabled = false,
}: ZoneRowProps) {
  return (
    <Card data-testid={`ha-zone-row-${zone.zoneIndex}`}>
      <CardContent className="space-y-3 p-4">
        <div className="flex items-end justify-between gap-3">
          <div className="flex-1">
            <RoleLabel>Zone label</RoleLabel>
            <div className="flex items-center gap-2">
              <span className="rounded-md border border-border bg-muted px-2 py-1 font-mono text-2xs uppercase text-muted-foreground">
                Zone {zone.zoneIndex}
              </span>
              <Input
                value={zone.locationLabel}
                onChange={(e) => onChange({ locationLabel: e.target.value })}
                placeholder={`Zone ${zone.zoneIndex}`}
                disabled={disabled}
                data-testid={`ha-zone-label-${zone.zoneIndex}`}
              />
            </div>
          </div>
          <Button
            type="button"
            size="icon"
            variant="ghost"
            aria-label={`Remove zone ${zone.zoneIndex}`}
            disabled={disabled}
            onClick={onRemove}
            className="h-9 w-9 shrink-0"
          >
            <Trash2 className="h-3.5 w-3.5 text-critical" />
          </Button>
        </div>

        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <div>
            <RoleLabel>VWC sensors</RoleLabel>
            <MultiEntityPicker
              values={zone.vwcSensors}
              onChange={(next) => onChange({ vwcSensors: next })}
              entities={entities}
              areas={areas}
              entityById={entityById}
              defaultAreaId={defaultAreaId}
              disabled={disabled}
            />
          </div>
          <div>
            <RoleLabel>EC sensors</RoleLabel>
            <MultiEntityPicker
              values={zone.ecSensors}
              onChange={(next) => onChange({ ecSensors: next })}
              entities={entities}
              areas={areas}
              entityById={entityById}
              defaultAreaId={defaultAreaId}
              disabled={disabled}
            />
          </div>
        </div>

        <div>
          <RoleLabel>Valves</RoleLabel>
          <MultiEntityPicker
            values={zone.valves}
            onChange={(next) => onChange({ valves: next })}
            entities={entities}
            areas={areas}
            entityById={entityById}
            defaultAreaId={defaultAreaId}
            disabled={disabled}
          />
          <p className="mt-1 text-2xs text-muted-foreground">
            Typically a single zone valve; multi-valve zones are
            supported.
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
