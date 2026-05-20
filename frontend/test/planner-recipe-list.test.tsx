import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithQuery } from "./render";
import type { GrowRecipe, GrowRecipesResponse } from "@/lib/types";

/**
 * Smoke test for the P1 recipe-list page at `/planner`.
 *
 * The page fetches the recipe list once, renders one card per recipe,
 * and surfaces the cycle-day / phase-count badges derived from each
 * recipe's phases. The `+ New recipe` button is disabled in P1.
 */

const listGrowRecipes = vi.fn();

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
    listGrowRecipes: (...args: unknown[]) => listGrowRecipes(...args),
  },
}));

import PlannerRecipeListPage from "@/app/planner/page";

function recipe(overrides: Partial<GrowRecipe> = {}): GrowRecipe {
  return {
    id: "rec-1",
    orgId: "open-crop-steering",
    name: "Cannabis 12-week",
    recipeType: ["cannabis"],
    description: "Twelve-week indoor cannabis baseline.",
    version: 1,
    isActive: true,
    estimatedTotalDurationDays: 84,
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
        targets: {},
      },
      {
        phaseName: "Late Veg",
        durationDays: 21,
        order: 2,
        startDay: 22,
        endDay: 42,
        targets: {},
      },
      {
        phaseName: "Flower",
        durationDays: 42,
        order: 3,
        startDay: 43,
        endDay: 84,
        targets: {},
      },
    ],
    createdAt: 0,
    updatedAt: 0,
    ...overrides,
  };
}

describe("PlannerRecipeListPage (P1)", () => {
  beforeEach(() => {
    listGrowRecipes.mockReset();
  });

  it("renders every recipe returned by the API as a card", async () => {
    listGrowRecipes.mockResolvedValue({
      growRecipes: [
        recipe({ id: "rec-1", name: "Cannabis 12-week" }),
        recipe({ id: "rec-2", name: "Lettuce 6-week" }),
      ],
    } satisfies GrowRecipesResponse);

    renderWithQuery(<PlannerRecipeListPage />);

    expect(await screen.findByText("Cannabis 12-week")).toBeInTheDocument();
    expect(screen.getByText("Lettuce 6-week")).toBeInTheDocument();
    const cards = screen.getAllByTestId("recipe-card-link");
    expect(cards).toHaveLength(2);
  });

  it("links each card to /planner/recipes/{id}", async () => {
    listGrowRecipes.mockResolvedValue({
      growRecipes: [recipe({ id: "rec-1" })],
    } satisfies GrowRecipesResponse);

    renderWithQuery(<PlannerRecipeListPage />);

    const card = await screen.findByTestId("recipe-card-link");
    expect(card).toHaveAttribute("href", "/planner/recipes/rec-1");
  });

  it("shows the derived cycle-day and phase-count badges per card", async () => {
    listGrowRecipes.mockResolvedValue({
      growRecipes: [recipe()],
    } satisfies GrowRecipesResponse);

    renderWithQuery(<PlannerRecipeListPage />);

    expect(await screen.findByText("84-day cycle")).toBeInTheDocument();
    expect(screen.getByText("3 phases")).toBeInTheDocument();
  });

  it("renders the empty state when no recipes exist", async () => {
    listGrowRecipes.mockResolvedValue({
      growRecipes: [],
    } satisfies GrowRecipesResponse);

    renderWithQuery(<PlannerRecipeListPage />);

    expect(
      await screen.findByText(/No recipes yet/i),
    ).toBeInTheDocument();
  });

  it("disables the New recipe button in P1", async () => {
    listGrowRecipes.mockResolvedValue({
      growRecipes: [],
    } satisfies GrowRecipesResponse);

    renderWithQuery(<PlannerRecipeListPage />);
    // The button exists but is disabled with the P5 tooltip.
    const btn = await screen.findByRole("button", {
      name: /Create a new recipe/i,
    });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("title", "Coming in P5");
  });
});
