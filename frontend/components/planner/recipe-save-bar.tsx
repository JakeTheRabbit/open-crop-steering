"use client";

import * as React from "react";
import { Check, Redo2, Save as SaveIcon, Trash, Undo2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";

import { describeSaveError, useRecipeDraft } from "./recipe-draft-context";
import { DiscardChangesDialog } from "./discard-changes-dialog";

/**
 * Sticky-bottom save bar.
 *
 * Surfaces the standard four affordances per spec §3:
 *
 *  - `N unsaved` counter (left).
 *  - Undo / Redo buttons (enabled when their stack is non-empty).
 *  - Discard button (opens `<DiscardChangesDialog>`).
 *  - Save button (gated on `dirtyCount > 0 && hardCount === 0`).
 *
 * On a partial-failure save (recipe PUT succeeded, override PUT failed)
 * the bar grows a "Retry overrides" affordance and a critical-toned
 * error row underneath. The success path swaps the Save icon for a
 * checkmark for a beat so the operator gets visible feedback before
 * the buttons go quiet.
 */
export function RecipeSaveBar() {
  const {
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
  } = useRecipeDraft();

  const hardCount = findings.hardRefusals.length;
  const undoDisabled = state.undoStack.length === 0;
  const redoDisabled = state.redoStack.length === 0;
  const isSaving = saveState === "saving";
  const isPartial = saveState === "partial";
  const isClean = dirtyCount === 0 && saveState !== "partial";
  const canSave = !isSaving && dirtyCount > 0 && hardCount === 0;
  const [discardOpen, setDiscardOpen] = React.useState(false);

  return (
    <div
      data-testid="recipe-save-bar"
      data-save-state={saveState}
      className={cn(
        "sticky bottom-0 z-30 flex flex-col gap-2 border-t border-border bg-card/95 px-3 py-2 backdrop-blur",
        isPartial ? "border-critical/40" : null,
      )}
    >
      <div className="flex items-center justify-between gap-3">
        <span
          data-testid="dirty-count"
          className={cn(
            "text-xs",
            dirtyCount > 0 ? "text-foreground" : "text-muted-foreground",
          )}
        >
          {isClean ? "All changes saved" : `${dirtyCount} unsaved edit${dirtyCount === 1 ? "" : "s"}`}
        </span>
        <div className="flex items-center gap-1.5">
          <Button
            size="sm"
            variant="ghost"
            onClick={undo}
            disabled={undoDisabled}
            aria-label="Undo"
            className="h-7 gap-1"
          >
            <Undo2 className="h-3.5 w-3.5" />
            Undo
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={redo}
            disabled={redoDisabled}
            aria-label="Redo"
            className="h-7 gap-1"
          >
            <Redo2 className="h-3.5 w-3.5" />
            Redo
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setDiscardOpen(true)}
            disabled={isSaving || dirtyCount === 0}
            data-testid="discard-changes-button"
            className="h-7 gap-1"
          >
            <Trash className="h-3.5 w-3.5" />
            Discard
          </Button>
          {isPartial ? (
            <Button
              size="sm"
              variant="default"
              onClick={retryOverrides}
              data-testid="retry-overrides-button"
              className="h-7 gap-1"
            >
              <SaveIcon className="h-3.5 w-3.5" />
              Retry overrides
            </Button>
          ) : (
            <Button
              size="sm"
              variant={saveState === "ok" ? "success" : "default"}
              onClick={save}
              disabled={!canSave}
              data-testid="save-button"
              className="h-7 gap-1"
            >
              {isSaving ? (
                <>
                  <Spinner />
                  Saving…
                </>
              ) : saveState === "ok" ? (
                <>
                  <Check className="h-3.5 w-3.5" />
                  Saved
                </>
              ) : (
                <>
                  <SaveIcon className="h-3.5 w-3.5" />
                  Save
                </>
              )}
            </Button>
          )}
        </div>
      </div>
      {saveError && saveState !== "ok" ? (
        <p
          data-testid="save-error"
          className="rounded-md border border-critical/30 bg-critical/10 px-2 py-1 text-2xs text-critical"
        >
          {isPartial
            ? `Recipe header saved but overrides failed: ${describeSaveError(saveError)}. Retry the overrides or discard.`
            : describeSaveError(saveError)}
        </p>
      ) : null}
      {hardCount > 0 ? (
        <p
          data-testid="hard-blocker"
          className="text-2xs text-critical"
        >
          {hardCount} hard finding{hardCount === 1 ? "" : "s"} blocks saving.
          Fix the highlighted phase{hardCount === 1 ? "" : "s"} to enable Save.
        </p>
      ) : null}

      <DiscardChangesDialog
        open={discardOpen}
        dirtyCount={dirtyCount}
        onClose={() => setDiscardOpen(false)}
        onConfirm={discard}
      />
    </div>
  );
}
