/** Centralized TanStack Query keys — one place to keep cache keys honest. */
export const queryKeys = {
  health: ["health"] as const,
  rollout: ["rollout"] as const,
  deviations: (roomId?: string) => ["deviations", roomId ?? "all"] as const,
  approvals: (includeDecided: boolean) =>
    ["approvals", includeDecided] as const,
  approval: (id: number) => ["approval", id] as const,
  auditEvents: (params: Record<string, unknown>) =>
    ["audit", "events", params] as const,
  auditVerify: ["audit", "verify"] as const,
  users: ["admin", "users"] as const,
  telegramMap: ["admin", "telegram-map"] as const,
  knowledge: ["knowledge", "cultivation"] as const,
  recipe: (roomId: string) => ["recipe", roomId] as const,
  guardrails: ["guardrails"] as const,
  noTouchWindows: ["no-touch-windows"] as const,
  rooms: ["admin", "rooms"] as const,
  haRegistry: ["admin", "ha-registry"] as const,
  // --- Convex-aligned facility tier ---
  buildings: ["admin", "buildings"] as const,
  convexRooms: (buildingId?: string) =>
    ["admin", "convex-rooms", buildingId ?? "all"] as const,
  locations: (roomId?: string) =>
    ["admin", "locations", roomId ?? "all"] as const,
  // Scope sensor / equipment lists by the filters we actually use so
  // a roomId-keyed invalidation stays surgical.
  sensorsByRoom: (roomId: string) =>
    ["admin", "sensors", { roomId }] as const,
  equipmentByRoom: (roomId: string) =>
    ["admin", "equipment", { roomId }] as const,
  // --- grow-recipe planner (phase-bound) ---
  growRecipes: ["cultivation", "grow-recipes"] as const,
  growRecipe: (id: string) =>
    ["cultivation", "grow-recipe", id] as const,
  recipeOverrides: (id: string) =>
    ["cultivation", "grow-recipe", id, "day-overrides"] as const,
  effectiveTargets: (id: string, day?: number) =>
    ["cultivation", "grow-recipe", id, "effective-targets", day ?? "all"] as const,
};
