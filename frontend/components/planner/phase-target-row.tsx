"use client";

import * as React from "react";
import { Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { PhaseTargetSpec } from "@/lib/types";

import { useRecipeDraft } from "./recipe-draft-context";

/**
 * One row in a phase's flat target map.
 *
 * Three inline editors — value (number), tolerance (number, nullable),
 * unit (text) — plus a delete button. Keystrokes are debounced 250ms
 * before dispatching `SET_PHASE_TARGET`; pressing Enter or losing focus
 * commits immediately.
 *
 * The component owns a *local* draft string for each field so the
 * operator can type freely (including transient invalid states like
 * "" or "2.") without the reducer rejecting the partial value.
 */
export interface PhaseTargetRowProps {
  phaseIndex: number;
  paramName: string;
  target: PhaseTargetSpec;
  /** Optional override of the default 250ms debounce — handy for tests. */
  debounceMs?: number;
}

const DEFAULT_DEBOUNCE_MS = 250;

/** Format a nullable number for the input value. */
function fmt(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  return Number.isFinite(value) ? String(value) : "";
}

/** Parse a number that the operator typed; `null` for empty / invalid. */
function parseNullable(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

export function PhaseTargetRow({
  phaseIndex,
  paramName,
  target,
  debounceMs = DEFAULT_DEBOUNCE_MS,
}: PhaseTargetRowProps) {
  const { dispatch } = useRecipeDraft();

  const [valueDraft, setValueDraft] = React.useState<string>(fmt(target.value));
  const [toleranceDraft, setToleranceDraft] = React.useState<string>(
    fmt(target.tolerance ?? null),
  );
  const [unitDraft, setUnitDraft] = React.useState<string>(target.unit ?? "");

  // Re-sync the local drafts when the canonical state changes (e.g.
  // undo / discard rewinds the draft).
  React.useEffect(() => {
    setValueDraft(fmt(target.value));
  }, [target.value]);
  React.useEffect(() => {
    setToleranceDraft(fmt(target.tolerance ?? null));
  }, [target.tolerance]);
  React.useEffect(() => {
    setUnitDraft(target.unit ?? "");
  }, [target.unit]);

  /**
   * Push the current draft state into the reducer (debounced).
   *
   * We deliberately read from refs so the timer fires with the latest
   * drafts even if the parent re-renders during the wait window.
   */
  const draftRef = React.useRef({
    value: valueDraft,
    tolerance: toleranceDraft,
    unit: unitDraft,
  });
  draftRef.current = {
    value: valueDraft,
    tolerance: toleranceDraft,
    unit: unitDraft,
  };

  const commit = React.useCallback(() => {
    const { value, tolerance, unit } = draftRef.current;
    const parsedValue = parseNullable(value);
    if (parsedValue === null) return; // value is required — refuse to commit.
    dispatch({
      type: "SET_PHASE_TARGET",
      phaseIndex,
      paramName,
      value: parsedValue,
      tolerance: parseNullable(tolerance),
      unit: unit.trim() === "" ? null : unit.trim(),
    });
  }, [dispatch, phaseIndex, paramName]);

  // Per-row debounce timer.
  const timerRef = React.useRef<number | null>(null);
  const schedule = React.useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
    }
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      commit();
    }, debounceMs);
  }, [commit, debounceMs]);

  // Cancel any pending timer on unmount so a deleted row doesn't fire
  // after we've already removed it.
  React.useEffect(
    () => () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
      }
    },
    [],
  );

  const remove = () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    dispatch({ type: "REMOVE_PHASE_TARGET", phaseIndex, paramName });
  };

  return (
    <tr data-testid="phase-target-row" data-param-name={paramName}>
      <th className="text-left font-mono text-2xs font-medium text-foreground">
        {paramName}
      </th>
      <td className="px-1">
        <Input
          aria-label={`Value for ${paramName}`}
          inputMode="decimal"
          value={valueDraft}
          className="h-7 px-2 text-xs"
          onChange={(e) => {
            setValueDraft(e.target.value);
            schedule();
          }}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              (e.target as HTMLInputElement).blur();
            }
          }}
        />
      </td>
      <td className="px-1">
        <Input
          aria-label={`Tolerance for ${paramName}`}
          inputMode="decimal"
          value={toleranceDraft}
          className="h-7 px-2 text-xs"
          placeholder="±"
          onChange={(e) => {
            setToleranceDraft(e.target.value);
            schedule();
          }}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              (e.target as HTMLInputElement).blur();
            }
          }}
        />
      </td>
      <td className="px-1">
        <Input
          aria-label={`Unit for ${paramName}`}
          value={unitDraft}
          className="h-7 px-2 text-xs"
          onChange={(e) => {
            setUnitDraft(e.target.value);
            schedule();
          }}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              (e.target as HTMLInputElement).blur();
            }
          }}
        />
      </td>
      <td className="px-1">
        <Button
          size="icon"
          variant="ghost"
          aria-label={`Remove ${paramName}`}
          onClick={remove}
          className="h-7 w-7"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </td>
    </tr>
  );
}
