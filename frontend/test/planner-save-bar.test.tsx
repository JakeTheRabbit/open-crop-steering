import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithQuery } from "./render";
import type { DayOverride, GrowRecipe } from "@/lib/types";

/**
 * Save-bar tests.
 *
 * Mocks the two mutations the save flow drives:
 *  - `updateGrowRecipe`  — step 1.
 *  - `replaceDayOverrides` — step 2.
 *  - `listDayOverrides`   — re-fetch after success.
 *
 * Happy path: both succeed -> dirty count clears, save state `'ok'`.
 * Partial path: step 2 fails -> save state `'partial'`, retry button
 * appears, retry succeeds -> dirty count clears.
 */

// All the network-touching calls go through `api`. We mock the module
// so the save flow's mutations resolve from test fixtures.
const updateGrowRecipe = vi.fn();
const replaceDayOverrides = vi.fn();
const listDayOverrides = vi.fn();

vi.mock("@/lib/api-client", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    detail: unknown;
    constructor(status: number, detail: unknown) {
      super(`API error ${status}`);
      this.status = status;
      this.detail = detail;
    }
  },
  api: {
    updateGrowRecipe: (...args: unknown[]) => updateGrowRecipe(...args),
    replaceDayOverrides: (...args: unknown[]) =>
      replaceDayOverrides(...args),
    listDayOverrides: (...args: unknown[]) => listDayOverrides(...args),
  },
}));

// Imports must come AFTER the mock so the components pick up the
// mocked `api`.
import { RecipeDraftProvider } from "@/components/planner/recipe-draft-context";
import { RecipeSaveBar } from "@/components/planner/recipe-save-bar";
import { PhaseEditor } from "@/components/planner/phase-editor";

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

function fixture(): { recipe: GrowRecipe; overrides: DayOverride[] } {
  return {
    recipe: recipe(),
    overrides: [],
  };
}

function dirtyHarness() {
  // Wrap in a provider with the editor + save bar mounted. The editor
  // gives us a way to dispatch a SET_PHASE_TARGET via UI input so the
  // dirty count ticks up (more realistic than calling the dispatcher
  // directly from the test).
  const { recipe: r, overrides } = fixture();
  return renderWithQuery(
    <RecipeDraftProvider recipe={r} overrides={overrides}>
      <PhaseEditor phaseIndex={0} debounceMs={0} />
      <RecipeSaveBar />
    </RecipeDraftProvider>,
  );
}

beforeEach(() => {
  updateGrowRecipe.mockReset();
  replaceDayOverrides.mockReset();
  listDayOverrides.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("<RecipeSaveBar>", () => {
  it("Save button is disabled when the draft is clean", () => {
    dirtyHarness();
    const btn = screen.getByTestId("save-button") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(screen.getByTestId("dirty-count").textContent).toMatch(
      /All changes saved/,
    );
  });

  it("happy path — both PUTs succeed, dirty count clears", async () => {
    updateGrowRecipe.mockResolvedValue({ ...recipe(), name: "Test (saved)" });
    replaceDayOverrides.mockResolvedValue({ overrides: [] });
    listDayOverrides.mockResolvedValue([]);

    dirtyHarness();

    // Make the draft dirty.
    const value = screen.getByLabelText(
      "Value for temp_day",
    ) as HTMLInputElement;
    fireEvent.change(value, { target: { value: "26" } });
    fireEvent.blur(value);

    await waitFor(() =>
      expect(screen.getByTestId("dirty-count").textContent).toMatch(/unsaved/),
    );

    fireEvent.click(screen.getByTestId("save-button"));

    await waitFor(() => {
      expect(updateGrowRecipe).toHaveBeenCalledTimes(1);
      expect(replaceDayOverrides).toHaveBeenCalledTimes(1);
    });

    await waitFor(() =>
      expect(screen.getByTestId("recipe-save-bar").getAttribute("data-save-state")).toBe(
        "ok",
      ),
    );
    expect(screen.getByTestId("dirty-count").textContent).toMatch(
      /All changes saved/,
    );
  });

  it("partial path — step 2 fails, retry button appears, retry succeeds", async () => {
    updateGrowRecipe.mockResolvedValue({ ...recipe(), name: "Test (saved)" });
    // First call fails, retry succeeds.
    replaceDayOverrides
      .mockRejectedValueOnce(new Error("override constraint violation"))
      .mockResolvedValueOnce({ overrides: [] });
    listDayOverrides.mockResolvedValue([]);

    dirtyHarness();

    // Make the draft dirty so save is enabled.
    const value = screen.getByLabelText(
      "Value for temp_day",
    ) as HTMLInputElement;
    fireEvent.change(value, { target: { value: "26" } });
    fireEvent.blur(value);

    await waitFor(() =>
      expect(screen.getByTestId("dirty-count").textContent).toMatch(/unsaved/),
    );

    fireEvent.click(screen.getByTestId("save-button"));

    await waitFor(() =>
      expect(
        screen.getByTestId("recipe-save-bar").getAttribute("data-save-state"),
      ).toBe("partial"),
    );

    // Retry button should be present.
    const retry = await screen.findByTestId("retry-overrides-button");
    expect(retry).toBeInTheDocument();

    // Save error message surfaces.
    expect(screen.getByTestId("save-error")).toBeInTheDocument();

    // The draft must STILL be dirty — the recipe PUT succeeded but
    // the overrides did not, so the saved snapshot is unchanged.
    expect(screen.getByTestId("dirty-count").textContent).toMatch(/unsaved/);

    // Click retry — second call resolves, both buckets confirm.
    fireEvent.click(retry);

    await waitFor(() =>
      expect(
        screen.getByTestId("recipe-save-bar").getAttribute("data-save-state"),
      ).toBe("ok"),
    );
    // Now we should be clean.
    await waitFor(() =>
      expect(screen.getByTestId("dirty-count").textContent).toMatch(
        /All changes saved/,
      ),
    );

    // Step 1 was called exactly once across both attempts; step 2 was
    // called twice (initial + retry).
    expect(updateGrowRecipe).toHaveBeenCalledTimes(1);
    expect(replaceDayOverrides).toHaveBeenCalledTimes(2);
  });

  it("step 1 failure leaves the draft dirty and surfaces the error", async () => {
    updateGrowRecipe.mockRejectedValue(new Error("server exploded"));

    dirtyHarness();

    const value = screen.getByLabelText(
      "Value for temp_day",
    ) as HTMLInputElement;
    fireEvent.change(value, { target: { value: "26" } });
    fireEvent.blur(value);

    await waitFor(() =>
      expect(screen.getByTestId("dirty-count").textContent).toMatch(/unsaved/),
    );

    fireEvent.click(screen.getByTestId("save-button"));

    await waitFor(() => expect(updateGrowRecipe).toHaveBeenCalled());
    // Step 2 should NOT have been called because step 1 failed.
    expect(replaceDayOverrides).not.toHaveBeenCalled();

    await waitFor(() => {
      expect(
        screen.getByTestId("recipe-save-bar").getAttribute("data-save-state"),
      ).toBe("idle");
    });
    expect(screen.getByTestId("save-error")).toBeInTheDocument();
    expect(screen.getByTestId("dirty-count").textContent).toMatch(/unsaved/);
  });
});

describe("<DiscardChangesDialog> + RESET_TO_SAVED smoke", () => {
  it("opens on click, confirms RESET_TO_SAVED, clears dirty", async () => {
    dirtyHarness();

    // Dirty the draft.
    const value = screen.getByLabelText(
      "Value for temp_day",
    ) as HTMLInputElement;
    fireEvent.change(value, { target: { value: "26" } });
    fireEvent.blur(value);

    await waitFor(() =>
      expect(screen.getByTestId("dirty-count").textContent).toMatch(/unsaved/),
    );

    // Open the dialog.
    fireEvent.click(screen.getByTestId("discard-changes-button"));
    const confirm = await screen.findByTestId("discard-changes-confirm");
    fireEvent.click(confirm);

    await waitFor(() =>
      expect(screen.getByTestId("dirty-count").textContent).toMatch(
        /All changes saved/,
      ),
    );
  });
});
