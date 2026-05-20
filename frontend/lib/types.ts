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
  /** Area display name (shown to the user). */
  area: string | null;
  /** Area id — the stable key the area filter matches on. */
  area_id: string | null;
  state: string | null;
  unit: string | null;
}

/** The `/api/rooms/ha-registry` response — areas + ~8000 entities. */
export interface HaRegistryResponse {
  areas: HaArea[];
  entities: HaEntity[];
}

/**
 * One irrigation zone (a grow row / bench) in a {@link RoomEquipmentMap}.
 *
 * The zone owns its valve(s) and substrate probes; the pump and the
 * mainline / manifold valves are room-level (a shot opens the shared
 * supply and the zone valve together).
 */
export interface RoomZone {
  zone_id: string;
  valve_entities: string[];
  vwc_sensors: string[];
  ec_sensors: string[];
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
  // sensors — a room can have several of each (multiple temp probes,
  // CO2 heads, substrate VWC/EC sensors, …), so each role is a list
  temp_sensors: string[];
  rh_sensors: string[];
  co2_sensors: string[];
  leaf_temp_sensors: string[];
  under_canopy_rh_probes: string[];
  vwc_sensors: string[];
  ec_sensors: string[];
  ppfd_sensors: string[];
  dli_sensors: string[];
  pm1_sensors: string[];
  pm25_sensors: string[];
  pm4_sensors: string[];
  pm10_sensors: string[];
  // the cooling-headroom source stays a single reference entity
  cooling_capacity_entity: string | null;
  // actuators — a room can have several of each (two AC units, multiple
  // grow-light circuits, …), so each role is a list of entity ids
  light_entities: string[];
  ac_entities: string[];
  dehumidifier_entities: string[];
  reheat_entities: string[];
  exhaust_entities: string[];
  co2_solenoid_entities: string[];
  // room-level irrigation supply — the pump(s) + mainline / manifold
  // valve(s) that open for every zone's shot
  irrigation_pump_entities: string[];
  mainline_valve_entities: string[];
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

// --- Convex-aligned sites / sensors / equipment -----------------------
//
// Source of truth: the FastAPI Pydantic schemas under
// `backend/app/schemas/{sites,sensors,equipment}.py`. Wire format is
// camelCase, timestamps are epoch milliseconds. These shapes are
// added alongside (not in place of) the legacy `Room` /
// `RoomEquipmentMap` types above so consumers can be migrated one at
// a time.

/** Convex-aligned `buildings` row; `BuildingRead` (camelCase wire). */
export interface Building {
  id: string;
  orgId: string;
  name: string;
  address: string | null;
  stories: string[] | null;
  width: number | null;
  height: number | null;
  length: number | null;
  createdAt: number;
  updatedAt: number;
}

export interface BuildingsResponse {
  buildings: Building[];
}

/** Payload for creating / updating a building. */
export interface BuildingUpsertBody {
  name: string;
  address?: string | null;
  stories?: string[] | null;
  width?: number | null;
  height?: number | null;
  length?: number | null;
}

/** Convex-aligned `rooms` row — distinct from the legacy {@link Room}. */
export interface ConvexRoom {
  id: string;
  orgId: string;
  buildingId: string;
  name: string;
  purpose: string | null;
  story: string | null;
  positionX: number | null;
  positionY: number | null;
  width: number | null;
  height: number | null;
  length: number | null;
  area: number | null;
  type: string | null;
  createdAt: number;
  updatedAt: number;
}

export interface ConvexRoomsResponse {
  rooms: ConvexRoom[];
}

/** Payload for creating / updating a Convex-aligned room. */
export interface ConvexRoomUpsertBody {
  buildingId: string;
  name: string;
  purpose?: string | null;
  story?: string | null;
  positionX?: number | null;
  positionY?: number | null;
  width?: number | null;
  height?: number | null;
  length?: number | null;
  area?: number | null;
  type?: string | null;
}

/** Convex-aligned `locations` row. */
export interface Location {
  id: string;
  orgId: string;
  roomId: string;
  label: string;
  path: string | null;
  capacity: number | null;
  story: string | null;
  positionX: number | null;
  positionY: number | null;
  width: number | null;
  height: number | null;
  length: number | null;
  area: number | null;
  type: string | null;
  createdAt: number;
  updatedAt: number;
}

export interface LocationsResponse {
  locations: Location[];
}

export interface LocationUpsertBody {
  roomId: string;
  label: string;
  path?: string | null;
  capacity?: number | null;
  story?: string | null;
  positionX?: number | null;
  positionY?: number | null;
  width?: number | null;
  height?: number | null;
  length?: number | null;
  area?: number | null;
  type?: string | null;
}

/** The Convex `sensors.type` literal union — 13 entries. */
export type SensorType =
  | "temperature"
  | "humidity"
  | "co2"
  | "ph"
  | "ec"
  | "vpd"
  | "light"
  | "pressure"
  | "moisture"
  | "flow"
  | "level"
  | "motion"
  | "air_quality";

export type SensorStatus =
  | "active"
  | "inactive"
  | "maintenance"
  | "error"
  | "calibrating";

/** Convex-aligned `sensors` row. */
export interface Sensor {
  id: string;
  orgId: string;
  name: string;
  code: string;
  type: SensorType;
  roomId: string | null;
  locationId: string | null;
  batchId: string | null;
  manufacturer: string | null;
  model: string | null;
  serialNumber: string | null;
  dataUnit: string;
  minValue: number | null;
  maxValue: number | null;
  accuracy: number | null;
  resolution: number | null;
  integrationId: string | null;
  externalId: string | null;
  pollInterval: number | null;
  status: SensorStatus;
  lastReadingTime: number | null;
  lastReadingValue: number | null;
  batteryLevel: number | null;
  signalStrength: number | null;
  lastCalibration: number | null;
  nextCalibration: number | null;
  calibrationOffset: number | null;
  calibrationNotes: string | null;
  alerts: unknown[] | null;
  notes: string | null;
  tags: string[] | null;
  isActive: boolean;
  createdAt: number;
  updatedAt: number;
}

export interface SensorsResponse {
  sensors: Sensor[];
}

/**
 * Payload for creating / updating a {@link Sensor}.
 *
 * The backend lazy-resolves `integrationId` when it is omitted but
 * `externalId` is set (the single per-install Home Assistant
 * integration). Other fields default to their respective `null` /
 * `undefined` per the Pydantic schema.
 */
export interface SensorUpsertBody {
  name: string;
  code: string;
  type: SensorType;
  dataUnit: string;
  status: SensorStatus;
  isActive: boolean;
  roomId?: string | null;
  locationId?: string | null;
  batchId?: string | null;
  manufacturer?: string | null;
  model?: string | null;
  serialNumber?: string | null;
  minValue?: number | null;
  maxValue?: number | null;
  accuracy?: number | null;
  resolution?: number | null;
  integrationId?: string | null;
  externalId?: string | null;
  pollInterval?: number | null;
  lastReadingTime?: number | null;
  lastReadingValue?: number | null;
  batteryLevel?: number | null;
  signalStrength?: number | null;
  lastCalibration?: number | null;
  nextCalibration?: number | null;
  calibrationOffset?: number | null;
  calibrationNotes?: string | null;
  alerts?: unknown[] | null;
  notes?: string | null;
  tags?: string[] | null;
}

/** Convex-aligned `equipment.type` literal union. */
export type EquipmentType =
  | "hvac"
  | "lighting"
  | "irrigation"
  | "extraction"
  | "processing"
  | "monitoring"
  | "other";

export type EquipmentStatus =
  | "operational"
  | "maintenance"
  | "repair"
  | "retired";

/** Convex-aligned `equipment` row. */
export interface Equipment {
  id: string;
  orgId: string;
  name: string;
  code: string;
  type: EquipmentType;
  manufacturer: string | null;
  model: string | null;
  serialNumber: string | null;
  purchaseDate: number | null;
  warrantyExpires: number | null;
  roomId: string | null;
  locationId: string | null;
  integrationId: string | null;
  externalId: string | null;
  status: EquipmentStatus;
  lastMaintenance: number | null;
  nextMaintenance: number | null;
  maintenanceInterval: number | null;
  maintenanceNotes: string | null;
  notes: string | null;
  tags: string[] | null;
  isActive: boolean;
  createdAt: number;
  updatedAt: number;
}

export interface EquipmentResponse {
  equipment: Equipment[];
}

export interface EquipmentUpsertBody {
  name: string;
  code: string;
  type: EquipmentType;
  status: EquipmentStatus;
  isActive: boolean;
  manufacturer?: string | null;
  model?: string | null;
  serialNumber?: string | null;
  purchaseDate?: number | null;
  warrantyExpires?: number | null;
  roomId?: string | null;
  locationId?: string | null;
  integrationId?: string | null;
  externalId?: string | null;
  lastMaintenance?: number | null;
  nextMaintenance?: number | null;
  maintenanceInterval?: number | null;
  maintenanceNotes?: string | null;
  notes?: string | null;
  tags?: string[] | null;
}

/** Convex-aligned `sensorIntegrations` row (subset OCS uses). */
export interface SensorIntegration {
  id: string;
  orgId: string;
  name: string;
  type:
    | "home_assistant"
    | "arduino"
    | "raspberry_pi"
    | "mqtt"
    | "http_webhook"
    | "modbus"
    | "custom";
  connectionUrl: string | null;
  apiKey: string | null;
  username: string | null;
  password: string | null;
  mqttTopic: string | null;
  status: "connected" | "disconnected" | "error" | "configuring";
  lastConnected: number | null;
  lastError: string | null;
  syncEnabled: boolean;
  syncInterval: number;
  lastSync: number | null;
  notes: string | null;
  createdAt: number;
  updatedAt: number;
}

export interface SensorIntegrationsResponse {
  sensorIntegrations: SensorIntegration[];
}

/** Generic `{deleted: id}` envelope returned by every DELETE handler. */
export interface DeleteResponse {
  deleted: string;
}

// --- Grow recipes (phase-bound planner) -------------------------------
//
// Source of truth:
//   - backend/app/schemas/cultivation.py — GrowRecipeRead etc.
//   - backend/app/schemas/recipe_overrides.py — DayOverrideRead,
//     EffectiveTarget.
//
// Wire format is camelCase (every Convex-aligned schema sets
// populate_by_name=True; output uses field aliases). Timestamps are
// epoch milliseconds, matching the existing Building / ConvexRoom
// pattern. New (P1) shapes feed the phase-strip timeline; the editor +
// resolver land in later passes.

/** A `{min, max}` numeric range — the shared Convex nested shape. */
export interface MinMaxRange {
  min: number;
  max: number;
}

/** One phase's lights-on / lights-off split (hours). */
export interface LightCycle {
  hoursOn: number;
  hoursOff: number;
}

/**
 * Per-phase climate band — each entry is a `MinMaxRange` or absent.
 *
 * Complementary to the flat `targets` map: the structured ranges feed
 * the Convex `environmentalTargets` doc, the flat map is the OCS
 * extension the resolver consumes for per-param overrides.
 */
export interface EnvironmentalTargets {
  temperature?: MinMaxRange | null;
  humidity?: MinMaxRange | null;
  co2?: MinMaxRange | null;
  vpd?: MinMaxRange | null;
}

/** One item on a phase's nutrient list. */
export interface NutrientItem {
  name: string;
  dosage: string;
  unit: string;
}

/** Per-phase nutrient programme. */
export interface PhaseNutrients {
  ec?: MinMaxRange | null;
  ph?: MinMaxRange | null;
  feedingSchedule?: string | null;
  nutrients?: NutrientItem[] | null;
}

/** One scheduled task template attached to a phase. */
export interface PhaseTask {
  taskTemplate: string;
  daysFromPhaseStartToCreate: number;
  dueDaysAfterCreation?: number | null;
}

/**
 * One `(value, tolerance, unit)` cell on a phase's flat target map.
 *
 * The shape matches `DayOverride` minus `day` / `paramName` (which are
 * the map address), so a per-day override is a literal shadow of the
 * phase default at that cell.
 */
export interface PhaseTargetSpec {
  value: number;
  tolerance?: number | null;
  unit?: string | null;
}

/**
 * One phase in a `GrowRecipe`.
 *
 * Convex stores `order + durationDays`; the backend's
 * `GrowRecipeBase._populate_phase_day_boundaries` post-validator stamps
 * 1-based `startDay` / `endDay` derived from the prefix sum of
 * `durationDays`. Those derived fields are emitted on the wire so the
 * planner UI does not have to recompute them.
 */
export interface GrowRecipePhase {
  phaseName: string;
  durationDays: number;
  order: number;
  lightCycle?: LightCycle | null;
  environmentalTargets?: EnvironmentalTargets | null;
  nutrients?: PhaseNutrients | null;
  phaseTasks?: PhaseTask[] | null;
  /** OCS-extension flat `paramName -> {value, tolerance?, unit?}` map. */
  targets?: Record<string, PhaseTargetSpec> | null;
  /** Derived 1-based day boundary (populated by the backend resolver). */
  startDay?: number | null;
  endDay?: number | null;
}

/**
 * A persisted grow recipe — header + phases.
 *
 * Per-day overrides are persisted as a separate `GrowRecipeDayOverride`
 * collection and reach the UI either through the bulk PUT endpoint's
 * echo body or, in later passes, through `GET /effective-targets`.
 * `GrowRecipeRead` itself does NOT denormalize them.
 */
export interface GrowRecipe {
  id: string;
  orgId: string;
  name: string;
  recipeType: string[];
  description?: string | null;
  version?: number | null;
  isActive: boolean;
  estimatedTotalDurationDays?: number | null;
  phases: GrowRecipePhase[];
  genetics?: string | null;
  createdBy?: string | null;
  lastModifiedBy?: string | null;
  createdAt: number;
  updatedAt: number;
}

/** `GET /api/cultivation/grow-recipes` envelope. */
export interface GrowRecipesResponse {
  growRecipes: GrowRecipe[];
}

/**
 * One persisted day-override row.
 *
 * Each row is one `(recipeId, day, paramName) -> {value, tolerance?,
 * unit?}` cell that overrides the corresponding phase default.
 */
export interface DayOverride {
  id: string;
  orgId: string;
  recipeId: string;
  day: number;
  paramName: string;
  value: number;
  tolerance?: number | null;
  unit?: string | null;
  createdAt: number;
  updatedAt: number;
}

/** `EffectiveTarget.source` discriminator. */
export type EffectiveTargetSource = "phase_default" | "day_override";

/**
 * One resolved `(day, paramName) -> target` cell.
 *
 * Produced by `GET /api/cultivation/grow-recipes/{id}/effective-targets`
 * (with or without a `day=N` filter). The `source` field tells the
 * planner UI whether the cell came from the phase default map or from
 * an override row shadowing it.
 */
export interface EffectiveTarget {
  day: number;
  paramName: string;
  value: number;
  tolerance?: number | null;
  unit?: string | null;
  phaseName: string;
  phaseOrder: number;
  source: EffectiveTargetSource;
}

/** `GET /api/cultivation/grow-recipes/{id}/effective-targets` envelope. */
export interface EffectiveTargetsResponse {
  effectiveTargets: EffectiveTarget[];
  cycleDayCount: number;
}

/**
 * `GET /api/cultivation/grow-recipes/{id}/day-overrides` envelope.
 *
 * P2 of the planner redesign reads this endpoint (added in parallel by
 * the backend pass) instead of filtering the dense effective-targets
 * grid the way P1 did. The body shape matches the bulk-replace PUT's
 * echo response so the same parser handles both.
 */
export interface DayOverridesListResponse {
  overrides: DayOverride[];
}

/**
 * Body for `PUT /api/cultivation/grow-recipes/{id}/day-overrides`.
 *
 * Bulk-replaces every override row for the recipe atomically — see
 * the FastAPI handler in `app/api/cultivation.py`. Each entry is the
 * input form of one override (no `id`/`orgId`/timestamps; the path
 * carries the `recipeId`).
 */
export interface DayOverrideInput {
  day: number;
  paramName: string;
  value: number;
  tolerance?: number | null;
  unit?: string | null;
}

export interface DayOverridesBulkReplaceBody {
  overrides: DayOverrideInput[];
}

/**
 * Body for `PUT /api/cultivation/grow-recipes/{id}` — the recipe
 * update endpoint.
 *
 * Mirrors `GrowRecipeCreate` from `backend/app/schemas/cultivation.py`:
 * every mutable field of `GrowRecipeBase` plus `genetics`. Fields are
 * camelCase on the wire; `populate_by_name=True` means snake_case
 * works too, but matching the AiGrowApp wire shape avoids confusion.
 */
export interface GrowRecipeUpdateBody {
  name: string;
  recipeType: string[];
  description?: string | null;
  version?: number | null;
  isActive: boolean;
  estimatedTotalDurationDays?: number | null;
  phases: GrowRecipePhase[];
  genetics?: string | null;
  createdBy?: string | null;
  lastModifiedBy?: string | null;
}
