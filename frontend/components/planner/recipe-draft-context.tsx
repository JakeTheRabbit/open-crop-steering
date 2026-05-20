"use client";

import * as React from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import type {
  DayOverride,
  DayOverrideInput,
  GrowRecipe,
  GrowRecipeUpdateBody,
} from "@/lib/types";

import {
  draftReducer,
  initDraftState,
  dirtyCount as computeDirtyCount,
  type Action,
  type DraftState,
} from "@/lib/planner/draft-reducer";
import { validate, type ValidationResult } from "@/lib/planner/validator";

/**
 * Recipe-draft React context (P2 of the planner redesign).
 *
 * Wraps the planner canvas with a `useReducer`-backed batched draft
 * (see `lib/planner/draft-reducer.ts`) and exposes the standard set
 * of imperative helpers via `useRecipeDraft()`:
 *
 *  - `state` — the live draft (recipe + overrides + undo/redo stacks).
 *  - `dispatch` — fire one of the {@link Action} variants.
 *  - `undo` / `redo` — convenience wrappers over `dispatch({type:'UNDO'})`.
 *  - `save` — the two-PUT flow (recipe header, then overrides). Returns
 *    a `SaveResult` describing which step succeeded.
 *  - `discard` — dispatch `RESET_TO_SAVED`.
 *  - `dirtyCount` — number of cells differing from `savedSnapshot`.
 *  - `findings` — memoised validator output.
 *  - `saveState` — `'idle' | 'saving' | 'partial' | 'ok'`.
 *  - `saveError` — last error (network, validation) from a failed PUT.
 *  - `retryOverrides` — re-runs step 2 only after a partial failure.
 *
 * The provider seeds itself from `recipe` + `overrides` props (which the
 * planner page resolves from TanStack Query), and re-seeds whenever
 * those identities change (a refetch lands or the user navigates to a
 * different recipe inside the same provider — both unusual today but
 * cheap to handle correctly).
 */

interface RecipeDraftContextValue {
  state: DraftState;
  dispatch: React.Dispatch<Action>;
  undo: () => void;
  redo: () => void;
  save: () => void;
  discard: () => void;
  retryOverrides: () => void;
  dirtyCount: number;
  findings: ValidationResult;
  saveState: SaveState;
  saveError: unknown;
}

export type SaveState = "idle" | "saving" | "partial" | "ok";

const RecipeDraftContext = React.createContext<
  RecipeDraftContextValue | undefined
>(undefined);

export interface RecipeDraftProviderProps {
  recipe: GrowRecipe;
  overrides: DayOverride[];
  children: React.ReactNode;
}

/**
 * Build a `GrowRecipeUpdateBody` from the current draft. Mirrors the
 * Pydantic `GrowRecipeCreate` shape (every mutable field of the base
 * recipe + genetics). Server-managed fields (`id`, `orgId`,
 * `createdAt`, `updatedAt`) are stripped.
 */
function recipeToUpdateBody(recipe: GrowRecipe): GrowRecipeUpdateBody {
  return {
    name: recipe.name,
    recipeType: recipe.recipeType,
    description: recipe.description ?? null,
    version: recipe.version ?? null,
    isActive: recipe.isActive,
    estimatedTotalDurationDays: recipe.estimatedTotalDurationDays ?? null,
    phases: recipe.phases,
    genetics: recipe.genetics ?? null,
    createdBy: recipe.createdBy ?? null,
    lastModifiedBy: recipe.lastModifiedBy ?? null,
  };
}

/** Bulk-replace input from a list of `DayOverride` rows. */
function overridesToInput(overrides: DayOverride[]): DayOverrideInput[] {
  return overrides.map((o) => ({
    day: o.day,
    paramName: o.paramName,
    value: o.value,
    tolerance: o.tolerance ?? null,
    unit: o.unit ?? null,
  }));
}

export function RecipeDraftProvider({
  recipe,
  overrides,
  children,
}: RecipeDraftProviderProps) {
  const [state, dispatch] = React.useReducer(
    draftReducer,
    undefined,
    () => initDraftState(recipe, overrides),
  );

  // Re-seed when the loaded recipe/overrides identities change — happens
  // after a successful save (the save flow invalidates the queries, and
  // the refetched data flows in here through the prop).
  const recipeId = recipe.id;
  React.useEffect(() => {
    dispatch({
      type: "LOAD_FROM_SERVER",
      recipe,
      overrides,
    });
    // We intentionally narrow the dep set — re-seeding on every
    // identity change of `recipe.phases` would defeat the purpose of
    // the batched draft. The query layer hands us a stable reference
    // until the next refetch; the planner page only re-renders this
    // provider when `recipeId` flips.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recipeId]);

  const undo = React.useCallback(() => dispatch({ type: "UNDO" }), []);
  const redo = React.useCallback(() => dispatch({ type: "REDO" }), []);
  const discard = React.useCallback(
    () => dispatch({ type: "RESET_TO_SAVED" }),
    [],
  );

  const dirtyCount = React.useMemo(() => computeDirtyCount(state), [state]);
  const findings = React.useMemo(
    () => validate(state.recipe, state.overrides),
    [state.recipe, state.overrides],
  );

  const queryClient = useQueryClient();

  // --- save flow ---------------------------------------------------
  const [saveState, setSaveState] = React.useState<SaveState>("idle");
  const [saveError, setSaveError] = React.useState<unknown>(null);
  // Track the recipe response that came back from step 1 so a Retry
  // overrides operation can re-fire step 2 without redoing step 1.
  const partialRecipeRef = React.useRef<GrowRecipe | null>(null);

  /**
   * Step 1: PUT the recipe header.
   * Step 2: PUT the bulk-replace overrides.
   *
   * Wired as TWO separate mutations so the partial-failure mode (step 2
   * fails after step 1 succeeded) can be surfaced explicitly — see
   * spec §5.
   */
  const updateRecipeMutation = useMutation({
    mutationFn: (body: GrowRecipeUpdateBody) =>
      api.updateGrowRecipe(recipeId, body),
  });
  const replaceOverridesMutation = useMutation({
    mutationFn: (overrides: DayOverrideInput[]) =>
      api.replaceDayOverrides(recipeId, { overrides }),
  });

  const finishSuccessfully = React.useCallback(
    async (savedRecipe: GrowRecipe) => {
      // Refresh the saved snapshot from the server's view of the
      // recipe — and refetch the overrides since they roundtripped
      // via bulk-replace (the response body is the echo, but the GET
      // hands back the full DayOverride[] with timestamps).
      await queryClient.invalidateQueries({
        queryKey: queryKeys.growRecipe(recipeId),
      });
      await queryClient.invalidateQueries({
        queryKey: queryKeys.recipeOverrides(recipeId),
      });
      // The dashboard + supervisor read effective-targets; if either is
      // mounted, force a refetch.
      await queryClient.invalidateQueries({
        queryKey: queryKeys.effectiveTargets(recipeId),
      });
      // List shows updated-at; invalidate it too.
      await queryClient.invalidateQueries({
        queryKey: queryKeys.growRecipes,
      });
      // Try to fetch the freshly-persisted overrides; if the GET round
      // trip fails for any reason, fall back to the in-memory draft —
      // we still want to clear the dirty count.
      let freshOverrides: DayOverride[] = state.overrides;
      try {
        freshOverrides = await api.listDayOverrides(recipeId);
      } catch {
        // Swallow — the save itself succeeded; the next render will
        // re-fetch via TanStack Query's normal flow.
      }
      dispatch({
        type: "LOAD_FROM_SERVER",
        recipe: savedRecipe,
        overrides: freshOverrides,
      });
      setSaveState("ok");
      setSaveError(null);
      partialRecipeRef.current = null;
    },
    [queryClient, recipeId, state.overrides],
  );

  const save = React.useCallback(() => {
    if (dirtyCount === 0) return;
    if (findings.hardRefusals.length > 0) return;
    setSaveState("saving");
    setSaveError(null);
    const body = recipeToUpdateBody(state.recipe);
    updateRecipeMutation.mutate(body, {
      onSuccess: (savedRecipe) => {
        partialRecipeRef.current = savedRecipe;
        const overrideBody = overridesToInput(state.overrides);
        replaceOverridesMutation.mutate(overrideBody, {
          onSuccess: () => {
            void finishSuccessfully(savedRecipe);
          },
          onError: (err) => {
            // Step-2 failed AFTER step-1 succeeded — the partial mode.
            setSaveState("partial");
            setSaveError(err);
          },
        });
      },
      onError: (err) => {
        // Step-1 failed; no server mutation happened. Draft stays dirty.
        setSaveState("idle");
        setSaveError(err);
      },
    });
  }, [
    dirtyCount,
    findings.hardRefusals.length,
    state.recipe,
    state.overrides,
    updateRecipeMutation,
    replaceOverridesMutation,
    finishSuccessfully,
  ]);

  const retryOverrides = React.useCallback(() => {
    const savedRecipe = partialRecipeRef.current;
    if (!savedRecipe) return;
    setSaveState("saving");
    setSaveError(null);
    const overrideBody = overridesToInput(state.overrides);
    replaceOverridesMutation.mutate(overrideBody, {
      onSuccess: () => {
        void finishSuccessfully(savedRecipe);
      },
      onError: (err) => {
        setSaveState("partial");
        setSaveError(err);
      },
    });
  }, [state.overrides, replaceOverridesMutation, finishSuccessfully]);

  const value = React.useMemo<RecipeDraftContextValue>(
    () => ({
      state,
      dispatch,
      undo,
      redo,
      save,
      discard,
      retryOverrides,
      dirtyCount,
      findings,
      saveState,
      saveError,
    }),
    [
      state,
      undo,
      redo,
      save,
      discard,
      retryOverrides,
      dirtyCount,
      findings,
      saveState,
      saveError,
    ],
  );

  return (
    <RecipeDraftContext.Provider value={value}>
      {children}
    </RecipeDraftContext.Provider>
  );
}

/**
 * Read the recipe-draft context.
 *
 * Throws when called outside a `<RecipeDraftProvider>` — the planner
 * canvas always mounts inside one, so an absent context is a wiring
 * bug, not a user-facing error.
 */
export function useRecipeDraft(): RecipeDraftContextValue {
  const ctx = React.useContext(RecipeDraftContext);
  if (!ctx) {
    throw new Error(
      "useRecipeDraft must be called inside <RecipeDraftProvider>",
    );
  }
  return ctx;
}

/** Cheap human-readable error string for the save bar. */
export function describeSaveError(error: unknown): string {
  if (error instanceof ApiError) {
    if (typeof error.detail === "string") return error.detail;
    return `Request failed (HTTP ${error.status}).`;
  }
  if (error instanceof Error) return error.message;
  return "Save failed.";
}
