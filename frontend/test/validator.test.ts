import { beforeEach, describe, expect, it } from "vitest";

import {
  validate,
  validateUncached,
  _resetValidatorCache,
  cycleDayCount,
  type Finding,
} from "@/lib/planner/validator";
import { stampPhaseBoundaries } from "@/lib/planner/draft-reducer";
import type { DayOverride, GrowRecipe } from "@/lib/types";

/**
 * Validator unit tests — mirrors `backend/tests/unit/test_recipe_resolver.py`
 * `TestValidateRecipe`. The TS validator must produce the same
 * finding codes + severities the Python validator emits so the two
 * stay fixture-compatible.
 */

function recipe(overrides: Partial<GrowRecipe> = {}): GrowRecipe {
  const base: GrowRecipe = {
    id: "rec-1",
    orgId: "open-crop-steering",
    name: "Test 14-day",
    recipeType: ["indoor"],
    description: null,
    version: null,
    isActive: true,
    estimatedTotalDurationDays: null,
    genetics: null,
    createdBy: null,
    lastModifiedBy: null,
    phases: stampPhaseBoundaries([
      {
        phaseName: "Veg",
        durationDays: 7,
        order: 1,
        targets: {
          temp_day: { value: 25, tolerance: 0.5, unit: "C" },
          rh_day: { value: 70, tolerance: 3, unit: "%" },
        },
      },
      {
        phaseName: "Flower",
        durationDays: 7,
        order: 2,
        targets: {
          temp_day: { value: 27, tolerance: 0.5, unit: "C" },
          rh_day: { value: 55, tolerance: 3, unit: "%" },
        },
      },
    ]),
    createdAt: 0,
    updatedAt: 0,
    ...overrides,
  };
  return base;
}

function override(day: number, paramName: string, value: number): DayOverride {
  return {
    id: `ov-${day}-${paramName}`,
    orgId: "o1",
    recipeId: "rec-1",
    day,
    paramName,
    value,
    tolerance: null,
    unit: null,
    createdAt: 0,
    updatedAt: 0,
  };
}

beforeEach(() => {
  _resetValidatorCache();
});

describe("cycleDayCount", () => {
  it("returns the prefix sum of phase durations", () => {
    expect(cycleDayCount(recipe())).toBe(14);
  });
});

describe("validate — clean recipe", () => {
  it("emits no hard refusals", () => {
    const result = validateUncached(recipe(), []);
    expect(result.hardRefusals).toHaveLength(0);
  });
});

describe("validate — contiguity findings", () => {
  it("recipe_no_phases when phases is empty", () => {
    const r = recipe({ phases: [] });
    const result = validateUncached(r, []);
    const codes = result.hardRefusals.map((f) => f.code);
    expect(codes).toContain("recipe_no_phases");
  });

  it("recipe_phases_not_contiguous when a phase gap is hand-mutated", () => {
    const r = recipe();
    // Shift Flower's start to day 9 to leave a gap at day 8.
    const phase = r.phases[1]!;
    phase.startDay = 9;
    phase.endDay = 15;
    const result = validateUncached(r, []);
    const codes = result.hardRefusals.map((f) => f.code);
    expect(codes).toContain("recipe_phases_not_contiguous");
  });

  it("recipe_phases_do_not_start_at_day_1 when the first phase doesn't", () => {
    const r = recipe();
    r.phases[0]!.startDay = 2;
    const result = validateUncached(r, []);
    const codes = result.hardRefusals.map((f) => f.code);
    expect(codes).toContain("recipe_phases_do_not_start_at_day_1");
  });

  it("recipe_phases_do_not_cover_cycle when last phase is short", () => {
    const r = recipe();
    r.phases[1]!.endDay = 13; // cycle stays 14
    const result = validateUncached(r, []);
    const codes = result.hardRefusals.map((f) => f.code);
    expect(codes).toContain("recipe_phases_do_not_cover_cycle");
  });

  it("contiguity findings carry a phaseIndex hint for jump-to", () => {
    const r = recipe();
    r.phases[1]!.startDay = 9;
    r.phases[1]!.endDay = 15;
    const result = validateUncached(r, []);
    const finding = result.hardRefusals.find(
      (f) => f.code === "recipe_phases_not_contiguous",
    );
    expect(finding?.phaseIndex).toBe(1);
  });
});

describe("validate — override findings", () => {
  it("override_day_out_of_cycle is a hard refusal", () => {
    const r = recipe();
    const ov = override(99, "temp_day", 25);
    const result = validateUncached(r, [ov]);
    const hard = result.hardRefusals.find(
      (f) => f.code === "override_day_out_of_cycle",
    );
    expect(hard).toBeDefined();
    expect(hard?.day).toBe(99);
  });

  it("override_param_orphan is a warning", () => {
    const r = recipe();
    // `ppfd` isn't declared by either phase's targets.
    const ov = override(3, "ppfd", 450);
    const result = validateUncached(r, [ov]);
    const orphan = result.warnings.find(
      (f) => f.code === "override_param_orphan",
    );
    expect(orphan).toBeDefined();
    expect(orphan?.hard).toBe(false);
  });

  it("orphan warning is suppressed when ALL phases have empty targets", () => {
    // Mirrors the Python: when `declared` is empty, no orphan warnings
    // are emitted — the empty-phase warning covers that case instead.
    const r = recipe();
    r.phases.forEach((p) => {
      p.targets = {};
    });
    const ov = override(3, "ppfd", 450);
    const result = validateUncached(r, [ov]);
    const orphans = result.warnings.filter(
      (f) => f.code === "override_param_orphan",
    );
    expect(orphans).toHaveLength(0);
  });
});

describe("validate — phase_targets_empty", () => {
  it("emits one warning per phase with empty targets", () => {
    const r = recipe();
    r.phases[0]!.targets = {};
    const result = validateUncached(r, []);
    const warnings = result.warnings.filter(
      (f) => f.code === "phase_targets_empty",
    );
    expect(warnings).toHaveLength(1);
    expect(warnings[0]!.phaseIndex).toBe(0);
  });

  it("treats missing targets the same as an empty object", () => {
    const r = recipe();
    r.phases[0]!.targets = undefined;
    const result = validateUncached(r, []);
    const warnings = result.warnings.filter(
      (f) => f.code === "phase_targets_empty",
    );
    expect(warnings).toHaveLength(1);
  });
});

describe("validate — memoisation", () => {
  it("returns the same result object when inputs are unchanged", () => {
    const r = recipe();
    const a = validate(r, []);
    const b = validate(r, []);
    expect(a).toBe(b);
  });

  it("recomputes when the recipe identity changes", () => {
    const a = validate(recipe(), []);
    const b = validate(recipe({ name: "Different" }), []);
    expect(a).not.toBe(b);
  });

  it("re-running with a different override re-runs the validator", () => {
    const r = recipe();
    const empty = validate(r, []);
    const withBad = validate(r, [override(99, "temp_day", 25)]);
    expect(empty).not.toBe(withBad);
    expect(
      withBad.hardRefusals.some((f: Finding) => f.code === "override_day_out_of_cycle"),
    ).toBe(true);
  });
});
