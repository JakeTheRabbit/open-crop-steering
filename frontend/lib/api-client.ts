/**
 * Typed fetch wrappers for the Open Crop Steering FastAPI backend.
 *
 * Request base path
 * -----------------
 * Every request is built as a ROOT-absolute URL (`/api/...`) so it does
 * not resolve against the current page — a relative URL would break on
 * any sub-route (`/admin/rooms/` + `api/rooms` -> `/admin/rooms/api/rooms`).
 *
 * The base is resolved at request time, in this order:
 *
 * 1. `NEXT_PUBLIC_API_BASE` — explicit override (dev: point the UI at
 *    `http://localhost:8099`).
 * 2. HA Ingress — the add-on UI is served below a per-session path
 *    `/api/hassio_ingress/<token>/`; requests must carry that prefix, so
 *    it is detected from `window.location.pathname`.
 * 3. Standalone — the base is the server root (`""`).
 *
 * Errors
 * ------
 * A non-2xx response throws an {@link ApiError} carrying the status and
 * the parsed `detail` (FastAPI's error field). TanStack Query surfaces
 * these to the UI.
 */

import type {
  AdminUser,
  AdminUsersResponse,
  ApprovalsResponse,
  AuditEventsResponse,
  AuditEventType,
  AuditVerifyResponse,
  Building,
  BuildingUpsertBody,
  BuildingsResponse,
  ConvexRoom,
  ConvexRoomUpsertBody,
  ConvexRoomsResponse,
  DayOverridesBulkReplaceBody,
  DayOverridesListResponse,
  DeleteResponse,
  DeviationsResponse,
  Equipment,
  EquipmentResponse,
  EquipmentUpsertBody,
  GrowRecipe,
  GrowRecipeUpdateBody,
  GrowRecipesResponse,
  GuardrailsResponse,
  HaRegistryResponse,
  Location,
  LocationUpsertBody,
  LocationsResponse,
  NoTouchWindowsResponse,
  PendingApprovalDetail,
  PendingApprovalSummary,
  ReadyzResponse,
  RecipeDraftBody,
  RecipeRevision,
  RolloutResponse,
  Room,
  RoomDeleteResponse,
  RoomUpsertBody,
  RoomsResponse,
  Sensor,
  SensorUpsertBody,
  SensorsResponse,
  TelegramMapResponse,
  UserUpsertBody,
} from "@/lib/types";

/** Explicit base override from the build env (dev / standalone). */
const CONFIGURED_BASE: string =
  (typeof process !== "undefined" &&
    process.env.NEXT_PUBLIC_API_BASE?.replace(/\/+$/, "")) ||
  "";

/** Matches the HA Ingress per-session prefix at the start of a path. */
const INGRESS_PREFIX = /^(\/api\/hassio_ingress\/[^/]+)/;

/**
 * Resolve the base every request URL is prefixed with.
 *
 * Empty string means "the server root" — requests then go to
 * `/api/...` absolutely, which is correct for standalone. Under HA
 * Ingress the live page path carries the ingress prefix; we echo it
 * back so requests stay inside the ingress tunnel.
 */
function resolveBase(): string {
  if (CONFIGURED_BASE) return CONFIGURED_BASE;
  if (typeof window !== "undefined") {
    const prefix = window.location.pathname.match(INGRESS_PREFIX)?.[1];
    if (prefix) return prefix;
  }
  return "";
}

/** An HTTP error from the backend. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown, message?: string) {
    super(message ?? `API error ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/**
 * Build a root-absolute request URL.
 *
 * The returned URL always starts with `/` (or the configured/ingress
 * base) so it resolves against the server root, never the current page.
 *
 * Exported for unit testing.
 */
export function apiUrl(
  path: string,
  query?: Record<string, string | number | boolean | undefined | null>,
): string {
  const base = resolveBase();
  const cleanPath = path.startsWith("/") ? path : `/${path}`;
  let url = `${base}${cleanPath}`;

  if (query) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== "") {
        params.append(key, String(value));
      }
    }
    const qs = params.toString();
    if (qs) url += `?${qs}`;
  }
  return url;
}

type QueryParams = Record<
  string,
  string | number | boolean | undefined | null
>;

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  query?: QueryParams;
  body?: unknown;
  signal?: AbortSignal;
}

/** Core fetch wrapper — JSON in, JSON out, {@link ApiError} on non-2xx. */
async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = "GET", query, body, signal } = opts;
  const res = await fetch(apiUrl(path, query), {
    method,
    signal,
    headers: {
      Accept: "application/json",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    let detail: unknown = null;
    try {
      const parsed = await res.json();
      detail = (parsed as { detail?: unknown })?.detail ?? parsed;
    } catch {
      detail = await res.text().catch(() => null);
    }
    throw new ApiError(
      res.status,
      detail,
      typeof detail === "string" ? detail : `API error ${res.status}`,
    );
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** Fetch a `text/plain` body (knowledge base, CSV preview). */
async function requestText(path: string, query?: QueryParams): Promise<string> {
  const res = await fetch(apiUrl(path, query), {
    headers: { Accept: "text/plain, text/markdown, */*" },
  });
  if (!res.ok) {
    throw new ApiError(res.status, await res.text().catch(() => null));
  }
  return res.text();
}

// --- the typed API surface --------------------------------------------

export const api = {
  // health
  healthz: () => request<{ status: string }>("/healthz"),
  readyz: () => request<ReadyzResponse>("/readyz"),

  // rollout + dashboard
  getRollout: (signal?: AbortSignal) =>
    request<RolloutResponse>("/api/rollout", { signal }),
  advanceRollout: (roomId: string, notes?: string) =>
    request<unknown>(`/api/rollout/${encodeURIComponent(roomId)}/advance`, {
      method: "POST",
      body: { notes: notes ?? null },
    }),

  // deviations
  getDeviations: (roomId?: string, signal?: AbortSignal) =>
    request<DeviationsResponse>("/api/deviations", {
      query: { room_id: roomId },
      signal,
    }),
  ackDeviation: (eventId: number, notes?: string) =>
    request<unknown>(`/api/deviations/${eventId}/ack`, {
      method: "POST",
      body: { notes: notes ?? null },
    }),

  // approvals (SFW)
  listApprovals: (includeDecided = false, roomId?: string) =>
    request<ApprovalsResponse>("/api/approvals", {
      query: { include_decided: includeDecided, room_id: roomId },
    }),
  getApproval: (pendingId: number) =>
    request<PendingApprovalDetail>(`/api/approvals/${pendingId}`),
  approveApproval: (pendingId: number, notes?: string) =>
    request<PendingApprovalSummary>(`/api/approvals/${pendingId}/approve`, {
      method: "POST",
      body: { notes: notes ?? null },
    }),
  rejectApproval: (pendingId: number, notes?: string) =>
    request<PendingApprovalSummary>(`/api/approvals/${pendingId}/reject`, {
      method: "POST",
      body: { notes: notes ?? null },
    }),

  // audit
  listAuditEvents: (params?: {
    limit?: number;
    offset?: number;
    event_type?: AuditEventType;
    room_id?: string;
  }) =>
    request<AuditEventsResponse>("/api/audit/events", {
      query: params as QueryParams,
    }),
  verifyAuditChain: (startId?: number, endId?: number) =>
    request<AuditVerifyResponse>("/api/audit/verify", {
      query: { start_id: startId, end_id: endId },
    }),
  /** Build the CSV-export download URL (the link the browser hits). */
  auditExportUrl: (start?: string, end?: string) =>
    apiUrl("/api/audit/export", { format: "csv", start, end }),

  // admin
  listUsers: () => request<AdminUsersResponse>("/api/admin/users"),
  upsertUser: (body: UserUpsertBody) =>
    request<AdminUser>("/api/admin/users", { method: "POST", body }),
  listTelegramMap: () =>
    request<TelegramMapResponse>("/api/admin/telegram-map"),

  // knowledge
  getCultivationKnowledge: () => requestText("/api/knowledge/cultivation"),

  // recipes (planner) — forward contract; see lib/types.ts note
  getRecipe: (roomId: string) =>
    request<RecipeRevision>(
      `/api/recipes/${encodeURIComponent(roomId)}/active`,
    ),
  createRecipeDraft: (body: RecipeDraftBody) =>
    request<RecipeRevision>("/api/recipes", { method: "POST", body }),

  // guardrails (admin, read-only) — forward contract
  getGuardrails: () => request<GuardrailsResponse>("/api/guardrails"),

  // no-touch windows (admin, read-only) — forward contract
  getNoTouchWindows: () =>
    request<NoTouchWindowsResponse>("/api/no-touch-windows"),

  // rooms + HA entity registry (admin)
  /** The HA area + entity registry (~8000 entities) — used by the picker. */
  getHaRegistry: (signal?: AbortSignal) =>
    request<HaRegistryResponse>("/api/rooms/ha-registry", { signal }),
  /** List configured rooms with their equipment maps. */
  listRooms: (signal?: AbortSignal) =>
    request<RoomsResponse>("/api/rooms", { signal }),
  /** Create or update a room; the equipment_map.room_id must match. */
  saveRoom: (roomId: string, body: RoomUpsertBody) =>
    request<Room>(`/api/rooms/${encodeURIComponent(roomId)}`, {
      method: "PUT",
      body,
    }),
  /** Delete a room and its equipment map. */
  deleteRoom: (roomId: string) =>
    request<RoomDeleteResponse>(
      `/api/rooms/${encodeURIComponent(roomId)}`,
      { method: "DELETE" },
    ),

  // --- Convex-aligned facility tier (`/api/sites/*`) ---------------
  //
  // The pass-5b admin/rooms page reads buildings + rooms + locations
  // through this surface instead of the legacy `/api/rooms`. Sensors
  // and equipment are filtered by `roomId` (and, for the picker's
  // dedupe lookup, by `externalId`) — every wrapper below is a thin
  // pass-through to keep the call sites in the page readable.

  listBuildings: (signal?: AbortSignal) =>
    request<BuildingsResponse>("/api/sites/buildings", { signal }),
  createBuilding: (body: BuildingUpsertBody) =>
    request<Building>("/api/sites/buildings", { method: "POST", body }),
  getBuilding: (buildingId: string, signal?: AbortSignal) =>
    request<Building>(
      `/api/sites/buildings/${encodeURIComponent(buildingId)}`,
      { signal },
    ),
  updateBuilding: (buildingId: string, body: BuildingUpsertBody) =>
    request<Building>(
      `/api/sites/buildings/${encodeURIComponent(buildingId)}`,
      { method: "PUT", body },
    ),
  deleteBuilding: (buildingId: string) =>
    request<DeleteResponse>(
      `/api/sites/buildings/${encodeURIComponent(buildingId)}`,
      { method: "DELETE" },
    ),

  /** List Convex-aligned rooms (optionally filtered by buildingId). */
  listConvexRooms: (buildingId?: string, signal?: AbortSignal) =>
    request<ConvexRoomsResponse>("/api/sites/rooms", {
      query: { buildingId },
      signal,
    }),
  createConvexRoom: (body: ConvexRoomUpsertBody) =>
    request<ConvexRoom>("/api/sites/rooms", { method: "POST", body }),
  getConvexRoom: (roomId: string, signal?: AbortSignal) =>
    request<ConvexRoom>(`/api/sites/rooms/${encodeURIComponent(roomId)}`, {
      signal,
    }),
  updateConvexRoom: (roomId: string, body: ConvexRoomUpsertBody) =>
    request<ConvexRoom>(`/api/sites/rooms/${encodeURIComponent(roomId)}`, {
      method: "PUT",
      body,
    }),
  deleteConvexRoom: (roomId: string) =>
    request<DeleteResponse>(`/api/sites/rooms/${encodeURIComponent(roomId)}`, {
      method: "DELETE",
    }),

  listLocations: (roomId?: string, signal?: AbortSignal) =>
    request<LocationsResponse>("/api/sites/locations", {
      query: { roomId },
      signal,
    }),
  createLocation: (body: LocationUpsertBody) =>
    request<Location>("/api/sites/locations", { method: "POST", body }),
  getLocation: (locationId: string, signal?: AbortSignal) =>
    request<Location>(
      `/api/sites/locations/${encodeURIComponent(locationId)}`,
      { signal },
    ),
  updateLocation: (locationId: string, body: LocationUpsertBody) =>
    request<Location>(
      `/api/sites/locations/${encodeURIComponent(locationId)}`,
      { method: "PUT", body },
    ),
  deleteLocation: (locationId: string) =>
    request<DeleteResponse>(
      `/api/sites/locations/${encodeURIComponent(locationId)}`,
      { method: "DELETE" },
    ),

  // --- sensors -----------------------------------------------------
  /**
   * List sensors. The picker filters by `roomId` (every room is one
   * scope) and optionally by `externalId` to check whether an HA
   * entity already has a record before POSTing a new one.
   */
  listSensors: (
    params?: { roomId?: string; type?: string; externalId?: string },
    signal?: AbortSignal,
  ) =>
    request<SensorsResponse>("/api/sensors", {
      query: params as QueryParams,
      signal,
    }),
  /**
   * Create a sensor. The backend lazy-resolves the singleton HA
   * `integrationId` when the payload sets `externalId` but leaves
   * `integrationId` undefined — so the picker never needs to know it.
   */
  createSensor: (body: SensorUpsertBody) =>
    request<Sensor>("/api/sensors", { method: "POST", body }),
  getSensor: (sensorId: string, signal?: AbortSignal) =>
    request<Sensor>(`/api/sensors/${encodeURIComponent(sensorId)}`, { signal }),
  updateSensor: (sensorId: string, body: SensorUpsertBody) =>
    request<Sensor>(`/api/sensors/${encodeURIComponent(sensorId)}`, {
      method: "PUT",
      body,
    }),
  deleteSensor: (sensorId: string) =>
    request<DeleteResponse>(`/api/sensors/${encodeURIComponent(sensorId)}`, {
      method: "DELETE",
    }),

  // --- equipment ---------------------------------------------------
  listEquipment: (
    params?: { roomId?: string; type?: string; externalId?: string },
    signal?: AbortSignal,
  ) =>
    request<EquipmentResponse>("/api/equipment", {
      query: params as QueryParams,
      signal,
    }),
  createEquipment: (body: EquipmentUpsertBody) =>
    request<Equipment>("/api/equipment", { method: "POST", body }),
  getEquipment: (equipmentId: string, signal?: AbortSignal) =>
    request<Equipment>(
      `/api/equipment/${encodeURIComponent(equipmentId)}`,
      { signal },
    ),
  updateEquipment: (equipmentId: string, body: EquipmentUpsertBody) =>
    request<Equipment>(`/api/equipment/${encodeURIComponent(equipmentId)}`, {
      method: "PUT",
      body,
    }),
  deleteEquipment: (equipmentId: string) =>
    request<DeleteResponse>(
      `/api/equipment/${encodeURIComponent(equipmentId)}`,
      { method: "DELETE" },
    ),

  // --- grow recipes (phase-bound planner) -------------------------
  //
  // P1 added read wrappers; P2 adds the mutations needed by the
  // batched-draft save flow (recipe header PUT + override bulk
  // PUT) and switches `listDayOverrides` to the dedicated GET
  // endpoint the backend added in parallel.

  listGrowRecipes: (signal?: AbortSignal) =>
    request<GrowRecipesResponse>("/api/cultivation/grow-recipes", {
      signal,
    }),
  getGrowRecipe: (recipeId: string, signal?: AbortSignal) =>
    request<GrowRecipe>(
      `/api/cultivation/grow-recipes/${encodeURIComponent(recipeId)}`,
      { signal },
    ),
  /**
   * Replace a grow recipe's mutable fields.
   *
   * Mirrors `PUT /api/cultivation/grow-recipes/{id}`. The body must be
   * the same camelCase wire shape as `GrowRecipeCreate` (alias-keyed,
   * matching `model_dump(by_alias=True)` on the Pydantic schema).
   *
   * Returns the server's view of the updated recipe — the planner's
   * save flow dispatches `LOAD_FROM_SERVER` with this so `savedSnapshot`
   * resets cleanly.
   */
  updateGrowRecipe: (recipeId: string, body: GrowRecipeUpdateBody) =>
    request<GrowRecipe>(
      `/api/cultivation/grow-recipes/${encodeURIComponent(recipeId)}`,
      { method: "PUT", body },
    ),
  /**
   * Read the override rows for a recipe.
   *
   * Hits the dedicated GET endpoint added in the P2 backend dispatch:
   * `GET /api/cultivation/grow-recipes/{id}/day-overrides` returns
   * `{overrides: DayOverrideRead[]}` with full `id` / `orgId` /
   * timestamps populated. The planner UI uses the rich metadata to
   * cross-check after save and for the day inspector's pin list.
   */
  listDayOverrides: async (recipeId: string, signal?: AbortSignal) => {
    const response = await request<DayOverridesListResponse>(
      `/api/cultivation/grow-recipes/${encodeURIComponent(recipeId)}/day-overrides`,
      { signal },
    );
    return response.overrides;
  },
  /**
   * Bulk-replace every override row for a recipe atomically.
   *
   * Mirrors `PUT /api/cultivation/grow-recipes/{id}/day-overrides` —
   * the server deletes every existing override and inserts the new
   * set in one transaction. The body is the same shape the GET
   * endpoint above echoes (minus `id` / `orgId` / timestamps).
   *
   * The handler returns the bulk-replace echo body which carries the
   * freshly-persisted rows; the save flow re-runs `listDayOverrides`
   * after success to seed `savedSnapshot`, so we don't strictly need
   * the echo — but we surface it as the unknown JSON the handler
   * sends back so the test mocks have something concrete to assert.
   */
  replaceDayOverrides: (
    recipeId: string,
    body: DayOverridesBulkReplaceBody,
  ) =>
    request<unknown>(
      `/api/cultivation/grow-recipes/${encodeURIComponent(recipeId)}/day-overrides`,
      { method: "PUT", body },
    ),
};

export type Api = typeof api;
