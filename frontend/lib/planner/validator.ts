/**
 * TypeScript port of `backend/app/core/recipe_resolver.py`'s
 * `validate_recipe`.
 *
 * Runs the SAME structural checks the Python validator runs:
 *
 *  - Cross-phase contiguity / coverage — first phase must start at
 *    day 1, last phase must end at the cycle's last day, intermediate
 *    phases must be back-to-back (no gap, no overlap).
 *  - Each override's `day` must fall inside `[1, cycleDayCount]`.
 *  - Soft warning when an override's `paramName` is declared by NO
 *    phase's targets map.
 *  - Soft warning when a phase has an empty targets map.
 *
 * Memoisation: the result is cached on a content-hash of
 * `(recipe, overrides)`. The reducer re-derives state on every action,
 * but the recipe + overrides are usually unchanged for the action's
 * common case (e.g. selecting a phase) — caching skips the rerun.
 *
 * Mirrors the same finding codes the Python emits so the two stay
 * fixture-compatible:
 *
 *  - `recipe_no_phases` (hard)
 *  - `recipe_phases_do_not_start_at_day_1` (hard)
 *  - `recipe_phases_not_contiguous` (hard)
 *  - `recipe_phases_do_not_cover_cycle` (hard)
 *  - `override_day_out_of_cycle` (hard)
 *  - `override_param_orphan` (warning)
 *  - `phase_targets_empty` (warning)
 */

import type { DayOverride, GrowRecipe, GrowRecipePhase } from "@/lib/types";

/**
 * One validation finding — the TS twin of `WizardFinding`.
 *
 * The optional `phaseIndex` / `day` / `paramName` are not part of the
 * Python contract; they let `<ValidationBanner>` highlight / scroll to
 * the offending phase or day when a finding is clicked.
 */
export interface Finding {
  /** Stable machine code. Mirrors the Python validator's codes. */
  code: string;
  /** Human-readable explanation for the planner UI. */
  message: string;
  /** `true` for a hard refusal, `false` for a fail-soft warning. */
  hard: boolean;
  /** Optional target hints (used by the banner to jump to a phase / day). */
  phaseIndex?: number;
  day?: number;
  paramName?: string;
}

export interface ValidationResult {
  hardRefusals: Finding[];
  warnings: Finding[];
}

// ---------------------------------------------------------------------------
// Cycle helpers
// ---------------------------------------------------------------------------

/**
 * Sum of `Math.floor(phase.durationDays)`. Mirrors `cycle_day_count`
 * from the Python `GrowRecipeBase` property.
 */
export function cycleDayCount(recipe: GrowRecipe): number {
  return recipe.phases.reduce(
    (sum, p) => sum + Math.max(0, Math.floor(p.durationDays)),
    0,
  );
}

// ---------------------------------------------------------------------------
// Finding builders
// ---------------------------------------------------------------------------

function contiguityFindings(recipe: GrowRecipe): Finding[] {
  const out: Finding[] = [];

  // Phases come in `order`-sorted from the reducer, but defensively
  // sort here too (a hand-built fixture in a test may not).
  const phases = [...recipe.phases].sort((a, b) => a.order - b.order);

  const first = phases[0];
  if (!first) {
    out.push({
      code: "recipe_no_phases",
      hard: true,
      message:
        "Recipe has no phases — at least one phase is required so every day in the cycle is covered.",
    });
    return out;
  }

  const total = cycleDayCount(recipe);

  if ((first.startDay ?? 1) !== 1) {
    out.push({
      code: "recipe_phases_do_not_start_at_day_1",
      hard: true,
      message: `First phase '${first.phaseName}' starts at day ${
        first.startDay ?? "?"
      }, not day 1 — phases must cover the cycle from day 1.`,
      phaseIndex: 0,
    });
  }

  for (let i = 1; i < phases.length; i += 1) {
    const prev = phases[i - 1];
    const phase = phases[i];
    if (!prev || !phase) continue;
    if (
      phase.startDay == null ||
      prev.endDay == null ||
      phase.startDay !== prev.endDay + 1
    ) {
      out.push({
        code: "recipe_phases_not_contiguous",
        hard: true,
        message: `Phase '${prev.phaseName}' ends at day ${prev.endDay} but next phase '${phase.phaseName}' starts at day ${phase.startDay} — phases must be contiguous with no gap or overlap.`,
        phaseIndex: i,
      });
    }
  }

  const last = phases[phases.length - 1];
  if (last && (last.endDay ?? -1) !== total) {
    out.push({
      code: "recipe_phases_do_not_cover_cycle",
      hard: true,
      message: `Last phase '${last.phaseName}' ends at day ${last.endDay} but the cycle is ${total} days — phases must cover the whole cycle.`,
      phaseIndex: phases.length - 1,
    });
  }

  return out;
}

function overrideFindings(
  recipe: GrowRecipe,
  overrides: DayOverride[],
): Finding[] {
  const out: Finding[] = [];
  const total = cycleDayCount(recipe);
  const declared = new Set<string>();
  for (const phase of recipe.phases) {
    if (phase.targets) {
      for (const k of Object.keys(phase.targets)) declared.add(k);
    }
  }

  for (const override of overrides) {
    if (override.day < 1 || override.day > total) {
      out.push({
        code: "override_day_out_of_cycle",
        hard: true,
        message: `Override on day ${override.day} param '${override.paramName}' is outside the recipe's cycle [1, ${total}].`,
        day: override.day,
        paramName: override.paramName,
      });
      continue;
    }
    // Mirror the Python: only emit the orphan warning when there ARE
    // declared params somewhere; an entirely empty target set is its
    // own warning code (phase_targets_empty) and we don't want to
    // double-up.
    if (declared.size > 0 && !declared.has(override.paramName)) {
      out.push({
        code: "override_param_orphan",
        hard: false,
        message: `Override on day ${override.day} param '${override.paramName}': no phase declares this parameter in its targets map — likely a typo.`,
        day: override.day,
        paramName: override.paramName,
      });
    }
  }
  return out;
}

function emptyPhaseFindings(recipe: GrowRecipe): Finding[] {
  const out: Finding[] = [];
  recipe.phases.forEach((phase, i) => {
    const empty = !phase.targets || Object.keys(phase.targets).length === 0;
    if (empty) {
      out.push({
        code: "phase_targets_empty",
        hard: false,
        message: `Phase '${phase.phaseName}' (days ${phase.startDay}..${phase.endDay}) has no targets — every day in this phase will need an override row to produce an effective target.`,
        phaseIndex: i,
      });
    }
  });
  return out;
}

// ---------------------------------------------------------------------------
// Memoisation
// ---------------------------------------------------------------------------

/**
 * A very cheap, deterministic content hash of the inputs.
 *
 * We use `JSON.stringify` with explicit key ordering for the recipe +
 * overrides — both shapes are small (<5 KB once serialised), so the
 * hashing cost is dwarfed by the validator's own work. Picking a
 * proper hashing library would be overkill.
 */
function hashKey(recipe: GrowRecipe, overrides: DayOverride[]): string {
  // Use a stable shape projection — we don't care about server-managed
  // fields like createdAt / updatedAt for validation. They never affect
  // the findings, and dropping them increases hit-rate after a save.
  const recipeProjection = {
    name: recipe.name,
    phases: recipe.phases.map((p) => ({
      phaseName: p.phaseName,
      durationDays: Math.floor(p.durationDays),
      order: p.order,
      startDay: p.startDay,
      endDay: p.endDay,
      targets: p.targets ?? null,
      environmentalTargets: p.environmentalTargets ?? null,
      lightCycle: p.lightCycle ?? null,
    })),
  };
  const overrideProjection = overrides.map((o) => ({
    day: o.day,
    paramName: o.paramName,
    value: o.value,
    tolerance: o.tolerance ?? null,
    unit: o.unit ?? null,
  }));
  return (
    JSON.stringify(recipeProjection) +
    "::" +
    JSON.stringify(overrideProjection)
  );
}

let _lastKey: string | null = null;
let _lastResult: ValidationResult | null = null;

/**
 * Run the validator on a `(recipe, overrides)` pair.
 *
 * Result is memoised on the content hash of the inputs; calling
 * `validate(sameRecipe, sameOverrides)` returns the cached result.
 *
 * `validateUncached` (the underlying function) is exported for tests
 * that want to bypass the cache.
 */
export function validate(
  recipe: GrowRecipe,
  overrides: DayOverride[],
): ValidationResult {
  const key = hashKey(recipe, overrides);
  if (_lastKey === key && _lastResult !== null) return _lastResult;
  const result = validateUncached(recipe, overrides);
  _lastKey = key;
  _lastResult = result;
  return result;
}

/** Reset the validator cache. Tests use this; runtime doesn't need it. */
export function _resetValidatorCache(): void {
  _lastKey = null;
  _lastResult = null;
}

/** Run the validator unconditionally — exported for tests. */
export function validateUncached(
  recipe: GrowRecipe,
  overrides: DayOverride[],
): ValidationResult {
  const findings: Finding[] = [];
  findings.push(...contiguityFindings(recipe));
  findings.push(...overrideFindings(recipe, overrides));
  findings.push(...emptyPhaseFindings(recipe));

  const hardRefusals: Finding[] = [];
  const warnings: Finding[] = [];
  for (const f of findings) {
    (f.hard ? hardRefusals : warnings).push(f);
  }
  return { hardRefusals, warnings };
}

// Allow callers to also resolve a `GrowRecipePhase` by 0-based index
// without re-importing — saves a circular-dep risk if `<ValidationBanner>`
// wants to format finding-specific copy.
export type { GrowRecipePhase };
