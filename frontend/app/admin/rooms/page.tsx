"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";

import { api } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import type {
  HaEntity,
  Room,
  RoomEquipmentMap,
  RoomUpsertBody,
} from "@/lib/types";
import { PageHeader } from "@/components/page-header";
import { QueryState, errorText } from "@/components/query-state";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { RoomEquipmentForm } from "@/components/admin/room-equipment-form";

/**
 * Admin — Rooms & Equipment.
 *
 * Lets an admin map Home Assistant entities to per-room control roles.
 * The left column lists configured rooms (`GET /api/rooms`); selecting
 * one loads its `equipment_map` into the {@link RoomEquipmentForm}.
 * Saving issues a `PUT /api/rooms/{room_id}`.
 *
 * Static export — all data fetching is client-side via TanStack Query.
 */

/**
 * Build a complete, default {@link RoomEquipmentMap} for `roomId`.
 *
 * The backend schema is `extra="forbid"`, so the saved body must carry
 * exactly these keys. A room with `equipment_map === {}` (unconfigured)
 * is normalized through here; an existing partial map is merged on top.
 */
function defaultEquipmentMap(
  roomId: string,
  existing?: Partial<RoomEquipmentMap>,
): RoomEquipmentMap {
  /** Copy a list-valued field, or default to an empty list. */
  const list = (v: readonly string[] | undefined): string[] =>
    v ? [...v] : [];
  return {
    room_id: roomId,
    env_control_enabled: existing?.env_control_enabled ?? false,
    ppfd_control_enabled: existing?.ppfd_control_enabled ?? false,
    irrigation_control_enabled: existing?.irrigation_control_enabled ?? false,
    tank_control_enabled: existing?.tank_control_enabled ?? false,
    co2_control_enabled: existing?.co2_control_enabled ?? false,
    temp_sensors: list(existing?.temp_sensors),
    rh_sensors: list(existing?.rh_sensors),
    co2_sensors: list(existing?.co2_sensors),
    leaf_temp_sensors: list(existing?.leaf_temp_sensors),
    under_canopy_rh_probes: list(existing?.under_canopy_rh_probes),
    vwc_sensors: list(existing?.vwc_sensors),
    ec_sensors: list(existing?.ec_sensors),
    ppfd_sensors: list(existing?.ppfd_sensors),
    dli_sensors: list(existing?.dli_sensors),
    pm1_sensors: list(existing?.pm1_sensors),
    pm25_sensors: list(existing?.pm25_sensors),
    pm4_sensors: list(existing?.pm4_sensors),
    pm10_sensors: list(existing?.pm10_sensors),
    cooling_capacity_entity: existing?.cooling_capacity_entity ?? null,
    light_entities: list(existing?.light_entities),
    ac_entities: list(existing?.ac_entities),
    dehumidifier_entities: list(existing?.dehumidifier_entities),
    reheat_entities: list(existing?.reheat_entities),
    exhaust_entities: list(existing?.exhaust_entities),
    co2_solenoid_entities: list(existing?.co2_solenoid_entities),
    zones: existing?.zones ? existing.zones.map((z) => ({ ...z })) : [],
    tanks: existing?.tanks
      ? existing.tanks.map((t) => ({
          ...t,
          doser_entities: [...t.doser_entities],
        }))
      : [],
  };
}

/** True when `equipment_map` carries real config rather than `{}`. */
function hasEquipmentMap(
  m: Room["equipment_map"],
): m is RoomEquipmentMap {
  return typeof m === "object" && m !== null && "room_id" in m;
}

/** A draft being edited: the room id, display name and equipment map. */
interface RoomDraft {
  roomId: string;
  displayName: string;
  equipmentMap: RoomEquipmentMap;
  /** True for a brand-new room not yet persisted. */
  isNew: boolean;
}

export default function AdminRoomsPage() {
  const queryClient = useQueryClient();

  const roomsQuery = useQuery({
    queryKey: queryKeys.rooms,
    queryFn: ({ signal }) => api.listRooms(signal),
  });
  const registryQuery = useQuery({
    queryKey: queryKeys.haRegistry,
    queryFn: ({ signal }) => api.getHaRegistry(signal),
    // The registry is large and changes rarely — cache it generously.
    staleTime: 5 * 60_000,
    refetchInterval: false,
  });

  const rooms = React.useMemo(
    () => roomsQuery.data?.rooms ?? [],
    [roomsQuery.data],
  );
  const entities = React.useMemo(
    () => registryQuery.data?.entities ?? [],
    [registryQuery.data],
  );
  const areas = React.useMemo(
    () => registryQuery.data?.areas ?? [],
    [registryQuery.data],
  );
  /** entity_id → entity, for resolving picker selections to labels. */
  const entityById = React.useMemo(() => {
    const m = new Map<string, HaEntity>();
    for (const e of entities) m.set(e.entity_id, e);
    return m;
  }, [entities]);

  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [draft, setDraft] = React.useState<RoomDraft | null>(null);
  const [createOpen, setCreateOpen] = React.useState(false);
  const [newRoomId, setNewRoomId] = React.useState("");
  const [newDisplayName, setNewDisplayName] = React.useState("");
  const [deleteTarget, setDeleteTarget] = React.useState<Room | null>(null);
  const [saved, setSaved] = React.useState<string | null>(null);

  /** Load an existing room into the editor. */
  const selectRoom = React.useCallback((room: Room) => {
    setSelectedId(room.room_id);
    setSaved(null);
    setDraft({
      roomId: room.room_id,
      displayName: room.display_name ?? "",
      equipmentMap: defaultEquipmentMap(
        room.room_id,
        hasEquipmentMap(room.equipment_map)
          ? room.equipment_map
          : undefined,
      ),
      isNew: false,
    });
  }, []);

  const saveMutation = useMutation({
    mutationFn: (vars: { roomId: string; body: RoomUpsertBody }) =>
      api.saveRoom(vars.roomId, vars.body),
    onSuccess: (room) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.rooms });
      setSaved(room.room_id);
      setDraft((prev) =>
        prev ? { ...prev, isNew: false } : prev,
      );
      setSelectedId(room.room_id);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (roomId: string) => api.deleteRoom(roomId),
    onSuccess: (_res, roomId) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.rooms });
      setDeleteTarget(null);
      if (selectedId === roomId) {
        setSelectedId(null);
        setDraft(null);
      }
    },
  });

  /** Match a room to an HA area by id or name (best-effort). */
  const roomAreaId = React.useMemo(() => {
    if (!draft) return null;
    const wanted = draft.roomId.toLowerCase();
    const wantedName = draft.displayName.trim().toLowerCase();
    const hit = areas.find(
      (a) =>
        a.area_id.toLowerCase() === wanted ||
        a.name.toLowerCase() === wanted ||
        (wantedName && a.name.toLowerCase() === wantedName),
    );
    return hit?.area_id ?? null;
  }, [draft, areas]);

  const handleSave = () => {
    if (!draft) return;
    setSaved(null);
    saveMutation.mutate({
      roomId: draft.roomId,
      body: {
        display_name: draft.displayName.trim() || null,
        // room_id must equal the path param — keep them in lockstep.
        equipment_map: { ...draft.equipmentMap, room_id: draft.roomId },
      },
    });
  };

  const handleCreate = () => {
    const id = newRoomId.trim();
    if (!id) return;
    setCreateOpen(false);
    setSelectedId(id);
    setSaved(null);
    setDraft({
      roomId: id,
      displayName: newDisplayName.trim(),
      equipmentMap: defaultEquipmentMap(id),
      isNew: true,
    });
    setNewRoomId("");
    setNewDisplayName("");
  };

  const newRoomIdValid =
    newRoomId.trim().length > 0 &&
    !rooms.some((r) => r.room_id === newRoomId.trim());

  return (
    <div>
      <PageHeader
        title="Rooms & Equipment"
        description="Map Home Assistant entities to per-room control roles."
        actions={
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="h-3.5 w-3.5" />
            New room
          </Button>
        }
      />

      {registryQuery.isError ? (
        <Card className="mb-4 border-critical/40 bg-critical/10">
          <CardContent className="p-3 text-xs text-critical">
            Could not load the Home Assistant entity registry:{" "}
            {errorText(registryQuery.error)}. Entity pickers will be empty
            until it loads.
          </CardContent>
        </Card>
      ) : null}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[16rem_1fr]">
        {/* room list */}
        <div>
          <QueryState
            isLoading={roomsQuery.isLoading}
            isError={roomsQuery.isError}
            error={roomsQuery.error}
            isEmpty={rooms.length === 0}
            emptyMessage="No rooms configured. Create the first room to begin."
          >
            <Card>
              <CardContent className="space-y-1 p-2">
                {rooms.map((r) => {
                  const active = r.room_id === selectedId;
                  const configured = hasEquipmentMap(r.equipment_map);
                  return (
                    <div key={r.room_id} className="flex items-center gap-1">
                      <button
                        type="button"
                        data-testid="room-list-item"
                        onClick={() => selectRoom(r)}
                        className={
                          active
                            ? "flex-1 rounded-md bg-primary/15 px-2.5 py-1.5 text-left text-sm text-primary"
                            : "flex-1 rounded-md px-2.5 py-1.5 text-left text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                        }
                      >
                        <span className="block font-mono font-semibold uppercase">
                          {r.room_id}
                        </span>
                        <span className="block text-2xs text-muted-foreground">
                          {r.display_name || "—"}
                          {!configured ? " · unconfigured" : ""}
                        </span>
                      </button>
                      <Button
                        type="button"
                        size="icon"
                        variant="ghost"
                        aria-label={`Delete room ${r.room_id}`}
                        className="h-8 w-8 shrink-0"
                        onClick={() => setDeleteTarget(r)}
                      >
                        <Trash2 className="h-3.5 w-3.5 text-critical" />
                      </Button>
                    </div>
                  );
                })}
              </CardContent>
            </Card>
          </QueryState>
        </div>

        {/* editor */}
        <div>
          {!draft ? (
            <Card>
              <CardContent className="p-8 text-center text-sm text-muted-foreground">
                Select a room on the left, or create a new one, to assign
                its Home Assistant entities.
              </CardContent>
            </Card>
          ) : (
            <div className="space-y-4">
              <Card>
                <CardContent className="space-y-3 p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-base font-semibold uppercase">
                      {draft.roomId}
                    </span>
                    {draft.isNew ? (
                      <Badge variant="warning">unsaved — new room</Badge>
                    ) : null}
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-muted-foreground">
                      Display name
                    </label>
                    <Input
                      value={draft.displayName}
                      onChange={(e) =>
                        setDraft((prev) =>
                          prev
                            ? { ...prev, displayName: e.target.value }
                            : prev,
                        )
                      }
                      placeholder="e.g. Flower Room 1"
                      className="max-w-sm"
                    />
                  </div>
                </CardContent>
              </Card>

              <RoomEquipmentForm
                value={draft.equipmentMap}
                onChange={(next) =>
                  setDraft((prev) =>
                    prev ? { ...prev, equipmentMap: next } : prev,
                  )
                }
                entities={entities}
                areas={areas}
                entityById={entityById}
                defaultAreaId={roomAreaId}
                disabled={saveMutation.isPending}
              />

              <div className="flex flex-wrap items-center gap-3">
                <Button
                  onClick={handleSave}
                  disabled={saveMutation.isPending}
                >
                  {saveMutation.isPending ? "Saving…" : "Save room"}
                </Button>
                {saved === draft.roomId && !saveMutation.isPending ? (
                  <span className="text-xs text-healthy">
                    Saved successfully.
                  </span>
                ) : null}
                {saveMutation.isError ? (
                  <span className="text-xs text-critical">
                    {errorText(saveMutation.error)}
                  </span>
                ) : null}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* create-room dialog */}
      <Dialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title="New room"
        description="Pick a short room id (e.g. f1) and an optional display name."
        footer={
          <>
            <Button variant="outline" onClick={() => setCreateOpen(false)}>
              Cancel
            </Button>
            <Button disabled={!newRoomIdValid} onClick={handleCreate}>
              Create
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">
              Room id
            </label>
            <Input
              value={newRoomId}
              onChange={(e) => setNewRoomId(e.target.value)}
              placeholder="e.g. f1"
              autoFocus
            />
            {newRoomId.trim() &&
            rooms.some((r) => r.room_id === newRoomId.trim()) ? (
              <p className="mt-1 text-2xs text-critical">
                A room with this id already exists.
              </p>
            ) : null}
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">
              Display name (optional)
            </label>
            <Input
              value={newDisplayName}
              onChange={(e) => setNewDisplayName(e.target.value)}
              placeholder="e.g. Flower Room 1"
            />
          </div>
          <p className="text-2xs text-muted-foreground">
            The room is not persisted until you assign its equipment and
            press Save.
          </p>
        </div>
      </Dialog>

      {/* delete-confirm dialog */}
      <Dialog
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="Delete room"
        description={
          deleteTarget
            ? `Permanently delete room "${deleteTarget.room_id}" and its equipment map?`
            : undefined
        }
        footer={
          <>
            <Button
              variant="outline"
              onClick={() => setDeleteTarget(null)}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={deleteMutation.isPending}
              onClick={() =>
                deleteTarget && deleteMutation.mutate(deleteTarget.room_id)
              }
            >
              {deleteMutation.isPending ? "Deleting…" : "Delete room"}
            </Button>
          </>
        }
      >
        {deleteMutation.isError ? (
          <p className="text-xs text-critical">
            {errorText(deleteMutation.error)}
          </p>
        ) : null}
      </Dialog>
    </div>
  );
}
