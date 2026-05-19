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
};
