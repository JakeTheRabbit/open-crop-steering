import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { PlannerCanvas } from "@/app/planner/recipes/[recipeId]/planner-page";
import type { DayOverride, GrowRecipe } from "@/lib/types";

/**
 * Smoke test for the P1 recipe-canvas component.
 *
 * Directly renders `<PlannerCanvas>` with a fixture recipe + override
 * set (3 phases, 5 overrides) and asserts the timeline renders the
 * right number of phase blocks, each with the right day-range label,
 * and that pin markers appear at the days with overrides. The host
 * page (`PlannerPage`) only wraps this in TanStack Query — no need to
 * exercise that here.
 */

function recipeFixture(): GrowRecipe {
  return {
    id: "rec-1",
    orgId: "open-crop-steering",
    name: "Cannabis 12-week (Blue Dream)",
    recipeType: ["cannabis"],
    description: null,
    version: 1,
    isActive: true,
    estimatedTotalDurationDays: null,
    genetics: null,
    createdBy: null,
    lastModifiedBy: null,
    phases: [
      {
        phaseName: "Early Veg",
        durationDays: 21,
        order: 1,
        startDay: 1,
        endDay: 21,
        targets: {
          temp_day: { value: 25, tolerance: 0.5, unit: "C" },
          rh_day: { value: 70, tolerance: 3, unit: "%" },
        },
      },
      {
        phaseName: "Late Veg",
        durationDays: 21,
        order: 2,
        startDay: 22,
        endDay: 42,
        targets: {
          temp_day: { value: 27, tolerance: 0.5, unit: "C" },
        },
      },
      {
        phaseName: "Flower",
        durationDays: 42,
        order: 3,
        startDay: 43,
        endDay: 84,
        targets: {
          temp_day: { value: 26.5, tolerance: 0.5, unit: "C" },
          ppfd: { value: 850, tolerance: 40, unit: "umol/m2/s" },
        },
      },
    ],
    createdAt: 0,
    updatedAt: 0,
  };
}

function override(
  day: number,
  paramName: string,
  value: number,
): DayOverride {
  return {
    id: "",
    orgId: "",
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

function overridesFixture(): DayOverride[] {
  return [
    override(7, "temp_day", 26),
    override(7, "rh_day", 68),
    override(35, "ec_target", 2.4),
    override(50, "ppfd", 920),
    override(70, "vwc_target", 48),
  ];
}

describe("PlannerCanvas (P1)", () => {
  it("renders one phase block per phase with the right day range", () => {
    render(
      <PlannerCanvas
        recipe={recipeFixture()}
        overrides={overridesFixture()}
      />,
    );

    const blocks = screen.getAllByTestId("phase-block");
    expect(blocks).toHaveLength(3);

    expect(blocks[0]).toHaveAttribute("data-phase-name", "Early Veg");
    expect(blocks[0]).toHaveAttribute("data-start-day", "1");
    expect(blocks[0]).toHaveAttribute("data-end-day", "21");

    expect(blocks[1]).toHaveAttribute("data-phase-name", "Late Veg");
    expect(blocks[1]).toHaveAttribute("data-start-day", "22");
    expect(blocks[1]).toHaveAttribute("data-end-day", "42");

    expect(blocks[2]).toHaveAttribute("data-phase-name", "Flower");
    expect(blocks[2]).toHaveAttribute("data-start-day", "43");
    expect(blocks[2]).toHaveAttribute("data-end-day", "84");
  });

  it("renders one pin marker per day that has overrides", () => {
    render(
      <PlannerCanvas
        recipe={recipeFixture()}
        overrides={overridesFixture()}
      />,
    );

    const pins = screen.getAllByTestId("override-pin-marker");
    // Days 7, 35, 50, 70 — day 7 has 2 overrides but stacks into one
    // marker, so we expect 4 markers, not 5.
    expect(pins).toHaveLength(4);
    const pinDays = pins
      .map((p) => Number(p.getAttribute("data-day")))
      .sort((a, b) => a - b);
    expect(pinDays).toEqual([7, 35, 50, 70]);
  });

  it("surfaces the right summary badges in the recipe header", () => {
    render(
      <PlannerCanvas
        recipe={recipeFixture()}
        overrides={overridesFixture()}
      />,
    );

    expect(screen.getByTestId("cycle-day-count")).toHaveTextContent(
      "84-day cycle",
    );
    expect(screen.getByTestId("phase-count")).toHaveTextContent("3 phases");
    // 3 distinct params across the three phases: temp_day, rh_day, ppfd.
    expect(screen.getByTestId("param-count")).toHaveTextContent(
      "3 parameters",
    );
    expect(screen.getByTestId("override-count")).toHaveTextContent(
      "5 day-overrides",
    );
  });

  it("renders the day axis with at least the first and last ticks", () => {
    render(
      <PlannerCanvas
        recipe={recipeFixture()}
        overrides={overridesFixture()}
      />,
    );

    const ticks = screen.getAllByTestId("day-axis-tick");
    const dayValues = ticks.map((t) => Number(t.getAttribute("data-day")));
    expect(dayValues[0]).toBe(1);
    expect(dayValues[dayValues.length - 1]).toBe(84);
  });
});
