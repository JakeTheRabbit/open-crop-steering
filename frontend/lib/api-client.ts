/**
 * Typed fetch wrappers for the Open Crop Steering FastAPI backend.
 *
 * Ingress base path
 * -----------------
 * The add-on is served through Home Assistant Ingress, which prefixes
 * every request with a per-session path like
 * `/api/hassio_ingress/<token>/`. The exported bundle cannot know that
 * prefix at build time, so this client builds **relative** URLs:
 * `apiUrl("/api/audit/events")` resolves against `document.baseURI`,
 * which the browser sets from the page's own URL (the ingress prefix).
 *
 * `NEXT_PUBLIC_API_BASE` may override the base for standalone / dev use
 * (e.g. point the UI at `http://localhost:8099`); it defaults to `""`,
 * i.e. "relative to wherever this page is served from".
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
  DeviationsResponse,
  GuardrailsResponse,
  NoTouchWindowsResponse,
  HaRegistryResponse,
  PendingApprovalDetail,
  PendingApprovalSummary,
  ReadyzResponse,
  RecipeDraftBody,
  RecipeRevision,
  RolloutResponse,
  Room,
  RoomDeleteResponse,
  RoomsResponse,
  RoomUpsertBody,
  TelegramMapResponse,
  UserUpsertBody,
} from "@/lib/types";

/** Configured base. Empty string = relative to the current document. */
const API_BASE: string =
  (typeof process !== "undefined" &&
    process.env.NEXT_PUBLIC_API_BASE?.replace(/\/+$/, "")) ||
  "";

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
 * Build a request URL.
 *
 * With an empty {@link API_BASE} the returned value is a relative path
 * with the leading slash stripped (`"api/audit/events"`), so the browser
 * resolves it against the ingress-prefixed `document.baseURI` rather
 * than the server root. With an explicit base, the path is appended
 * absolutely.
 *
 * Exported for unit testing.
 */
export function apiUrl(
  path: string,
  query?: Record<string, string | number | boolean | undefined | null>,
): string {
  const cleanPath = path.startsWith("/") ? path.slice(1) : path;
  let url = API_BASE ? `${API_BASE}/${cleanPath}` : cleanPath;

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
};

export type Api = typeof api;
