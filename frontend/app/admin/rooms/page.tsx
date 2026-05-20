"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";

import { api } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import type {
  Building,
  ConvexRoom,
  HaEntity,
} from "@/lib/types";
import { PageHeader } from "@/components/page-header";
import { QueryState, errorText } from "@/components/query-state";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { RoomEquipmentForm } from "@/components/admin/room-equipment-form";

/**
 * Admin — Rooms & Equipment (pass-5b).
 *
 * The page now talks to the Convex-aligned tables instead of the
 * legacy `room_runtime.equipment_map` blob:
 *
 * * `GET /api/sites/buildings` (lazy-creates "Facility" on first load)
 * * `GET /api/sites/rooms?buildingId=…` (the room list)
 * * `POST /api/sites/rooms` (create), `DELETE …` (delete)
 * * `GET /api/sensors?roomId=…` + `GET /api/equipment?roomId=…`
 *   (the editor's source data, fetched per-room on selection)
 *
 * The per-room editor ({@link RoomEquipmentForm}) writes through
 * {@link SensorRolePicker} — one POST / DELETE per picked entity.
 * There is no draft / Save Room button: each pick applies
 * immediately and the underlying queries refetch.
 *
 * Single-facility install assumption: the page auto-creates exactly
 * one `Facility` building if none exists, then pre-selects it. A
 * follow-up pass can surface a building-switcher when multi-building
 * installs land.
 */

const DEFAULT_FACILITY_NAME = "Facility";

export default function AdminRoomsPage() {
  const queryClient = useQueryClient();

  // --- buildings: load → lazy-create one named "Facility" -----------
  const buildingsQuery = useQuery({
    queryKey: queryKeys.buildings,
    queryFn: ({ signal }) => api.listBuildings(signal),
  });

  const buildings = React.useMemo(
    () => buildingsQuery.data?.buildings ?? [],
    [buildingsQuery.data],
  );

  /** Pick a building to act under — the first one returned, by created order. */
  const activeBuilding: Building | null = buildings[0] ?? null;

  const ensureFacilityMutation = useMutation({
    mutationFn: () => api.createBuilding({ name: DEFAULT_FACILITY_NAME }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.buildings });
    },
  });

  // After the buildings query lands with no rows, kick off a one-shot
  // POST so the rest of the page has a building to scope queries to.
  React.useEffect(() => {
    if (
      buildingsQuery.isSuccess &&
      buildings.length === 0 &&
      !ensureFacilityMutation.isPending &&
      !ensureFacilityMutation.isSuccess
    ) {
      ensureFacilityMutation.mutate();
    }
  }, [
    buildingsQuery.isSuccess,
    buildings.length,
    ensureFacilityMutation,
  ]);

  // --- rooms scoped to the active building --------------------------
  const roomsQuery = useQuery({
    queryKey: queryKeys.convexRooms(activeBuilding?.id),
    queryFn: ({ signal }) =>
      api.listConvexRooms(activeBuilding?.id ?? undefined, signal),
    enabled: activeBuilding !== null,
  });

  // --- HA registry (cached, large) ----------------------------------
  const registryQuery = useQuery({
    queryKey: queryKeys.haRegistry,
    queryFn: ({ signal }) => api.getHaRegistry(signal),
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

  const [selectedRoomId, setSelectedRoomId] = React.useState<string | null>(
    null,
  );

  const selectedRoom = React.useMemo<ConvexRoom | null>(
    () => rooms.find((r) => r.id === selectedRoomId) ?? null,
    [rooms, selectedRoomId],
  );

  // --- per-room sensors / equipment (fetched on selection) -----------
  const sensorsQuery = useQuery({
    queryKey: queryKeys.sensorsByRoom(selectedRoomId ?? ""),
    queryFn: ({ signal }) =>
      api.listSensors({ roomId: selectedRoomId ?? "" }, signal),
    enabled: selectedRoomId !== null,
  });
  const equipmentQuery = useQuery({
    queryKey: queryKeys.equipmentByRoom(selectedRoomId ?? ""),
    queryFn: ({ signal }) =>
      api.listEquipment({ roomId: selectedRoomId ?? "" }, signal),
    enabled: selectedRoomId !== null,
  });

  const sensors = React.useMemo(
    () => sensorsQuery.data?.sensors ?? [],
    [sensorsQuery.data],
  );
  const equipment = React.useMemo(
    () => equipmentQuery.data?.equipment ?? [],
    [equipmentQuery.data],
  );

  // --- create / delete room -----------------------------------------
  const [createOpen, setCreateOpen] = React.useState(false);
  const [newRoomName, setNewRoomName] = React.useState("");
  const [deleteTarget, setDeleteTarget] = React.useState<ConvexRoom | null>(
    null,
  );

  const createRoomMutation = useMutation({
    mutationFn: () =>
      api.createConvexRoom({
        buildingId: activeBuilding!.id,
        name: newRoomName.trim().toUpperCase(),
      }),
    onSuccess: (room) => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.convexRooms(activeBuilding?.id),
      });
      setSelectedRoomId(room.id);
      setNewRoomName("");
      setCreateOpen(false);
    },
  });

  const deleteRoomMutation = useMutation({
    mutationFn: (roomId: string) => api.deleteConvexRoom(roomId),
    onSuccess: (_res, roomId) => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.convexRooms(activeBuilding?.id),
      });
      setDeleteTarget(null);
      if (selectedRoomId === roomId) setSelectedRoomId(null);
    },
  });

  /** Match a room to an HA area by id or name (best-effort). */
  const roomAreaId = React.useMemo(() => {
    if (!selectedRoom) return null;
    const wantedName = selectedRoom.name.trim().toLowerCase();
    const hit = areas.find(
      (a) =>
        a.area_id.toLowerCase() === wantedName ||
        a.name.toLowerCase() === wantedName,
    );
    return hit?.area_id ?? null;
  }, [selectedRoom, areas]);

  const newRoomNameValid =
    newRoomName.trim().length > 0 &&
    !rooms.some(
      (r) => r.name.toLowerCase() === newRoomName.trim().toLowerCase(),
    );

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------

  return (
    <div>
      <PageHeader
        title="Rooms & Equipment"
        description="Map Home Assistant entities to per-room sensor and equipment records."
        actions={
          <Button
            size="sm"
            onClick={() => setCreateOpen(true)}
            disabled={activeBuilding === null}
          >
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

      {ensureFacilityMutation.isError ? (
        <Card className="mb-4 border-critical/40 bg-critical/10">
          <CardContent className="p-3 text-xs text-critical">
            Could not auto-create the &quot;Facility&quot; building:{" "}
            {errorText(ensureFacilityMutation.error)}.
          </CardContent>
        </Card>
      ) : null}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[16rem_1fr]">
        {/* room list */}
        <div>
          <QueryState
            isLoading={
              buildingsQuery.isLoading ||
              (activeBuilding !== null && roomsQuery.isLoading)
            }
            isError={buildingsQuery.isError || roomsQuery.isError}
            error={buildingsQuery.error ?? roomsQuery.error}
            isEmpty={activeBuilding !== null && rooms.length === 0}
            emptyMessage="No rooms in this facility. Create the first room to begin."
          >
            <Card>
              <CardContent className="space-y-1 p-2">
                {rooms.map((r) => {
                  const active = r.id === selectedRoomId;
                  return (
                    <div key={r.id} className="flex items-center gap-1">
                      <button
                        type="button"
                        data-testid="room-list-item"
                        onClick={() => setSelectedRoomId(r.id)}
                        className={
                          active
                            ? "flex-1 rounded-md bg-primary/15 px-2.5 py-1.5 text-left text-sm text-primary"
                            : "flex-1 rounded-md px-2.5 py-1.5 text-left text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                        }
                      >
                        <span className="block font-mono font-semibold uppercase">
                          {r.name}
                        </span>
                        {r.purpose ? (
                          <span className="block text-2xs text-muted-foreground">
                            {r.purpose}
                          </span>
                        ) : null}
                      </button>
                      <Button
                        type="button"
                        size="icon"
                        variant="ghost"
                        aria-label={`Delete room ${r.name}`}
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
          {!selectedRoom ? (
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
                      {selectedRoom.name}
                    </span>
                  </div>
                  {selectedRoom.purpose ? (
                    <p className="text-2xs text-muted-foreground">
                      {selectedRoom.purpose}
                    </p>
                  ) : null}
                </CardContent>
              </Card>

              <QueryState
                isLoading={sensorsQuery.isLoading || equipmentQuery.isLoading}
                isError={sensorsQuery.isError || equipmentQuery.isError}
                error={sensorsQuery.error ?? equipmentQuery.error}
              >
                <RoomEquipmentForm
                  roomId={selectedRoom.id}
                  sensors={sensors}
                  equipment={equipment}
                  entities={entities}
                  areas={areas}
                  entityById={entityById}
                  defaultAreaId={roomAreaId}
                />
              </QueryState>

              <p className="text-2xs text-muted-foreground">
                Changes apply immediately — there is no save button. Each
                picked entity is stored as a sensor or equipment record
                tied to this room.
              </p>
            </div>
          )}
        </div>
      </div>

      {/* create-room dialog */}
      <Dialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title="New room"
        description="Pick a short room name (e.g. F1, F2, VEG)."
        footer={
          <>
            <Button variant="outline" onClick={() => setCreateOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={!newRoomNameValid || createRoomMutation.isPending}
              onClick={() => createRoomMutation.mutate()}
            >
              {createRoomMutation.isPending ? "Creating…" : "Create"}
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">
              Room name
            </label>
            <Input
              value={newRoomName}
              onChange={(e) => setNewRoomName(e.target.value)}
              placeholder="e.g. F1"
              autoFocus
            />
            {newRoomName.trim() &&
            rooms.some(
              (r) =>
                r.name.toLowerCase() === newRoomName.trim().toLowerCase(),
            ) ? (
              <p className="mt-1 text-2xs text-critical">
                A room with this name already exists.
              </p>
            ) : null}
          </div>
          {createRoomMutation.isError ? (
            <p className="text-2xs text-critical">
              {errorText(createRoomMutation.error)}
            </p>
          ) : null}
        </div>
      </Dialog>

      {/* delete-confirm dialog */}
      <Dialog
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="Delete room"
        description={
          deleteTarget
            ? `Permanently delete room "${deleteTarget.name}"? Sensors and equipment in this room will lose their roomId.`
            : undefined
        }
        footer={
          <>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={deleteRoomMutation.isPending}
              onClick={() =>
                deleteTarget && deleteRoomMutation.mutate(deleteTarget.id)
              }
            >
              {deleteRoomMutation.isPending ? "Deleting…" : "Delete room"}
            </Button>
          </>
        }
      >
        {deleteRoomMutation.isError ? (
          <p className="text-xs text-critical">
            {errorText(deleteRoomMutation.error)}
          </p>
        ) : null}
      </Dialog>
    </div>
  );
}
