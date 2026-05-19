/**
 * TypeScript mirrors of the Open Crop Steering backend contracts.
 *
 * These shapes are hand-kept in sync with the FastAPI routers and
 * SQLAlchemy models under `backend/app/`. They are intentionally
 * structural (not branded) — the UI only ever reads JSON the backend
 * serialized.
 *
 * Source of truth:
 *   - backend/app/api/{health,audit,admin,approvals,rollout}.py
 *   - backend/app/models/*.py
 *   - backend/app/schemas/llm_decision.py
 */

// --- enums -------------------------------------------------------------

/** Role ladder; `RoleName` in backend/app/models/user.py. */
export type RoleName = "operator" | "cultivator" | "qap" | "admin";

/** Per-room health state; `RoomRuntime.current_state`. Lower-cased. */
export type RoomState = "healthy" | "impaired" | "critical";

/** Operational + domain audit event taxonomy; `AuditEventType`. */
export type AuditEventType =
  | "info_event"
  | "controlled_adjustment"
  | "system_warning"
  | "guardrail_rejection"
  | "formal_deviation"
  | "critical_incident"
  | "recipe_revision_created"
  | "recipe_revision_approved"
  | "runtime_adjustment_added"
  | "runtime_adjustment_reverted"
  | "user_role_changed"
  | "rollout_advanced"
  | "deviation_acknowledged"
  | "audit_export"
  | "hmac_key_rotated";

/** Event severity; `EventSeverity` in backend/app/models/event_log.py. */
export type EventSeverity = "info" | "warning" | "critical";

/** SFW pending lifecycle; `PendingStatus`. */
export type PendingStatus = "open" | "approved" | "rejected" | "expired";

/** Recipe revision lifecycle; `RecipeStatus`. */
export type RecipeStatus =
  | "draft"
  | "pending_approval"
  | "approved"
  | "superseded";

// --- health ------------------------------------------------------------

export interface ReadyzResponse {
  status: "ready" | "not_ready";
  mode: string;
  db: "ok" | "fail";
}

// --- rollout + deviations ---------------------------------------------

export interface RolloutStage {
  index: number;
  name: string;
  description: string;
}

/** One room's rollout state + advance-gate check; `_advance_check_dict`. */
export interface RolloutRoom {
  room_id: string;
  can_advance: boolean;
  current_stage: RolloutStage;
  next_stage: RolloutStage | null;
  no_open_deviations: boolean;
  open_deviation_count: number;
  sfw_rejections_7d: number;
  sfw_rejection_gate_met: boolean;
  min_days_at_stage_met: boolean;
  qap_approval_recorded: boolean;
  blockers: string[];
  paused: boolean;
  muted: boolean;
  current_state: RoomState;
}

export interface RolloutResponse {
  stages: RolloutStage[];
  rooms: RolloutRoom[];
}

/** A formal-deviation event_log row; `_deviation_dict`. */
export interface Deviation {
  id: number;
  room_id: string | null;
  occurred_at: string | null;
  severity: EventSeverity;
  summary: string;
  reason_codes: string[];
  payload: Record<string, unknown>;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
  audit_event_id: number | null;
}

export interface DeviationsResponse {
  total: number;
  deviations: Deviation[];
}

// --- approvals (SFW) ---------------------------------------------------

/** A free-form proposed change inside an LLM decision; `ProposedChange`. */
export interface ProposedChange {
  param_name: string;
  direction: "increase" | "decrease" | "no_change";
  delta: number;
  unit: string | null;
  rationale: string | null;
}

/** The strict LLM decision contract; `ocs.llm_decision.v1`. */
export interface LLMDecision {
  schema_version: "ocs.llm_decision.v1";
  snapshot_id: number;
  assessment: string;
  recommended_action_id: string | null;
  proposed_changes: ProposedChange[];
  confidence: number;
  reason_codes: string[];
  human_summary: string;
  requires_human: boolean;
}

/** List-view shape of a pending approval; `_pending_summary`. */
export interface PendingApprovalSummary {
  id: number;
  room_id: string;
  status: PendingStatus;
  summary: string;
  snapshot_id: number;
  llm_call_id: number | null;
  created_at: string | null;
  expires_at: string | null;
  decided_at: string | null;
  decided_by: string | null;
  decision_channel: string | null;
}

export interface SensorSnapshotDetail {
  id: number;
  room_id: string;
  captured_at: string | null;
  recipe_revision_id: number | null;
  cycle_day: number | null;
  rollout_stage: string | null;
  payload: Record<string, unknown>;
}

/** Detail-view shape; `_pending_detail` — adds proposal + snapshot. */
export interface PendingApprovalDetail extends PendingApprovalSummary {
  proposal: Partial<LLMDecision> & Record<string, unknown>;
  decision_chat_id: string | null;
  decision_notes: string | null;
  snapshot: SensorSnapshotDetail | null;
}

export interface ApprovalsResponse {
  total: number;
  approvals: PendingApprovalSummary[];
}

// --- audit -------------------------------------------------------------

/** A serialized audit_event row; `_row_to_dict`. */
export interface AuditEventRow {
  id: number;
  occurred_at: string | null;
  event_type: AuditEventType;
  actor_id: string | null;
  actor_role: string | null;
  room_id: string | null;
  summary: string | null;
  params: Record<string, unknown>;
  reason_codes: string[];
  recipe_revision_id: number | null;
  runtime_adjustment_id: number | null;
  command_id: number | null;
  snapshot_id: number | null;
  llm_call_id: number | null;
  key_id: number;
  prev_event_hash: string;
  hmac: string;
}

export interface AuditEventsResponse {
  total: number;
  limit: number;
  offset: number;
  events: AuditEventRow[];
}

/** Chain-verify result; `/api/audit/verify`. */
export interface AuditVerifyResponse {
  ok: boolean;
  rows_checked: number;
  first_bad_id: number | null;
  message: string;
}

// --- admin -------------------------------------------------------------

/** A user with resolved roles; `_user_dict`. */
export interface AdminUser {
  id: string;
  display_name: string;
  email: string | null;
  active: boolean;
  created_at: string | null;
  roles: RoleName[];
}

export interface AdminUsersResponse {
  users: AdminUser[];
}

/** A Telegram chat-id mapping; `_telegram_dict`. */
export interface TelegramMapping {
  chat_id: string;
  user_id: string;
  label: string | null;
  created_at: string | null;
  created_by: string | null;
  last_verified_at: string | null;
}

export interface TelegramMapResponse {
  mappings: TelegramMapping[];
}

/** Upsert payload for a user; `UserUpsert`. */
export interface UserUpsertBody {
  id: string;
  display_name: string;
  email?: string | null;
  active: boolean;
  roles: RoleName[];
}

// --- knowledge ---------------------------------------------------------

// `/api/knowledge/cultivation` returns Markdown text/plain.

// --- recipes (planner) -------------------------------------------------
//
// NOTE: a recipe-CRUD HTTP surface (`/api/recipes`) is being built by
// the backend agent in parallel; the planner page targets the contract
// below. The materialized `effective_target` view shapes are stable
// (backend/app/models/{recipe_revision,effective_target}.py).

/** One (day, param) cell in a recipe revision; `RecipeRevisionParam`. */
export interface RecipeParamCell {
  day_index: number;
  param_name: string;
  value: number;
  tolerance: number | null;
  unit: string | null;
}

/** A recipe revision header + its param grid. */
export interface RecipeRevision {
  id: number;
  room_id: string;
  version: number;
  name: string;
  cycle_day_count: number;
  status: RecipeStatus;
  created_by: string;
  created_at: string | null;
  approved_by: string | null;
  approved_at: string | null;
  notes: string | null;
  params: RecipeParamCell[];
}

/** Payload for creating a draft revision from the planner. */
export interface RecipeDraftBody {
  room_id: string;
  name: string;
  cycle_day_count: number;
  notes?: string | null;
  params: RecipeParamCell[];
}

/** An effective_target row — recipe value + active overlay deltas. */
export interface EffectiveTargetRow {
  room_id: string;
  day_index: number;
  param_name: string;
  value: number;
  tolerance: number | null;
  unit: string | null;
  recipe_revision_id: number;
}

// --- guardrails (admin, read-only) ------------------------------------
//
// NOTE: forward-looking contract — `/api/guardrails` is a backend
// deliverable. The admin guardrails page degrades gracefully (shows a
// "not yet exposed" notice) if the endpoint 404s.

/** Absolute bounds + caps for one parameter. */
export interface GuardrailBound {
  param_name: string;
  param_class: "A" | "B" | "C" | "D" | "E";
  min_value: number | null;
  max_value: number | null;
  max_cumulative_delta_24h: number | null;
  max_cumulative_delta_7d: number | null;
  cooldown_minutes: number | null;
  unit: string | null;
}

export interface GuardrailsResponse {
  bounds: GuardrailBound[];
}

// --- no-touch windows (admin, read-only) ------------------------------
//
// NOTE: forward-looking contract — `/api/no-touch-windows`. Shape
// mirrors `NoTouchWindow` in backend/app/core/no_touch.py.

export interface NoTouchWindow {
  start: string;
  end: string;
  weekdays: number[];
  label: string;
}

export interface NoTouchWindowsResponse {
  windows: NoTouchWindow[];
}

// --- rooms + HA entity registry ---------------------------------------
//
// Source of truth:
//   - backend rooms router (`/api/rooms`, `/api/rooms/ha-registry`)
//   - the RoomEquipmentMap schema (extra="forbid" — send exactly these
//     keys; unknown fields are rejected with 422).

/** A Home Assistant area; one entry of the `ha-registry` `areas` list. */
export interface HaArea {
  area_id: string;
  name: string;
}

/** A Home Assistant entity; one entry of the `ha-registry` `entities` list. */
export interface HaEntity {
  entity_id: string;
  name: string;
  domain: string;
  area: string | null;
  state: string | null;
  unit: string | null;
}

/** The `/api/rooms/ha-registry` response — areas + ~8000 entities. */
export interface HaRegistryResponse {
  areas: HaArea[];
  entities: HaEntity[];
}

/** One irrigation zone in a {@link RoomEquipmentMap}. */
export interface RoomZone {
  zone_id: string;
  valve_entity: string | null;
  pump_entity: string | null;
  vwc_sensor: string | null;
  ec_sensor: string | null;
}

/** One nutrient tank in a {@link RoomEquipmentMap}. */
export interface RoomTank {
  tank_id: string;
  ph_sensor: string | null;
  ec_sensor: string | null;
  doser_entities: string[];
}

/**
 * Per-room HA-entity → role assignment.
 *
 * The backend schema is `extra="forbid"`: a PUT body must contain
 * exactly these keys and no others. `room_id` must equal the room being
 * saved or the PUT returns 422.
 */
export interface RoomEquipmentMap {
  room_id: string;
  env_control_enabled: boolean;
  ppfd_control_enabled: boolean;
  irrigation_control_enabled: boolean;
  tank_control_enabled: boolean;
  co2_control_enabled: boolean;
  lights_switch: string | null;
  temp_sensor: string | null;
  leaf_temp_sensor: string | null;
  rh_sensor: string | null;
  co2_sensor: string | null;
  under_canopy_rh_probe: string | null;
  co2_solenoid: string | null;
  dehumidifier_entity: string | null;
  ac_entity: string | null;
  reheat_entity: string | null;
  exhaust_entity: string | null;
  cooling_capacity_entity: string | null;
  zones: RoomZone[];
  tanks: RoomTank[];
}

/** A configured room; one entry of the `/api/rooms` `rooms` list. */
export interface Room {
  room_id: string;
  display_name: string | null;
  rollout_stage: string;
  current_state: string;
  /** A {@link RoomEquipmentMap}, or `{}` when the room is unconfigured. */
  equipment_map: RoomEquipmentMap | Record<string, never>;
}

export interface RoomsResponse {
  rooms: Room[];
}

/** PUT `/api/rooms/{room_id}` body. */
export interface RoomUpsertBody {
  display_name: string | null;
  equipment_map: RoomEquipmentMap;
}

/** DELETE `/api/rooms/{room_id}` response. */
export interface RoomDeleteResponse {
  deleted: string;
}
