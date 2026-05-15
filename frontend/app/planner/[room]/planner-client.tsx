"use client";

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, RotateCcw, Save } from "lucide-react";

import { api } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import type { RecipeDraftBody, RecipeParamCell, RecipeRevision } from "@/lib/types";
import { PageHeader } from "@/components/page-header";
import { QueryState, errorText } from "@/components/query-state";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  buildGridModel,
  cellKey,
  PlannerGrid,
} from "@/components/planner/planner-grid";

/**
 * Recipe planner for a single room.
 *
 * Loads the room's active recipe revision, presents it as an editable
 * day x parameter grid, and on Save creates a **new draft revision**
 * (recipes are immutable — the backend never mutates an approved
 * revision; a draft is a fresh row a cultivator later submits for QAP
 * approval). The room id is read from the route.
 */
export function PlannerClient() {
  const params = useParams<{ room: string }>();
  const room = decodeURIComponent(params?.room ?? "");
  const queryClient = useQueryClient();

  const recipeQuery = useQuery({
    queryKey: queryKeys.recipe(room),
    queryFn: () => api.getRecipe(room),
    enabled: room.length > 0 && room !== "room",
    refetchInterval: false,
  });

  // Working copy of edited values, keyed by `${day}:${param}`.
  const [edited, setEdited] = React.useState<Map<string, number>>(new Map());

  const model = React.useMemo(
    () => buildGridModel(recipeQuery.data?.params ?? []),
    [recipeQuery.data],
  );

  // Reset the working copy whenever a fresh revision loads.
  React.useEffect(() => {
    setEdited(new Map(model.values));
  }, [model.values]);

  const dirtyCount = React.useMemo(() => {
    let n = 0;
    for (const [k, v] of edited) {
      if (model.values.get(k) !== v) n += 1;
    }
    return n;
  }, [edited, model.values]);

  const handleEdit = React.useCallback(
    (dayIndex: number, paramName: string, value: number) => {
      setEdited((prev) => {
        const next = new Map(prev);
        next.set(cellKey(dayIndex, paramName), value);
        return next;
      });
    },
    [],
  );

  const saveMutation = useMutation({
    mutationFn: (body: RecipeDraftBody) => api.createRecipeDraft(body),
    onSuccess: (revision: RecipeRevision) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.recipe(room) });
      setSavedVersion(revision.version);
    },
  });
  const [savedVersion, setSavedVersion] = React.useState<number | null>(null);

  const handleSave = () => {
    const loaded = recipeQuery.data;
    if (!loaded) return;
    // Serialize the working copy back into flat param cells, carrying
    // the read-only tolerance + unit from the loaded revision.
    const cells: RecipeParamCell[] = [];
    for (const [key, value] of edited) {
      const [dayStr, param] = key.split(":");
      const meta = model.paramMeta[param ?? ""];
      cells.push({
        day_index: Number(dayStr),
        param_name: param ?? "",
        value,
        tolerance: meta?.tolerance ?? null,
        unit: meta?.unit ?? null,
      });
    }
    saveMutation.mutate({
      room_id: room,
      name: `${loaded.name} (draft)`,
      cycle_day_count: loaded.cycle_day_count,
      notes: `Draft from planner; based on v${loaded.version}.`,
      params: cells,
    });
  };

  const handleRevert = () => {
    setEdited(new Map(model.values));
    setSavedVersion(null);
  };

  if (!room || room === "room") {
    return (
      <div>
        <PageHeader title="Recipe Planner" />
        <p className="text-sm text-muted-foreground">
          No room selected. Pick a room from the{" "}
          <Link href="/" className="text-primary underline">
            dashboard
          </Link>
          .
        </p>
      </div>
    );
  }

  const loaded = recipeQuery.data;

  return (
    <div>
      <PageHeader
        title={`Recipe Planner — ${room}`}
        description={
          loaded
            ? `Active revision v${loaded.version} · ${loaded.cycle_day_count}-day cycle · ${model.paramNames.length} parameters`
            : "Editable day x parameter recipe grid."
        }
        actions={
          <>
            <Button asChild variant="ghost" size="sm">
              <Link href="/">
                <ArrowLeft className="h-3.5 w-3.5" />
                Dashboard
              </Link>
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={handleRevert}
              disabled={dirtyCount === 0 || saveMutation.isPending}
            >
              <RotateCcw className="h-3.5 w-3.5" />
              Revert
            </Button>
            <Button
              size="sm"
              onClick={handleSave}
              disabled={dirtyCount === 0 || saveMutation.isPending}
            >
              <Save className="h-3.5 w-3.5" />
              {saveMutation.isPending
                ? "Saving…"
                : `Save draft${dirtyCount > 0 ? ` (${dirtyCount})` : ""}`}
            </Button>
          </>
        }
      />

      {dirtyCount > 0 ? (
        <p className="mb-3 text-xs text-accent-foreground">
          <Badge variant="outline">{dirtyCount} unsaved</Badge> Save creates a
          new draft revision; the active recipe is unchanged until a QAP
          approves the draft.
        </p>
      ) : null}

      {saveMutation.isSuccess && savedVersion != null ? (
        <p className="mb-3 flex items-center gap-1.5 rounded-md border border-healthy/40 bg-healthy/10 px-3 py-2 text-xs text-healthy">
          <Check className="h-3.5 w-3.5" />
          Draft revision v{savedVersion} created.
        </p>
      ) : null}
      {saveMutation.isError ? (
        <p className="mb-3 rounded-md border border-critical/40 bg-critical/10 px-3 py-2 text-xs text-critical">
          Save failed: {errorText(saveMutation.error)}
        </p>
      ) : null}

      <QueryState
        isLoading={recipeQuery.isLoading}
        isError={recipeQuery.isError}
        error={recipeQuery.error}
        isEmpty={!!loaded && model.paramNames.length === 0}
        emptyMessage="This room's active recipe has no parameters."
        notFoundMessage={`No active recipe revision for ${room}. Seed one from the WEEK_DATA import, or this room may not be configured yet.`}
      >
        {loaded ? (
          <PlannerGrid
            paramNames={model.paramNames}
            dayCount={loaded.cycle_day_count}
            paramMeta={model.paramMeta}
            values={edited}
            original={model.values}
            onEdit={handleEdit}
            readOnly={saveMutation.isPending}
          />
        ) : null}
      </QueryState>
    </div>
  );
}
