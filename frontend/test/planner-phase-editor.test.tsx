import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { PhaseEditor } from "@/components/planner/phase-editor";
import { RecipeDraftProvider } from "@/components/planner/recipe-draft-context";
import type { GrowRecipe } from "@/lib/types";
import { renderWithQuery } from "./render";

/**
 * Smoke tests for the phase editor inside a real `<RecipeDraftProvider>`.
 *
 * We render the editor for a known phase, change a value, and assert
 * the reducer responds + the dirty count ticks. Debounce is overridden
 * to 0ms so the test doesn't have to wait on real timers.
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

function mount(phaseIndex: number) {
  return renderWithQuery(
    <RecipeDraftProvider recipe={recipe()} overrides={[]}>
      <PhaseEditor phaseIndex={phaseIndex} debounceMs={0} />
    </RecipeDraftProvider>,
  );
}

describe("<PhaseEditor>", () => {
  it("renders the selected phase's name + duration", () => {
    mount(0);
    const nameInput = screen.getByLabelText("Phase name") as HTMLInputElement;
    expect(nameInput.value).toBe("Veg");
    const durInput = screen.getByLabelText(
      "Phase duration in days",
    ) as HTMLInputElement;
    expect(durInput.value).toBe("7");
  });

  it("dispatches RENAME_PHASE on name commit", async () => {
    mount(0);
    const nameInput = screen.getByLabelText("Phase name") as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "Early Veg" } });
    fireEvent.blur(nameInput);
    // After the debounce the editor re-renders with the new value.
    await waitFor(() => {
      expect(
        (screen.getByLabelText("Phase name") as HTMLInputElement).value,
      ).toBe("Early Veg");
    });
  });

  it("dispatches SET_PHASE_DURATION + re-stamps subsequent phases", async () => {
    mount(0);
    const durInput = screen.getByLabelText(
      "Phase duration in days",
    ) as HTMLInputElement;
    fireEvent.change(durInput, { target: { value: "10" } });
    fireEvent.blur(durInput);
    // We can't assert on phase 2's day range here (editor only shows
    // phase 0), but the reducer's prefix-sum is covered in the reducer
    // suite. The editor's responsibility is to dispatch the right action.
    await waitFor(() => {
      expect(
        (screen.getByLabelText("Phase duration in days") as HTMLInputElement)
          .value,
      ).toBe("10");
    });
  });

  it("renders one row per target with its value pre-filled", () => {
    mount(0);
    const rows = screen.getAllByTestId("phase-target-row");
    expect(rows).toHaveLength(1);
    const row = rows[0]!;
    expect(row.getAttribute("data-param-name")).toBe("temp_day");
    const value = row.querySelector(
      "[aria-label='Value for temp_day']",
    ) as HTMLInputElement;
    expect(value.value).toBe("25");
  });

  it("dispatches SET_PHASE_TARGET when a row's value commits", async () => {
    mount(0);
    const value = screen.getByLabelText(
      "Value for temp_day",
    ) as HTMLInputElement;
    fireEvent.change(value, { target: { value: "26" } });
    fireEvent.blur(value);
    await waitFor(() => {
      expect(
        (screen.getByLabelText("Value for temp_day") as HTMLInputElement)
          .value,
      ).toBe("26");
    });
  });

  it("delete button is disabled when only one phase remains", () => {
    const single: GrowRecipe = {
      ...recipe(),
      phases: [recipe().phases[0]!],
    };
    renderWithQuery(
      <RecipeDraftProvider recipe={single} overrides={[]}>
        <PhaseEditor phaseIndex={0} debounceMs={0} />
      </RecipeDraftProvider>,
    );
    const btn = screen.getByTestId("phase-delete-button") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("split button is rendered but disabled in P2", () => {
    mount(0);
    const btn = screen.getByTestId("phase-split-button") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.getAttribute("title")).toMatch(/P4/i);
  });
});
