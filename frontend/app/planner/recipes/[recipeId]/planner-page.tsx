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
import { PhaseEditor } from "@/components/planner/phase-editor";
import {
  RecipeDraftProvider,
  useRecipeDraft,
} from "@/components/planner/recipe-draft-context";
import { RecipeSaveBar } from "@/components/planner/recipe-save-bar";
import { ValidationBanner } from "@/components/planner/validation-banner";
import type { Finding } from "@/lib/planner/validator";

/**
 * Recipe-canvas page.
 *
 * Resolves the `recipeId` route param, fires the `growRecipe(id)` +
 * `recipeOverrides(id)` queries in parallel, and once both land mounts
 * the canvas inside `<RecipeDraftProvider>` so every component reads
 * from a single batched draft.
 *
 * Layout (top → bottom):
 *
 *  - Page header (recipe name, link back to list).
 *  - Validation banner (when findings are present).
 *  - Summary card (cycle / phase / param / override counts).
 *  - Phase timeline (read-only ribbon — clicking a phase selects it).
 *  - Two-column body: phase editor (when selected) + day inspector
 *    slot (empty in P2; P3 fills it).
 *  - Sticky save bar.
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
 * Inner canvas — reads draft state from the provider context.
 *
 * Pulled out so the provider can wrap once and every child sees the
 * same draft without prop-drilling. `<PlannerCanvas>` is the public
 * boundary the page (and tests) mount.
 */
function CanvasInner() {
  const { state, findings } = useRecipeDraft();
  const recipe = state.recipe;
  const overrides = state.overrides;

  const [selected, setSelected] = React.useState<Selection | null>(null);

  const days = cycleDayCount(recipe);
  const params = paramCount(recipe);

  const handleFindingClick = React.useCallback((finding: Finding) => {
    if (finding.phaseIndex != null) {
      setSelected({ kind: "phase", phaseIndex: finding.phaseIndex });
      // The timeline tags each phase block with its index via the
      // `data-testid` + `data-phase-name` attributes; the simplest
      // scroll-target locator is "the nth phase-block in the canvas".
      if (typeof document !== "undefined") {
        const blocks = document.querySelectorAll<HTMLElement>(
          "[data-testid='phase-block']",
        );
        const target = blocks[finding.phaseIndex];
        if (target && typeof target.scrollIntoView === "function") {
          target.scrollIntoView({
            behavior: "smooth",
            block: "nearest",
            inline: "center",
          });
        }
      }
    } else if (finding.day != null) {
      setSelected({ kind: "day", day: finding.day });
    }
  }, []);

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

      <ValidationBanner
        findings={findings}
        onFindingClick={handleFindingClick}
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

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div data-testid="phase-editor-slot">
          {selected?.kind === "phase" && selected.phaseIndex != null ? (
            <PhaseEditor phaseIndex={selected.phaseIndex} />
          ) : (
            <p className="rounded-md border border-dashed border-border px-3 py-6 text-2xs text-muted-foreground">
              Click a phase on the timeline above to edit its defaults.
            </p>
          )}
        </div>
        <div data-testid="day-inspector-slot">
          {/* Day inspector lands in P3. For P2 the slot stays empty. */}
          <p className="rounded-md border border-dashed border-border px-3 py-6 text-2xs text-muted-foreground">
            Day inspector coming in the next planner pass.
          </p>
        </div>
      </div>

      <RecipeSaveBar />
    </div>
  );
}

/**
 * Mount the draft provider seeded by server data, then render the
 * canvas inside it.
 *
 * `key={recipe.id}` forces a fresh provider when the operator navigates
 * between recipes — the reducer's internal state isn't safe to carry
 * over across recipes (undo stack would contain phases from the
 * previous one).
 */
export function PlannerCanvas({ recipe, overrides }: CanvasProps) {
  return (
    <RecipeDraftProvider
      key={recipe.id}
      recipe={recipe}
      overrides={overrides}
    >
      <CanvasInner />
    </RecipeDraftProvider>
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
