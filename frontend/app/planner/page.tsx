"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, Plus } from "lucide-react";

import { api } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import type { GrowRecipe } from "@/lib/types";
import { PageHeader } from "@/components/page-header";
import { QueryState } from "@/components/query-state";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

/**
 * Recipe-planner index page (P1 of the planner redesign).
 *
 * Replaces the legacy per-room picker. Recipes are now a first-class
 * entity (referenced from a batch's `growRecipe` field); the operator
 * picks a recipe to open the planner canvas at
 * `/planner/recipes/{recipeId}`.
 *
 * P1 scope: render the list (read-only). The "+ New recipe" button is
 * disabled with a "Coming in P5" tooltip — the new-from-preset dialog
 * lands in pass 5 per §9 of the spec.
 */

/** Sum a recipe's phase durations — cycle length is always derived. */
function cycleDayCount(recipe: GrowRecipe): number {
  return recipe.phases.reduce(
    (total, phase) => total + Math.floor(phase.durationDays),
    0,
  );
}

export default function PlannerRecipeListPage() {
  const recipesQuery = useQuery({
    queryKey: queryKeys.growRecipes,
    queryFn: ({ signal }) => api.listGrowRecipes(signal),
    refetchInterval: false,
  });

  const recipes = recipesQuery.data?.growRecipes ?? [];

  return (
    <div>
      <PageHeader
        title="Recipe Planner"
        description="Phase-bound cultivation recipes. Pick one to open the planner."
        actions={
          <Button
            size="sm"
            disabled
            title="Coming in P5"
            aria-label="Create a new recipe (disabled — coming in P5)"
          >
            <Plus className="h-3.5 w-3.5" />
            New recipe
          </Button>
        }
      />

      <QueryState
        isLoading={recipesQuery.isLoading}
        isError={recipesQuery.isError}
        error={recipesQuery.error}
        isEmpty={!recipesQuery.isLoading && recipes.length === 0}
        emptyMessage="No recipes yet. Create one from a preset to start planning."
      >
        <div
          data-testid="recipe-list"
          className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3"
        >
          {recipes.map((r) => {
            const days = cycleDayCount(r);
            const phaseCount = r.phases.length;
            return (
              <Link
                key={r.id}
                href={`/planner/recipes/${encodeURIComponent(r.id)}`}
                className="block"
                data-testid="recipe-card-link"
                data-recipe-id={r.id}
              >
                <Card className="transition-colors hover:border-primary/60">
                  <CardContent className="space-y-2 p-4">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-semibold">
                          {r.name}
                        </div>
                        {r.description ? (
                          <p className="mt-1 line-clamp-2 text-2xs text-muted-foreground">
                            {r.description}
                          </p>
                        ) : null}
                      </div>
                      <ArrowUpRight className="h-4 w-4 shrink-0 text-muted-foreground" />
                    </div>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Badge variant="secondary">{days}-day cycle</Badge>
                      <Badge variant="secondary">
                        {phaseCount} phase{phaseCount === 1 ? "" : "s"}
                      </Badge>
                      {!r.isActive ? (
                        <Badge variant="outline">inactive</Badge>
                      ) : null}
                    </div>
                  </CardContent>
                </Card>
              </Link>
            );
          })}
        </div>
      </QueryState>
    </div>
  );
}
