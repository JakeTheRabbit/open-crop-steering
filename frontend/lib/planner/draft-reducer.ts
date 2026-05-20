/**
 * Pure draft reducer for the recipe planner (P2).
 *
 * The planner holds the recipe as a batched, validated DRAFT — every
 * edit dispatches an {@link Action} through this reducer rather than
 * round-tripping a mutation to the backend. Save is explicit (see
 * `<RecipeSaveBar>`'s two-PUT flow); discard rewinds to
 * {@link DraftState.savedSnapshot}.
 *
 * The reducer is side-effect free and fully unit-testable in isolation
 * (no React, no fetch). The hosting `<RecipeDraftProvider>` wires it
 * into a `useReducer` + the undo/redo helpers.
 *
 * After every mutating action the reducer:
 *
 *  1. Snapshots the prior state onto `undoStack` (capped at 50) and
 *     clears `redoStack`.
 *  2. Re-stamps `start_day` / `end_day` on every phase by prefix-sum of
 *     `duration_days` in `order`. This is the TS port of
 *     `GrowRecipeBase._populate_phase_day_boundaries` from
 *     `backend/app/schemas/cultivation.py` — the same contract the
 *     resolver / validator rely on.
 *  3. Re-normalises `order` to a dense 1..N sequence so structural
 *     actions (split / insert / delete) leave a wire-format-clean
 *     recipe.
 *
 * The validator runs separately (memoised on a `(recipe, overrides)`
 * content hash) so the reducer stays cheap and predictable.
 */

import type {
  DayOverride,
  GrowRecipe,
  GrowRecipePhase,
  PhaseTargetSpec,
} from "@/lib/types";

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------

/**
 * One point-in-time snapshot of the draft.
 *
 * Used both for the undo stack and for `savedSnapshot` (the
 * last-server-confirmed state used by `dirtyCount` and discard).
 */
export interface DraftSnapshot {
  recipe: GrowRecipe;
  overrides: DayOverride[];
}

export interface DraftState {
  recipe: GrowRecipe;
  overrides: DayOverride[];
  /** Last server-confirmed state — `dirtyCount` and discard diff against this. */
  savedSnapshot: DraftSnapshot;
  /** Pre-action snapshots, newest last. Capped at 50. */
  undoStack: DraftSnapshot[];
  /** Snapshots popped by UNDO, oldest last; cleared on a non-undo/redo action. */
  redoStack: DraftSnapshot[];
}

/** Maximum number of pre-action snapshots retained for undo. */
export const UNDO_STACK_LIMIT = 50;

// ---------------------------------------------------------------------------
// Action union
// ---------------------------------------------------------------------------

/**
 * Every action the planner can dispatch.
 *
 * P3/P4 action types are listed here as comments only — leaving them as
 * future-compatible *names* would force TS to require case arms, which
 * adds noise. They'll be added in their own pass.
 */
export type Action =
  | { type: "RENAME_RECIPE"; name: string }
  | { type: "RENAME_PHASE"; phaseIndex: number; name: string }
  | { type: "SET_PHASE_DURATION"; phaseIndex: number; days: number }
  | {
      type: "SET_PHASE_LIGHT_CYCLE";
      phaseIndex: number;
      hoursOn: number;
      hoursOff: number;
    }
  | {
      type: "SET_PHASE_ENV_BAND";
      phaseIndex: number;
      key: "temperature" | "humidity" | "co2" | "vpd";
      min: number;
      max: number;
    }
  | {
      type: "SET_PHASE_TARGET";
      phaseIndex: number;
      paramName: string;
      value: number;
      tolerance: number | null;
      unit: string | null;
    }
  | {
      type: "ADD_PHASE_TARGET";
      phaseIndex: number;
      paramName: string;
      value: number;
      tolerance: number | null;
      unit: string | null;
    }
  | { type: "REMOVE_PHASE_TARGET"; phaseIndex: number; paramName: string }
  | { type: "DELETE_PHASE"; phaseIndex: number }
  | { type: "ADD_PHASE_BEFORE"; phaseIndex: number; name: string; days: number }
  | { type: "ADD_PHASE_AFTER"; phaseIndex: number; name: string; days: number }
  | { type: "UNDO" }
  | { type: "REDO" }
  | { type: "RESET_TO_SAVED" }
  | {
      type: "LOAD_FROM_SERVER";
      recipe: GrowRecipe;
      overrides: DayOverride[];
    };

// ---------------------------------------------------------------------------
// Helpers — phase boundary re-stamping
// ---------------------------------------------------------------------------

/**
 * TS port of `GrowRecipeBase._populate_phase_day_boundaries`.
 *
 * Sorts phases by `order`, re-stamps `startDay` / `endDay` from the
 * prefix-sum of `durationDays`, and normalises `order` to a dense
 * 1..N integer sequence (so structural actions like SPLIT_PHASE that
 * insert at `prev.order + 0.5` flatten back to integers).
 *
 * Returns a fresh array — the original is not mutated.
 */
export function stampPhaseBoundaries(
  phases: GrowRecipePhase[],
): GrowRecipePhase[] {
  // Sort by order first; renormalise after so the original order field
  // doesn't disappear from view if anyone inspects the intermediate.
  const sorted = [...phases].sort((a, b) => a.order - b.order);
  let cursor = 1;
  return sorted.map((phase, i) => {
    const length = Math.max(1, Math.floor(phase.durationDays));
    const startDay = cursor;
    const endDay = cursor + length - 1;
    cursor += length;
    return {
      ...phase,
      // Coerce duration_days to an integer count — the backend rejects
      // fractional durations.
      durationDays: length,
      order: i + 1,
      startDay,
      endDay,
    };
  });
}

/**
 * Build a deep clone of the state's mutable fields.
 *
 * `structuredClone` is built into Node 17+ and every modern browser —
 * no polyfill needed.
 */
function snapshot(state: DraftState): DraftSnapshot {
  return {
    recipe: structuredClone(state.recipe),
    overrides: structuredClone(state.overrides),
  };
}

/**
 * Push a snapshot onto `undoStack`, dropping the oldest entry when the
 * stack would exceed {@link UNDO_STACK_LIMIT}.
 */
function pushUndo(
  stack: DraftSnapshot[],
  next: DraftSnapshot,
): DraftSnapshot[] {
  if (stack.length >= UNDO_STACK_LIMIT) {
    return [...stack.slice(stack.length - UNDO_STACK_LIMIT + 1), next];
  }
  return [...stack, next];
}

// ---------------------------------------------------------------------------
// Phase mutators
// ---------------------------------------------------------------------------

/**
 * Apply a mutation to one phase, returning a fresh phases array with
 * boundaries re-stamped and orders normalised.
 *
 * The `mutate` callback receives a SHALLOW clone of the target phase
 * (with `targets` already cloned) — safe to mutate in place. Callers
 * that don't need to touch the phase still pay the clone cost; it's
 * cheap (one object) and keeps the contract honest.
 */
function withPhase(
  phases: GrowRecipePhase[],
  phaseIndex: number,
  mutate: (phase: GrowRecipePhase) => GrowRecipePhase,
): GrowRecipePhase[] {
  if (phaseIndex < 0 || phaseIndex >= phases.length) return phases;
  const next = phases.map((phase, i) => {
    if (i !== phaseIndex) return phase;
    const cloned: GrowRecipePhase = {
      ...phase,
      targets: phase.targets ? { ...phase.targets } : phase.targets,
      lightCycle: phase.lightCycle ? { ...phase.lightCycle } : phase.lightCycle,
      environmentalTargets: phase.environmentalTargets
        ? { ...phase.environmentalTargets }
        : phase.environmentalTargets,
    };
    return mutate(cloned);
  });
  return stampPhaseBoundaries(next);
}

/** Build a fresh phase with sensible defaults — used by ADD_PHASE_*. */
function freshPhase(name: string, days: number, order: number): GrowRecipePhase {
  return {
    phaseName: name,
    durationDays: Math.max(1, Math.floor(days)),
    order,
    targets: {},
  };
}

// ---------------------------------------------------------------------------
// Reducer
// ---------------------------------------------------------------------------

/**
 * Build a fresh state from a server-confirmed `(recipe, overrides)`
 * pair. Both `savedSnapshot` and `recipe` point to independent deep
 * clones so a draft edit can't leak into the saved baseline.
 *
 * Used both by the initial `useReducer` seed and by `LOAD_FROM_SERVER`.
 */
export function initDraftState(
  recipe: GrowRecipe,
  overrides: DayOverride[],
): DraftState {
  const cleanRecipe: GrowRecipe = {
    ...structuredClone(recipe),
    phases: stampPhaseBoundaries(recipe.phases),
  };
  const cleanOverrides = structuredClone(overrides);
  return {
    recipe: cleanRecipe,
    overrides: cleanOverrides,
    savedSnapshot: {
      recipe: structuredClone(cleanRecipe),
      overrides: structuredClone(cleanOverrides),
    },
    undoStack: [],
    redoStack: [],
  };
}

/**
 * The reducer. Pure (no `Date.now()`, no network, no console).
 *
 * Unknown action types fall through to `state` so a future action added
 * elsewhere doesn't crash the existing reducer.
 */
export function draftReducer(state: DraftState, action: Action): DraftState {
  switch (action.type) {
    // -----------------------------------------------------------------
    // Undo / redo / load — these don't push to undoStack themselves.
    // -----------------------------------------------------------------
    case "UNDO": {
      const prior = state.undoStack[state.undoStack.length - 1];
      if (!prior) return state;
      const restored: DraftSnapshot = {
        recipe: structuredClone(prior.recipe),
        overrides: structuredClone(prior.overrides),
      };
      return {
        ...state,
        recipe: restored.recipe,
        overrides: restored.overrides,
        undoStack: state.undoStack.slice(0, -1),
        // Push the current state onto redo so REDO can recover it.
        redoStack: [...state.redoStack, snapshot(state)],
      };
    }
    case "REDO": {
      const next = state.redoStack[state.redoStack.length - 1];
      if (!next) return state;
      const restored: DraftSnapshot = {
        recipe: structuredClone(next.recipe),
        overrides: structuredClone(next.overrides),
      };
      return {
        ...state,
        recipe: restored.recipe,
        overrides: restored.overrides,
        redoStack: state.redoStack.slice(0, -1),
        undoStack: [...state.undoStack, snapshot(state)],
      };
    }
    case "RESET_TO_SAVED": {
      // Discard every edit + every undo/redo entry. The save flow also
      // dispatches LOAD_FROM_SERVER after a successful save which has
      // the same effect plus refreshing `savedSnapshot`.
      return {
        ...state,
        recipe: structuredClone(state.savedSnapshot.recipe),
        overrides: structuredClone(state.savedSnapshot.overrides),
        undoStack: [],
        redoStack: [],
      };
    }
    case "LOAD_FROM_SERVER": {
      return initDraftState(action.recipe, action.overrides);
    }

    // -----------------------------------------------------------------
    // Mutating actions — every one pushes the prior state to undo and
    // clears redo.
    // -----------------------------------------------------------------
    case "RENAME_RECIPE": {
      if (action.name === state.recipe.name) return state;
      return finalise(state, {
        ...state,
        recipe: { ...state.recipe, name: action.name },
      });
    }

    case "RENAME_PHASE": {
      const phases = withPhase(state.recipe.phases, action.phaseIndex, (p) => ({
        ...p,
        phaseName: action.name,
      }));
      return finalise(state, {
        ...state,
        recipe: { ...state.recipe, phases },
      });
    }

    case "SET_PHASE_DURATION": {
      const days = Math.max(1, Math.floor(action.days));
      const phases = withPhase(state.recipe.phases, action.phaseIndex, (p) => ({
        ...p,
        durationDays: days,
      }));
      return finalise(state, {
        ...state,
        recipe: { ...state.recipe, phases },
      });
    }

    case "SET_PHASE_LIGHT_CYCLE": {
      const phases = withPhase(state.recipe.phases, action.phaseIndex, (p) => ({
        ...p,
        lightCycle: { hoursOn: action.hoursOn, hoursOff: action.hoursOff },
      }));
      return finalise(state, {
        ...state,
        recipe: { ...state.recipe, phases },
      });
    }

    case "SET_PHASE_ENV_BAND": {
      const phases = withPhase(state.recipe.phases, action.phaseIndex, (p) => {
        const env = { ...(p.environmentalTargets ?? {}) };
        env[action.key] = { min: action.min, max: action.max };
        return { ...p, environmentalTargets: env };
      });
      return finalise(state, {
        ...state,
        recipe: { ...state.recipe, phases },
      });
    }

    case "SET_PHASE_TARGET":
    case "ADD_PHASE_TARGET": {
      const spec: PhaseTargetSpec = {
        value: action.value,
        tolerance: action.tolerance,
        unit: action.unit,
      };
      const phases = withPhase(state.recipe.phases, action.phaseIndex, (p) => {
        const targets = { ...(p.targets ?? {}) };
        targets[action.paramName] = spec;
        return { ...p, targets };
      });
      return finalise(state, {
        ...state,
        recipe: { ...state.recipe, phases },
      });
    }

    case "REMOVE_PHASE_TARGET": {
      const phases = withPhase(state.recipe.phases, action.phaseIndex, (p) => {
        if (!p.targets || !(action.paramName in p.targets)) return p;
        const targets = { ...p.targets };
        delete targets[action.paramName];
        return { ...p, targets };
      });
      return finalise(state, {
        ...state,
        recipe: { ...state.recipe, phases },
      });
    }

    case "DELETE_PHASE": {
      // Merge the phase's days into the previous phase, growing its
      // duration. If the deleted phase is index 0, merge its days into
      // what becomes the new first phase (i.e. the phase formerly at
      // index 1). If there's only one phase, refuse — the recipe must
      // have at least one.
      const phases = state.recipe.phases;
      if (phases.length <= 1) return state;
      const target = phases[action.phaseIndex];
      if (!target) return state;
      const mergeInto =
        action.phaseIndex === 0 ? action.phaseIndex + 1 : action.phaseIndex - 1;
      const targetDuration = Math.floor(target.durationDays);
      const remaining = phases.map((p, i) => {
        if (i === mergeInto) {
          return {
            ...p,
            durationDays: Math.floor(p.durationDays) + targetDuration,
          };
        }
        return p;
      });
      const stamped = stampPhaseBoundaries(
        remaining.filter((_, i) => i !== action.phaseIndex),
      );
      return finalise(state, {
        ...state,
        recipe: { ...state.recipe, phases: stamped },
      });
    }

    case "ADD_PHASE_BEFORE": {
      const target = state.recipe.phases[action.phaseIndex];
      if (!target) return state;
      const inserted = freshPhase(
        action.name,
        action.days,
        target.order - 0.5,
      );
      const phases = stampPhaseBoundaries([
        ...state.recipe.phases.slice(0, action.phaseIndex),
        inserted,
        ...state.recipe.phases.slice(action.phaseIndex),
      ]);
      return finalise(state, {
        ...state,
        recipe: { ...state.recipe, phases },
      });
    }

    case "ADD_PHASE_AFTER": {
      const target = state.recipe.phases[action.phaseIndex];
      if (!target) return state;
      const inserted = freshPhase(
        action.name,
        action.days,
        target.order + 0.5,
      );
      const phases = stampPhaseBoundaries([
        ...state.recipe.phases.slice(0, action.phaseIndex + 1),
        inserted,
        ...state.recipe.phases.slice(action.phaseIndex + 1),
      ]);
      return finalise(state, {
        ...state,
        recipe: { ...state.recipe, phases },
      });
    }

    default:
      return state;
  }
}

/**
 * Shared epilogue for every mutating action.
 *
 *  1. Captures a snapshot of `prior` (the pre-action state) onto
 *     `undoStack`, capped at {@link UNDO_STACK_LIMIT}.
 *  2. Clears `redoStack` — a mutation invalidates any pending redo.
 */
function finalise(prior: DraftState, mutated: DraftState): DraftState {
  return {
    ...mutated,
    undoStack: pushUndo(prior.undoStack, snapshot(prior)),
    redoStack: [],
  };
}

// ---------------------------------------------------------------------------
// Derived helpers — exposed for tests + components
// ---------------------------------------------------------------------------

/**
 * Count how many cells differ between the draft and the saved snapshot.
 *
 * Cells counted:
 *  - Recipe name change (1 if different).
 *  - Per-phase: rename / duration / light cycle / env band changes
 *    (1 per field-pair that differs).
 *  - Per-phase per-target: value/tolerance/unit triple (1 per target
 *    that differs).
 *  - Per-override: presence / value / tolerance / unit (1 per override
 *    whose `(day, paramName)` key is new, removed, or whose body
 *    differs).
 *
 * The exact count is informational only — the UI just needs "is it
 * non-zero" + a rough number to display. Cheap enough that we don't
 * memoise it.
 */
export function dirtyCount(state: DraftState): number {
  const a = state.recipe;
  const b = state.savedSnapshot.recipe;
  let count = 0;

  if (a.name !== b.name) count += 1;

  // Index saved phases by order (the wire-format key) so re-ordered
  // phases match correctly.
  const savedByOrder = new Map(b.phases.map((p) => [p.order, p]));
  for (const phase of a.phases) {
    const saved = savedByOrder.get(phase.order);
    if (!saved) {
      // A whole new phase — count its name + duration as one structural
      // change plus every target as a cell change.
      count += 1;
      count += Object.keys(phase.targets ?? {}).length;
      continue;
    }
    if (phase.phaseName !== saved.phaseName) count += 1;
    if (
      Math.floor(phase.durationDays) !== Math.floor(saved.durationDays)
    )
      count += 1;
    if (!sameLightCycle(phase.lightCycle, saved.lightCycle)) count += 1;
    if (
      !sameEnvBand(phase.environmentalTargets, saved.environmentalTargets)
    )
      count += 1;
    count += diffTargetMaps(phase.targets, saved.targets);
  }
  // Phases present in saved but missing from draft — every deleted
  // phase is a structural change.
  for (const saved of b.phases) {
    if (!a.phases.some((p) => p.order === saved.order)) count += 1;
  }

  count += diffOverrides(a.phases, state.overrides, state.savedSnapshot.overrides);
  return count;
}

function sameLightCycle(
  a: GrowRecipePhase["lightCycle"],
  b: GrowRecipePhase["lightCycle"],
): boolean {
  if (!a && !b) return true;
  if (!a || !b) return false;
  return a.hoursOn === b.hoursOn && a.hoursOff === b.hoursOff;
}

function sameEnvBand(
  a: GrowRecipePhase["environmentalTargets"],
  b: GrowRecipePhase["environmentalTargets"],
): boolean {
  if (!a && !b) return true;
  const keys = ["temperature", "humidity", "co2", "vpd"] as const;
  for (const k of keys) {
    const av = a?.[k] ?? null;
    const bv = b?.[k] ?? null;
    if (av === null && bv === null) continue;
    if (av === null || bv === null) return false;
    if (av.min !== bv.min || av.max !== bv.max) return false;
  }
  return true;
}

function diffTargetMaps(
  a: GrowRecipePhase["targets"],
  b: GrowRecipePhase["targets"],
): number {
  const aKeys = new Set(Object.keys(a ?? {}));
  const bKeys = new Set(Object.keys(b ?? {}));
  let diff = 0;
  for (const k of aKeys) {
    if (!bKeys.has(k)) {
      diff += 1;
      continue;
    }
    const av = a?.[k];
    const bv = b?.[k];
    if (!av || !bv) continue;
    if (
      av.value !== bv.value ||
      (av.tolerance ?? null) !== (bv.tolerance ?? null) ||
      (av.unit ?? null) !== (bv.unit ?? null)
    ) {
      diff += 1;
    }
  }
  for (const k of bKeys) {
    if (!aKeys.has(k)) diff += 1;
  }
  return diff;
}

function diffOverrides(
  _phases: GrowRecipePhase[],
  draft: DayOverride[],
  saved: DayOverride[],
): number {
  const key = (o: DayOverride) => `${o.day}::${o.paramName}`;
  const draftMap = new Map(draft.map((o) => [key(o), o]));
  const savedMap = new Map(saved.map((o) => [key(o), o]));
  let diff = 0;
  for (const [k, d] of draftMap) {
    const s = savedMap.get(k);
    if (!s) {
      diff += 1;
      continue;
    }
    if (
      d.value !== s.value ||
      (d.tolerance ?? null) !== (s.tolerance ?? null) ||
      (d.unit ?? null) !== (s.unit ?? null)
    ) {
      diff += 1;
    }
  }
  for (const k of savedMap.keys()) {
    if (!draftMap.has(k)) diff += 1;
  }
  return diff;
}
