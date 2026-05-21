"use client";

import * as React from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, RefreshCw } from "lucide-react";

import { api } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import type {
  HaEntity,
  HaIrrigationDiscoveryResult,
  HaIrrigationRegisterBody,
  HaIrrigationRegisterResult,
  HaIrrigationRegisterRoomLevel,
  HaIrrigationRegisterZone,
} from "@/lib/types";
import { PageHeader } from "@/components/page-header";
import { QueryState, errorText } from "@/components/query-state";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { DiscoveryForm } from "@/components/admin/ha-irrigation/discovery-form";

/**
 * Admin → Integrations → HA-Irrigation-Strategy (P1).
 *
 * The first integration pass for HA-IS. The page wires three steps
 * into a single screen:
 *
 *   1. **Discover** — `GET /api/integrations/ha-irrigation/discover`
 *      runs on mount; the backend walks HA, finds the `crop_steering_*`
 *      entities, infers a zone count, and returns a candidate mapping
 *      (per-zone VWC / EC / valve suggestions, room-level pump /
 *      mainline / intent / EC-targets / anomaly / phase / RootSense
 *      report suggestions) plus a warnings list of detected oddities.
 *
 *   2. **Review + edit** — the candidate mapping seeds an editable
 *      {@link DiscoveryForm}: each role is a {@link MultiEntityPicker} or
 *      {@link EntityPicker} that the operator can add to / remove from /
 *      swap. The form draft is local React state — there is no
 *      per-pick API call.
 *
 *   3. **Register** — "Register with OCS" POSTs the confirmed mapping
 *      to `/api/integrations/ha-irrigation/register`. The backend
 *      creates / updates the room + locations + sensors + equipment
 *      rows in one atomic call and returns the counts plus a `records`
 *      breakdown. On success we render a success card with a link to
 *      `/admin/rooms`.
 *
 * Re-discovery is a header button that re-runs step 1 and resets the
 * draft to the new candidates. A confirm dialog appears if the form
 * is dirty so an in-progress edit isn't silently lost.
 *
 * The HA registry (`getHaRegistry`) is fetched in parallel because the
 * pickers need the full ~8000-entity list (so the operator can pick
 * *any* entity, not just the discovery candidates).
 *
 * Design spec: `docs/concepts/ha-irrigation-strategy-integration.md`
 * (§1, §2, §3 for the overlap map, §6 P1 row for this page's scope,
 * §7 for the data-model translation the register handler runs).
 */

/** Empty room-level draft — all single-role fields cleared. */
const EMPTY_ROOM_LEVEL: HaIrrigationRegisterRoomLevel = {
  pump: undefined,
  mainlineValve: undefined,
  steeringIntent: undefined,
  ecTargets: [],
  anomalyBinarySensor: undefined,
  phaseSelect: undefined,
  rootsenseReportSensor: undefined,
};

/** Seed the draft from a discovery result. Pure function — easy to test. */
function seedDraftFromDiscovery(
  d: HaIrrigationDiscoveryResult,
): {
  roomName: string;
  zones: HaIrrigationRegisterZone[];
  roomLevel: HaIrrigationRegisterRoomLevel;
} {
  const zones: HaIrrigationRegisterZone[] = d.zones.map((z) => ({
    zoneIndex: z.zoneIndex,
    locationLabel: z.suggestedLocationLabel,
    vwcSensors: [...z.candidateEntities.vwcSensors],
    ecSensors: [...z.candidateEntities.ecSensors],
    valves: [...z.candidateEntities.valves],
  }));
  const c = d.roomLevelCandidates;
  const roomLevel: HaIrrigationRegisterRoomLevel = {
    // Single-entity roles: take the first candidate if any.
    pump: c.pump[0],
    mainlineValve: c.mainlineValve[0],
    steeringIntent: c.steeringIntent[0],
    anomalyBinarySensor: c.anomalyBinarySensor[0],
    phaseSelect: c.phaseSelect[0],
    rootsenseReportSensor: c.rootsenseReportSensor[0],
    // Multi-entity role.
    ecTargets: [...c.ecTargets],
  };
  return { roomName: d.suggestedRoom.name, zones, roomLevel };
}

/**
 * Cheap value-equality check between two draft snapshots — used to
 * tell whether the form is dirty. JSON stringify is fine here: the
 * shapes are small (one room object) and the field set is fixed by
 * the contract, so key-order is stable across mutations.
 */
function draftEqual(
  a: { roomName: string; zones: HaIrrigationRegisterZone[]; roomLevel: HaIrrigationRegisterRoomLevel },
  b: { roomName: string; zones: HaIrrigationRegisterZone[]; roomLevel: HaIrrigationRegisterRoomLevel },
): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

export default function HaIrrigationIntegrationPage() {
  const queryClient = useQueryClient();

  // --- discover ----------------------------------------------------
  const discoveryQuery = useQuery({
    queryKey: queryKeys.haIrrigationDiscovery,
    queryFn: ({ signal }) => api.discoverHaIrrigation(signal),
    // The discovery is operator-driven (button-refreshable) — don't
    // refetch on focus / interval.
    refetchOnWindowFocus: false,
    refetchInterval: false,
  });

  // --- HA registry (cached, large) ---------------------------------
  const registryQuery = useQuery({
    queryKey: queryKeys.haRegistry,
    queryFn: ({ signal }) => api.getHaRegistry(signal),
    staleTime: 5 * 60_000,
    refetchInterval: false,
  });

  const entities = React.useMemo(
    () => registryQuery.data?.entities ?? [],
    [registryQuery.data],
  );
  const areas = React.useMemo(
    () => registryQuery.data?.areas ?? [],
    [registryQuery.data],
  );
  const entityById = React.useMemo(() => {
    const m = new Map<string, HaEntity>();
    for (const e of entities) m.set(e.entity_id, e);
    return m;
  }, [entities]);

  // --- draft state -------------------------------------------------
  // The draft mirrors the shape of the register POST body's room +
  // zones + roomLevel sections. We track the "seeded" snapshot so the
  // re-discover dialog can compare against the operator's current
  // draft to decide whether to warn about losing edits.
  const [roomName, setRoomName] = React.useState("");
  const [zones, setZones] = React.useState<HaIrrigationRegisterZone[]>([]);
  const [roomLevel, setRoomLevel] = React.useState<HaIrrigationRegisterRoomLevel>(
    EMPTY_ROOM_LEVEL,
  );
  /** The most recent discovery payload, kept for dirty-comparison + reseed. */
  const seededRef = React.useRef<{
    roomName: string;
    zones: HaIrrigationRegisterZone[];
    roomLevel: HaIrrigationRegisterRoomLevel;
  } | null>(null);
  /** The discovery payload the draft most recently seeded from. */
  const lastSeededDiscoveryRef = React.useRef<HaIrrigationDiscoveryResult | null>(
    null,
  );

  // Seed the draft each time a fresh discovery result lands and the
  // operator hasn't already started editing this exact payload.
  React.useEffect(() => {
    const d = discoveryQuery.data;
    if (!d) return;
    if (lastSeededDiscoveryRef.current === d) return;
    const seeded = seedDraftFromDiscovery(d);
    setRoomName(seeded.roomName);
    setZones(seeded.zones);
    setRoomLevel(seeded.roomLevel);
    seededRef.current = seeded;
    lastSeededDiscoveryRef.current = d;
    // Successful (re)discover also clears any prior register result.
    setRegisterResult(null);
  }, [discoveryQuery.data]);

  const currentDraft = React.useMemo(
    () => ({ roomName, zones, roomLevel }),
    [roomName, zones, roomLevel],
  );
  const isDirty = React.useMemo(
    () => seededRef.current !== null && !draftEqual(currentDraft, seededRef.current),
    [currentDraft],
  );

  // --- re-discover (confirm if dirty) -------------------------------
  const [reDiscoverOpen, setReDiscoverOpen] = React.useState(false);
  const runDiscover = React.useCallback(() => {
    void queryClient.invalidateQueries({
      queryKey: queryKeys.haIrrigationDiscovery,
    });
  }, [queryClient]);

  const onReDiscoverClick = React.useCallback(() => {
    if (isDirty) {
      setReDiscoverOpen(true);
      return;
    }
    runDiscover();
  }, [isDirty, runDiscover]);

  // --- register ----------------------------------------------------
  const [registerResult, setRegisterResult] =
    React.useState<HaIrrigationRegisterResult | null>(null);

  const registerMutation = useMutation({
    mutationFn: (body: HaIrrigationRegisterBody) =>
      api.registerHaIrrigation(body),
    onSuccess: (res) => {
      setRegisterResult(res);
      // The new room shows up in /admin/rooms — invalidate so a tab
      // there refetches without a manual reload.
      void queryClient.invalidateQueries({ queryKey: queryKeys.buildings });
    },
  });

  /**
   * Enable rule for the "Register with OCS" button.
   *
   * The contract requires:
   * * a non-empty room name,
   * * at least one zone with at least one valve picked.
   *
   * Anything beyond that is the backend's call (it validates the
   * full mapping on POST and returns `ok: false` with the offending
   * fields in the body if something doesn't compose).
   */
  const canRegister = React.useMemo(() => {
    if (!roomName.trim()) return false;
    return zones.some((z) => z.valves.length > 0);
  }, [roomName, zones]);

  const submitRegister = () => {
    const body: HaIrrigationRegisterBody = {
      room: { name: roomName.trim() },
      zones: zones.map((z) => ({
        zoneIndex: z.zoneIndex,
        locationLabel: z.locationLabel.trim() || `Zone ${z.zoneIndex}`,
        vwcSensors: z.vwcSensors,
        ecSensors: z.ecSensors,
        valves: z.valves,
      })),
      roomLevel: { ...roomLevel },
    };
    registerMutation.mutate(body);
  };

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------

  return (
    <div>
      <PageHeader
        title="HA-Irrigation-Strategy integration"
        description="Register the HA-IS install as one OCS room. The page discovers candidate sensors / valves / role entities, lets you review and edit, then materializes the room + locations + sensors + equipment rows in one call."
        actions={
          <Button
            size="sm"
            variant="outline"
            onClick={onReDiscoverClick}
            disabled={discoveryQuery.isFetching}
            data-testid="ha-rediscover"
          >
            <RefreshCw
              className={
                discoveryQuery.isFetching
                  ? "h-3.5 w-3.5 animate-spin"
                  : "h-3.5 w-3.5"
              }
            />
            Re-discover from HA
          </Button>
        }
      />

      {registryQuery.isError ? (
        <Card className="mb-4 border-critical/40 bg-critical/10">
          <CardContent className="p-3 text-xs text-critical">
            Could not load the Home Assistant entity registry:{" "}
            {errorText(registryQuery.error)}. Entity pickers will be
            empty until it loads — the discovery candidates will still
            show as raw entity IDs.
          </CardContent>
        </Card>
      ) : null}

      <QueryState
        isLoading={discoveryQuery.isLoading}
        isError={discoveryQuery.isError}
        error={discoveryQuery.error}
      >
        {discoveryQuery.data ? (
          <div className="space-y-4">
            {registerResult ? (
              <Card
                className="border-healthy/40 bg-healthy/10"
                data-testid="ha-register-success"
              >
                <CardContent className="space-y-3 p-4">
                  <div className="flex items-center gap-2 text-sm font-semibold text-healthy">
                    <CheckCircle2 className="h-4 w-4" />
                    Registered with OCS
                  </div>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
                    <div>
                      <div className="text-2xs text-muted-foreground">
                        Locations
                      </div>
                      <div className="font-mono">
                        {registerResult.locationsCreated} created,{" "}
                        {registerResult.locationsUpdated} updated
                      </div>
                    </div>
                    <div>
                      <div className="text-2xs text-muted-foreground">
                        Sensors
                      </div>
                      <div className="font-mono">
                        {registerResult.sensorsCreated} created,{" "}
                        {registerResult.sensorsUpdated} updated
                      </div>
                    </div>
                    <div>
                      <div className="text-2xs text-muted-foreground">
                        Equipment
                      </div>
                      <div className="font-mono">
                        {registerResult.equipmentCreated} created,{" "}
                        {registerResult.equipmentUpdated} updated
                      </div>
                    </div>
                    <div>
                      <div className="text-2xs text-muted-foreground">
                        Room id
                      </div>
                      <div className="truncate font-mono">
                        {registerResult.roomId}
                      </div>
                    </div>
                  </div>
                  <div>
                    <Button asChild size="sm" variant="outline">
                      <Link
                        href="/admin/rooms"
                        data-testid="ha-register-view-rooms-link"
                      >
                        View room in Rooms &amp; Equipment
                      </Link>
                    </Button>
                  </div>
                </CardContent>
              </Card>
            ) : null}

            <Card>
              <CardContent className="space-y-1 p-3 text-2xs text-muted-foreground">
                <div>
                  Detected{" "}
                  <span className="font-mono text-foreground">
                    {discoveryQuery.data.detectedZoneCount}
                  </span>{" "}
                  zone
                  {discoveryQuery.data.detectedZoneCount === 1 ? "" : "s"}{" "}
                  from HA-IS{" "}
                  <span className="font-mono">
                    ({discoveryQuery.data.suggestedRoom.externalSystemId})
                  </span>
                  .
                </div>
              </CardContent>
            </Card>

            <DiscoveryForm
              roomName={roomName}
              onRoomNameChange={setRoomName}
              zones={zones}
              onZonesChange={setZones}
              roomLevel={roomLevel}
              onRoomLevelChange={(patch) =>
                setRoomLevel((prev) => ({ ...prev, ...patch }))
              }
              warnings={discoveryQuery.data.warnings}
              entities={entities}
              areas={areas}
              entityById={entityById}
              disabled={registerMutation.isPending}
            />

            <Card>
              <CardContent className="flex flex-wrap items-center justify-between gap-3 p-4">
                <div className="space-y-1 text-2xs text-muted-foreground">
                  <div>
                    The register call is atomic — every row lands or
                    nothing does. Re-running it with the same room
                    name is idempotent (rows are updated, not
                    duplicated).
                  </div>
                  {!canRegister ? (
                    <div className="text-impaired">
                      Register is disabled until the room has a name and
                      at least one zone has a valve picked.
                    </div>
                  ) : null}
                </div>
                <Button
                  size="default"
                  onClick={submitRegister}
                  disabled={!canRegister || registerMutation.isPending}
                  data-testid="ha-register-submit"
                >
                  {registerMutation.isPending
                    ? "Registering…"
                    : "Register with OCS"}
                </Button>
              </CardContent>
            </Card>

            {registerMutation.isError ? (
              <Card
                className="border-critical/40 bg-critical/10"
                data-testid="ha-register-error"
              >
                <CardContent className="p-3 text-xs text-critical">
                  Register failed: {errorText(registerMutation.error)}
                </CardContent>
              </Card>
            ) : null}
            {registerMutation.data && !registerMutation.data.ok ? (
              <Card
                className="border-critical/40 bg-critical/10"
                data-testid="ha-register-not-ok"
              >
                <CardContent className="p-3 text-xs text-critical">
                  Register did not complete cleanly — the server
                  returned ok=false. The form is still editable; adjust
                  the mapping and try again.
                </CardContent>
              </Card>
            ) : null}
          </div>
        ) : null}
      </QueryState>

      {/* re-discover confirm dialog */}
      <Dialog
        open={reDiscoverOpen}
        onClose={() => setReDiscoverOpen(false)}
        title="Discard edits and re-run discovery?"
        description="The form has unsaved edits. Re-discovering will reset the mapping to fresh candidates from HA."
        footer={
          <>
            <Button
              variant="outline"
              onClick={() => setReDiscoverOpen(false)}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                setReDiscoverOpen(false);
                runDiscover();
              }}
              data-testid="ha-rediscover-confirm"
            >
              Discard and re-discover
            </Button>
          </>
        }
      />
    </div>
  );
}
