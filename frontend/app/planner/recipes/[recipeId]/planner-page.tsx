"use client";

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";

import { api } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import type { DayOverride, GrowRecipe } from "@/lib/types";
import { PageHeader } from "@/components/page-header";
import { QueryState } from "@/components/query-state";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { PhaseTimeline, type Selection } from "@/components/planner/phase-timeline";

/**
 * Recipe-canvas page (P1).
 *
 * Resolves the `recipeId` route param, fires the `growRecipe(id)` +
 * `recipeOverrides(id)` queries in parallel, and once both land mounts
 * a `<PhaseTimeline>` driven by their data.
 *
 * P1 scope: read-only. Selection state lives here (so a click on a
 * phase / day visibly highlights it), but no editor side panel is
 * mounted yet — those are P2+. Wrapping in `<RecipeDraftProvider>` is
 * deferred to P2; this component is intentionally prop-driven so the
 * P2 swap is mechanical (replace the local queries with reducer state
 * sourced from context).
 */

interface CanvasProps {
  recipe: GrowRecipe;
  overrides: DayOverride[];
}

/** Sum a recipe's phase durations — cycle length is always derived. */
function cycleDayCount(recipe: GrowRecipe): number {
  return recipe.phases.reduce(
    (total, phase) => total + Math.floor(phase.durationDays),
    0,
  );
}

/** Count the distinct parameter names referenced in phase targets. */
function paramCount(recipe: GrowRecipe): number {
  const names = new Set<string>();
  for (const phase of recipe.phases) {
    for (const name of Object.keys(phase.targets ?? {})) {
      names.add(name);
    }
  }
  return names.size;
}

/**
 * The actual canvas — header, summary, timeline.
 *
 * Pulled out as its own component so the test fixture can render it
 * directly without going through `useParams` + TanStack Query. P2 will
 * wrap this in `<RecipeDraftProvider>` and replace the props with
 * `useRecipeDraft()` context.
 */
export function PlannerCanvas({ recipe, overrides }: CanvasProps) {
  const [selected, setSelected] = React.useState<Selection | null>(null);

  const days = cycleDayCount(recipe);
  const params = paramCount(recipe);

  return (
    <div className="space-y-4">
      <PageHeader
        title={`Recipe Planner — ${recipe.name}`}
        description={recipe.description ?? undefined}
        actions={
          <Button asChild variant="ghost" size="sm">
            <Link href="/planner">
              <ArrowLeft className="h-3.5 w-3.5" />
              Recipes
            </Link>
          </Button>
        }
      />

      <Card>
        <CardContent
          data-testid="recipe-summary"
          className="flex flex-wrap items-center gap-2 p-3"
        >
          <Badge variant="secondary" data-testid="cycle-day-count">
            {days}-day cycle
          </Badge>
          <Badge variant="secondary" data-testid="phase-count">
            {recipe.phases.length} phase
            {recipe.phases.length === 1 ? "" : "s"}
          </Badge>
          <Badge variant="secondary" data-testid="param-count">
            {params} parameter{params === 1 ? "" : "s"}
          </Badge>
          <Badge variant="secondary" data-testid="override-count">
            {overrides.length} day-override
            {overrides.length === 1 ? "" : "s"}
          </Badge>
          {!recipe.isActive ? (
            <Badge variant="outline">inactive</Badge>
          ) : null}
        </CardContent>
      </Card>

      <PhaseTimeline
        phases={recipe.phases}
        cycleDayCount={days}
        overrides={overrides}
        selected={selected}
        onSelectPhase={(phaseIndex) =>
          setSelected({ kind: "phase", phaseIndex })
        }
        onSelectDay={(day) => setSelected({ kind: "day", day })}
      />

      <p className="text-2xs text-muted-foreground">
        Read-only preview. Editing — phase defaults, day overrides, save /
        discard — lands in the next planner pass.
      </p>
    </div>
  );
}

/**
 * Top-level page component — runs the queries and renders the canvas.
 *
 * Kept thin so the data layer is easy to mock in tests; the canvas
 * itself is prop-driven via {@link PlannerCanvas}.
 */
export function PlannerPage() {
  const params = useParams<{ recipeId: string }>();
  const recipeId = decodeURIComponent(params?.recipeId ?? "");
  const isPlaceholder = recipeId === "" || recipeId === "recipe";

  const recipeQuery = useQuery({
    queryKey: queryKeys.growRecipe(recipeId),
    queryFn: ({ signal }) => api.getGrowRecipe(recipeId, signal),
    enabled: !isPlaceholder,
    refetchInterval: false,
  });

  const overridesQuery = useQuery({
    queryKey: queryKeys.recipeOverrides(recipeId),
    queryFn: ({ signal }) => api.listDayOverrides(recipeId, signal),
    enabled: !isPlaceholder,
    refetchInterval: false,
  });

  if (isPlaceholder) {
    return (
      <div>
        <PageHeader title="Recipe Planner" />
        <p className="text-sm text-muted-foreground">
          No recipe selected. Pick one from the{" "}
          <Link href="/planner" className="text-primary underline">
            recipe list
          </Link>
          .
        </p>
      </div>
    );
  }

  return (
    <QueryState
      isLoading={recipeQuery.isLoading || overridesQuery.isLoading}
      isError={recipeQuery.isError || overridesQuery.isError}
      error={recipeQuery.error ?? overridesQuery.error}
      notFoundMessage="Recipe not found — it may have been deleted."
    >
      {recipeQuery.data && overridesQuery.data ? (
        <PlannerCanvas
          recipe={recipeQuery.data}
          overrides={overridesQuery.data}
        />
      ) : null}
    </QueryState>
  );
}
