"use client";

import * as React from "react";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";

/**
 * Confirmation dialog for the discard-changes flow.
 *
 * Wraps the dependency-free `<Dialog>` primitive — opens on `open`,
 * fires `onConfirm` when the operator confirms, fires `onClose` for
 * cancel / backdrop / Escape. The actual reducer dispatch happens in
 * the parent (`<RecipeSaveBar>`) so this stays presentational.
 */
export interface DiscardChangesDialogProps {
  open: boolean;
  dirtyCount: number;
  onClose: () => void;
  onConfirm: () => void;
}

export function DiscardChangesDialog({
  open,
  dirtyCount,
  onClose,
  onConfirm,
}: DiscardChangesDialogProps) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Discard unsaved changes?"
      description={`This will rewind ${dirtyCount} unsaved edit${dirtyCount === 1 ? "" : "s"} to the last saved state. The undo history will also be cleared.`}
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            size="sm"
            onClick={() => {
              onConfirm();
              onClose();
            }}
            data-testid="discard-changes-confirm"
          >
            Discard {dirtyCount} edit{dirtyCount === 1 ? "" : "s"}
          </Button>
        </>
      }
    />
  );
}
