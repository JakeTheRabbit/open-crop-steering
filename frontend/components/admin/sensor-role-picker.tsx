"use client";

import * as React from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import type {
  Equipment,
  EquipmentType,
  HaArea,
  HaEntity,
  Sensor,
  SensorType,
} from "@/lib/types";
import { MultiEntityPicker } from "@/components/admin/entity-picker";

/**
 * Role picker that auto-creates a `sensors` or `equipment` record
 * each time an HA entity is picked.
 *
 * Pass-5b retired the legacy `room_runtime.equipment_map` JSONB.
 * Each HA entity an operator picks is now a first-class
 * {@link Sensor} or {@link Equipment} row with `externalId` set to
 * the HA entity_id and `integrationId` server-resolved to the
 * singleton Home Assistant `sensorIntegrations` row.
 *
 * The picker UX stays the same shape (a {@link MultiEntityPicker}
 * with chips for current picks + a search-popover to add another),
 * but every selection change fires the matching POST / DELETE
 * immediately:
 *
 * * Picking a new entity → POST `/api/sensors` or `/api/equipment`
 *   with `externalId=<entity_id>` and the role's Convex `type`. The
 *   backend lazy-creates the HA integration on first call.
 * * Removing a current pick → DELETE the matching record by id.
 * * Re-picking an entity that was previously removed → fresh POST
 *   (the previous record was deleted, so this is a new row).
 *
 * The component owns its own cache invalidation: after a successful
 * mutation it invalidates `queryKeys.sensorsByRoom(roomId)` /
 * `queryKeys.equipmentByRoom(roomId)` so the page state refetches
 * and reflects the new server truth.
 *
 * The parent passes in the *current* list of records (already
 * filtered to this `roomId` + `type` by the page) and the
 * inverse-mapping lookup (`recordByExternalId`). The picker derives
 * its `values` (current HA entity ids) from those records and never
 * holds a separate draft.
 */

export type SensorRoleSpec = {
  kind: "sensor";
  type: SensorType;
  /** A human-readable role label folded into the new record's `name`. */
  roleLabel: string;
  /** Display unit on the new sensor row (e.g. ``"°C"``). */
  dataUnit: string;
};

export type EquipmentRoleSpec = {
  kind: "equipment";
  type: EquipmentType;
  /** A human-readable role label folded into the new record's `name`. */
  roleLabel: string;
};

export type RoleSpec = SensorRoleSpec | EquipmentRoleSpec;

export interface SensorRolePickerProps {
  /** The Convex `rooms.id` every new record FKs to. */
  roomId: string;
  /** The role this picker manages (one Convex `type` × kind). */
  role: RoleSpec;
  /** UI label shown above the picker (e.g. "Air temperature"). */
  label: string;
  /** Existing sensor / equipment rows for this role in this room. */
  records: ReadonlyArray<Sensor | Equipment>;
  entities: HaEntity[];
  areas: HaArea[];
  entityById: Map<string, HaEntity>;
  defaultAreaId?: string | null;
  disabled?: boolean;
}

/**
 * Cheap slug for the new record's `code` field. Mirrors the slugifier
 * in `tools/migrate_room_runtime_to_rooms.py` so a record created via
 * the picker and one materialized via the migration look the same.
 */
function slugifyEntityId(value: string): string {
  let slug = "";
  for (const ch of value.toLowerCase()) {
    slug += /[a-z0-9]/.test(ch) ? ch : "-";
  }
  return slug.replace(/-+/g, "-").replace(/^-|-$/g, "") || "entity";
}

export function SensorRolePicker({
  roomId,
  role,
  label,
  records,
  entities,
  areas,
  entityById,
  defaultAreaId,
  disabled = false,
}: SensorRolePickerProps) {
  const queryClient = useQueryClient();

  /** Current selection in HA-entity-id form. */
  const values = React.useMemo(
    () =>
      records
        .map((r) => r.externalId)
        .filter((id): id is string => typeof id === "string" && id.length > 0),
    [records],
  );

  /** Entity_id → record, for resolving a removed value back to its id. */
  const recordByExternalId = React.useMemo(() => {
    const m = new Map<string, Sensor | Equipment>();
    for (const r of records) {
      if (r.externalId) m.set(r.externalId, r);
    }
    return m;
  }, [records]);

  /** Invalidate whichever bucket this role lives in. */
  const invalidate = React.useCallback(() => {
    const key =
      role.kind === "sensor"
        ? queryKeys.sensorsByRoom(roomId)
        : queryKeys.equipmentByRoom(roomId);
    void queryClient.invalidateQueries({ queryKey: key });
  }, [queryClient, role.kind, roomId]);

  const createMutation = useMutation({
    mutationFn: async (entityId: string) => {
      if (role.kind === "sensor") {
        return api.createSensor({
          name: `${role.roleLabel} (${entityId})`,
          code: slugifyEntityId(entityId),
          type: role.type,
          dataUnit: role.dataUnit,
          status: "active",
          isActive: true,
          roomId,
          externalId: entityId,
          notes: role.roleLabel,
        });
      }
      return api.createEquipment({
        name: `${role.roleLabel} (${entityId})`,
        code: slugifyEntityId(entityId),
        type: role.type,
        status: "operational",
        isActive: true,
        roomId,
        externalId: entityId,
        notes: role.roleLabel,
      });
    },
    onSuccess: invalidate,
  });

  const deleteMutation = useMutation({
    mutationFn: async (record: Sensor | Equipment) => {
      if (role.kind === "sensor") {
        return api.deleteSensor(record.id);
      }
      return api.deleteEquipment(record.id);
    },
    onSuccess: invalidate,
  });

  const pending = createMutation.isPending || deleteMutation.isPending;
  const error = createMutation.error ?? deleteMutation.error;

  /** Diff the new value list against the current one. */
  const handleChange = (next: string[]) => {
    const current = new Set(values);
    const incoming = new Set(next);
    for (const id of next) {
      if (!current.has(id)) createMutation.mutate(id);
    }
    for (const id of values) {
      if (!incoming.has(id)) {
        const record = recordByExternalId.get(id);
        if (record) deleteMutation.mutate(record);
      }
    }
  };

  return (
    <div data-testid={`role-picker-${role.kind}-${role.type}`}>
      <label className="mb-1 block text-xs text-muted-foreground">
        {label}
      </label>
      <MultiEntityPicker
        values={values}
        onChange={handleChange}
        entities={entities}
        areas={areas}
        entityById={entityById}
        defaultAreaId={defaultAreaId}
        disabled={disabled || pending}
      />
      {error ? (
        <p className="mt-1 text-2xs text-critical">
          {error instanceof ApiError && typeof error.detail === "string"
            ? error.detail
            : error.message}
        </p>
      ) : null}
    </div>
  );
}
