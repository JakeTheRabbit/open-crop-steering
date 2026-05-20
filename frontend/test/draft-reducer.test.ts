import { describe, expect, it } from "vitest";

import {
  draftReducer,
  initDraftState,
  dirtyCount,
  stampPhaseBoundaries,
  UNDO_STACK_LIMIT,
  type DraftState,
} from "@/lib/planner/draft-reducer";
import type { DayOverride, GrowRecipe } from "@/lib/types";

/**
 * Unit tests for the planner draft reducer.
 *
 * Each action type is covered, plus the cross-cutting concerns the
 * reducer guarantees (prefix-sum stamping, dense `order`,
 * undo/redo correctness, RESET_TO_SAVED rewinds *everything*,
 * LOAD_FROM_SERVER seeds savedSnapshot).
 */

function recipe(): GrowRecipe {
  return {
    id: "rec-1",
    orgId: "open-crop-steering",
    name: "Test",
    recipeType: ["indoor"],
    description: null,
    version: null,
    isActive: true,
    estimatedTotalDurationDays: null,
    genetics: null,
    createdBy: null,
    lastModifiedBy: null,
    phases: [
      {
        phaseName: "Veg",
        durationDays: 7,
        order: 1,
        startDay: 1,
        endDay: 7,
        targets: {
          temp_day: { value: 25, tolerance: 0.5, unit: "C" },
          rh_day: { value: 70, tolerance: 3, unit: "%" },
        },
      },
      {
        phaseName: "Flower",
        durationDays: 7,
        order: 2,
        startDay: 8,
        endDay: 14,
        targets: {
          temp_day: { value: 27, tolerance: 0.5, unit: "C" },
        },
      },
    ],
    createdAt: 0,
    updatedAt: 0,
  };
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

function init(): DraftState {
  return initDraftState(recipe(), []);
}

describe("initDraftState", () => {
  it("re-stamps phase boundaries and seeds savedSnapshot", () => {
    const state = initDraftState(
      {
        ...recipe(),
        phases: [
          {
            ...recipe().phases[0]!,
            startDay: null,
            endDay: null,
          },
          {
            ...recipe().phases[1]!,
            startDay: null,
            endDay: null,
          },
        ],
      },
      [],
    );
    expect(state.recipe.phases[0]!.startDay).toBe(1);
    expect(state.recipe.phases[0]!.endDay).toBe(7);
    expect(state.recipe.phases[1]!.startDay).toBe(8);
    expect(state.recipe.phases[1]!.endDay).toBe(14);
    expect(state.savedSnapshot.recipe.phases[0]!.startDay).toBe(1);
    // saved and live must be independent clones.
    expect(state.savedSnapshot.recipe).not.toBe(state.recipe);
  });

  it("does not share mutable arrays between recipe and savedSnapshot", () => {
    const state = init();
    state.recipe.phases.push({
      phaseName: "X",
      durationDays: 1,
      order: 99,
    });
    expect(state.savedSnapshot.recipe.phases).toHaveLength(2);
  });
});

describe("stampPhaseBoundaries", () => {
  it("normalises order to a dense 1..N sequence", () => {
    const phases = stampPhaseBoundaries([
      { phaseName: "b", durationDays: 4, order: 2.5 },
      { phaseName: "a", durationDays: 3, order: 1 },
      { phaseName: "c", durationDays: 5, order: 4 },
    ]);
    expect(phases.map((p) => p.phaseName)).toEqual(["a", "b", "c"]);
    expect(phases.map((p) => p.order)).toEqual([1, 2, 3]);
    expect(phases[0]!.startDay).toBe(1);
    expect(phases[0]!.endDay).toBe(3);
    expect(phases[1]!.startDay).toBe(4);
    expect(phases[1]!.endDay).toBe(7);
    expect(phases[2]!.startDay).toBe(8);
    expect(phases[2]!.endDay).toBe(12);
  });
});

describe("RENAME_RECIPE", () => {
  it("updates name + pushes undo", () => {
    const state = init();
    const next = draftReducer(state, {
      type: "RENAME_RECIPE",
      name: "New",
    });
    expect(next.recipe.name).toBe("New");
    expect(next.undoStack).toHaveLength(1);
    expect(next.redoStack).toHaveLength(0);
  });

  it("is a no-op when name matches", () => {
    const state = init();
    const next = draftReducer(state, {
      type: "RENAME_RECIPE",
      name: state.recipe.name,
    });
    expect(next).toBe(state);
  });
});

describe("RENAME_PHASE", () => {
  it("updates phase name + preserves order", () => {
    const state = init();
    const next = draftReducer(state, {
      type: "RENAME_PHASE",
      phaseIndex: 0,
      name: "Early Veg",
    });
    expect(next.recipe.phases[0]!.phaseName).toBe("Early Veg");
    expect(next.recipe.phases[0]!.order).toBe(1);
  });
});

describe("SET_PHASE_DURATION", () => {
  it("re-stamps every subsequent phase via prefix-sum", () => {
    const state = init();
    const next = draftReducer(state, {
      type: "SET_PHASE_DURATION",
      phaseIndex: 0,
      days: 10,
    });
    expect(next.recipe.phases[0]!.startDay).toBe(1);
    expect(next.recipe.phases[0]!.endDay).toBe(10);
    expect(next.recipe.phases[1]!.startDay).toBe(11);
    expect(next.recipe.phases[1]!.endDay).toBe(17);
  });

  it("floors fractional days + enforces a minimum of 1", () => {
    const state = init();
    const next = draftReducer(state, {
      type: "SET_PHASE_DURATION",
      phaseIndex: 0,
      days: 0,
    });
    expect(next.recipe.phases[0]!.durationDays).toBe(1);
  });
});

describe("SET_PHASE_LIGHT_CYCLE / SET_PHASE_ENV_BAND", () => {
  it("sets the light cycle on the target phase only", () => {
    const state = init();
    const next = draftReducer(state, {
      type: "SET_PHASE_LIGHT_CYCLE",
      phaseIndex: 1,
      hoursOn: 12,
      hoursOff: 12,
    });
    expect(next.recipe.phases[1]!.lightCycle).toEqual({
      hoursOn: 12,
      hoursOff: 12,
    });
    expect(next.recipe.phases[0]!.lightCycle).toBeUndefined();
  });

  it("sets one env-band key without disturbing the rest", () => {
    let state = init();
    state = draftReducer(state, {
      type: "SET_PHASE_ENV_BAND",
      phaseIndex: 0,
      key: "temperature",
      min: 22,
      max: 26,
    });
    state = draftReducer(state, {
      type: "SET_PHASE_ENV_BAND",
      phaseIndex: 0,
      key: "humidity",
      min: 55,
      max: 65,
    });
    expect(state.recipe.phases[0]!.environmentalTargets).toEqual({
      temperature: { min: 22, max: 26 },
      humidity: { min: 55, max: 65 },
    });
  });
});

describe("SET_PHASE_TARGET / ADD_PHASE_TARGET / REMOVE_PHASE_TARGET", () => {
  it("upserts an existing target", () => {
    const state = init();
    const next = draftReducer(state, {
      type: "SET_PHASE_TARGET",
      phaseIndex: 0,
      paramName: "temp_day",
      value: 26,
      tolerance: 1,
      unit: "C",
    });
    expect(next.recipe.phases[0]!.targets!.temp_day).toEqual({
      value: 26,
      tolerance: 1,
      unit: "C",
    });
  });

  it("ADD_PHASE_TARGET inserts a new param", () => {
    const state = init();
    const next = draftReducer(state, {
      type: "ADD_PHASE_TARGET",
      phaseIndex: 0,
      paramName: "ppfd",
      value: 600,
      tolerance: 30,
      unit: "umol/m2/s",
    });
    expect(next.recipe.phases[0]!.targets!.ppfd).toEqual({
      value: 600,
      tolerance: 30,
      unit: "umol/m2/s",
    });
  });

  it("REMOVE_PHASE_TARGET drops the key", () => {
    const state = init();
    const next = draftReducer(state, {
      type: "REMOVE_PHASE_TARGET",
      phaseIndex: 0,
      paramName: "rh_day",
    });
    expect(next.recipe.phases[0]!.targets!.rh_day).toBeUndefined();
    expect(next.recipe.phases[0]!.targets!.temp_day).toBeDefined();
  });
});

describe("DELETE_PHASE", () => {
  it("merges the deleted phase's days into the previous phase", () => {
    const state = init();
    const next = draftReducer(state, {
      type: "DELETE_PHASE",
      phaseIndex: 1,
    });
    expect(next.recipe.phases).toHaveLength(1);
    // Veg (7d) + Flower (7d merged in) = 14d
    expect(next.recipe.phases[0]!.durationDays).toBe(14);
    expect(next.recipe.phases[0]!.phaseName).toBe("Veg");
    expect(next.recipe.phases[0]!.endDay).toBe(14);
  });

  it("when deleting index 0, merges into what becomes index 0", () => {
    const state = init();
    const next = draftReducer(state, {
      type: "DELETE_PHASE",
      phaseIndex: 0,
    });
    expect(next.recipe.phases).toHaveLength(1);
    expect(next.recipe.phases[0]!.phaseName).toBe("Flower");
    expect(next.recipe.phases[0]!.durationDays).toBe(14);
  });

  it("refuses to delete the last remaining phase", () => {
    let state = init();
    state = draftReducer(state, { type: "DELETE_PHASE", phaseIndex: 1 });
    const next = draftReducer(state, { type: "DELETE_PHASE", phaseIndex: 0 });
    expect(next).toBe(state);
  });
});

describe("ADD_PHASE_BEFORE / ADD_PHASE_AFTER", () => {
  it("ADD_PHASE_AFTER inserts and re-stamps", () => {
    const state = init();
    const next = draftReducer(state, {
      type: "ADD_PHASE_AFTER",
      phaseIndex: 0,
      name: "Stretch",
      days: 5,
    });
    expect(next.recipe.phases.map((p) => p.phaseName)).toEqual([
      "Veg",
      "Stretch",
      "Flower",
    ]);
    expect(next.recipe.phases.map((p) => p.order)).toEqual([1, 2, 3]);
    expect(next.recipe.phases[1]!.startDay).toBe(8);
    expect(next.recipe.phases[1]!.endDay).toBe(12);
    expect(next.recipe.phases[2]!.startDay).toBe(13);
  });

  it("ADD_PHASE_BEFORE inserts a new first phase when the target is 0", () => {
    const state = init();
    const next = draftReducer(state, {
      type: "ADD_PHASE_BEFORE",
      phaseIndex: 0,
      name: "Seedling",
      days: 3,
    });
    expect(next.recipe.phases.map((p) => p.phaseName)).toEqual([
      "Seedling",
      "Veg",
      "Flower",
    ]);
    expect(next.recipe.phases[0]!.startDay).toBe(1);
    expect(next.recipe.phases[0]!.endDay).toBe(3);
    expect(next.recipe.phases[1]!.startDay).toBe(4);
  });
});

describe("UNDO / REDO", () => {
  it("UNDO pops the most recent edit + REDO restores it", () => {
    const original = init();
    const renamed = draftReducer(original, {
      type: "RENAME_RECIPE",
      name: "Renamed",
    });
    expect(renamed.recipe.name).toBe("Renamed");

    const undone = draftReducer(renamed, { type: "UNDO" });
    expect(undone.recipe.name).toBe("Test");
    expect(undone.undoStack).toHaveLength(0);
    expect(undone.redoStack).toHaveLength(1);

    const redone = draftReducer(undone, { type: "REDO" });
    expect(redone.recipe.name).toBe("Renamed");
    expect(redone.redoStack).toHaveLength(0);
    expect(redone.undoStack).toHaveLength(1);
  });

  it("a fresh mutation clears redoStack", () => {
    const original = init();
    const a = draftReducer(original, {
      type: "RENAME_RECIPE",
      name: "A",
    });
    const undone = draftReducer(a, { type: "UNDO" });
    expect(undone.redoStack).toHaveLength(1);
    const b = draftReducer(undone, {
      type: "RENAME_RECIPE",
      name: "B",
    });
    expect(b.redoStack).toHaveLength(0);
  });

  it("UNDO with an empty stack is a no-op", () => {
    const state = init();
    const next = draftReducer(state, { type: "UNDO" });
    expect(next).toBe(state);
  });

  it("undo stack caps at the limit", () => {
    let state = init();
    for (let i = 0; i < UNDO_STACK_LIMIT + 5; i += 1) {
      state = draftReducer(state, {
        type: "RENAME_RECIPE",
        name: `name-${i}`,
      });
    }
    expect(state.undoStack.length).toBe(UNDO_STACK_LIMIT);
  });
});

describe("RESET_TO_SAVED", () => {
  it("rewinds recipe + overrides + clears both stacks", () => {
    let state = initDraftState(recipe(), [override(1, "temp_day", 99)]);
    state = draftReducer(state, { type: "RENAME_RECIPE", name: "X" });
    state = draftReducer(state, {
      type: "SET_PHASE_DURATION",
      phaseIndex: 0,
      days: 14,
    });
    expect(state.undoStack.length).toBeGreaterThan(0);

    const reset = draftReducer(state, { type: "RESET_TO_SAVED" });
    expect(reset.recipe.name).toBe("Test");
    expect(reset.recipe.phases[0]!.durationDays).toBe(7);
    expect(reset.overrides).toEqual(state.savedSnapshot.overrides);
    expect(reset.undoStack).toEqual([]);
    expect(reset.redoStack).toEqual([]);
  });
});

describe("LOAD_FROM_SERVER", () => {
  it("re-seeds the state from new server data + resets saved snapshot", () => {
    const state = init();
    const edited = draftReducer(state, {
      type: "RENAME_RECIPE",
      name: "Edited",
    });
    const reloaded = draftReducer(edited, {
      type: "LOAD_FROM_SERVER",
      recipe: { ...recipe(), name: "From server" },
      overrides: [override(1, "temp_day", 30)],
    });
    expect(reloaded.recipe.name).toBe("From server");
    expect(reloaded.savedSnapshot.recipe.name).toBe("From server");
    expect(reloaded.overrides).toHaveLength(1);
    expect(reloaded.undoStack).toEqual([]);
    expect(reloaded.redoStack).toEqual([]);
  });
});

describe("dirtyCount", () => {
  it("is 0 for a freshly-loaded state", () => {
    const state = init();
    expect(dirtyCount(state)).toBe(0);
  });

  it("counts a rename as 1", () => {
    const state = draftReducer(init(), {
      type: "RENAME_RECIPE",
      name: "Renamed",
    });
    expect(dirtyCount(state)).toBe(1);
  });

  it("counts a target edit as 1", () => {
    const state = draftReducer(init(), {
      type: "SET_PHASE_TARGET",
      phaseIndex: 0,
      paramName: "temp_day",
      value: 26,
      tolerance: 0.5,
      unit: "C",
    });
    expect(dirtyCount(state)).toBe(1);
  });

  it("counts a duration change as 1", () => {
    const state = draftReducer(init(), {
      type: "SET_PHASE_DURATION",
      phaseIndex: 0,
      days: 10,
    });
    expect(dirtyCount(state)).toBe(1);
  });

  it("counts a new override as 1", () => {
    let state = initDraftState(recipe(), []);
    state = {
      ...state,
      overrides: [override(1, "temp_day", 30)],
    };
    expect(dirtyCount(state)).toBeGreaterThanOrEqual(1);
  });
});
