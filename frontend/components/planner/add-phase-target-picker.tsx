"use client";

import * as React from "react";
import { ChevronDown, Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { PARAM_CATALOG } from "@/lib/planner/param-catalog";

import { useRecipeDraft } from "./recipe-draft-context";

/**
 * Dropdown that adds a new parameter to a phase's targets map.
 *
 * Two-step UX:
 *
 *  1. Closed — a single `[+ Add parameter ▼]` button.
 *  2. Open  — picker form: a native `<select>` of `PARAM_CATALOG`
 *     entries (filtered to those not already on the phase), plus a
 *     `Custom param…` option that swaps the select for a free-text
 *     input for the parameter name. The user fills in value /
 *     tolerance / unit and clicks `Add`.
 *
 * Picking a catalog entry pre-populates the unit + tolerance fields
 * with the catalog defaults so the operator only has to type the value.
 */
export interface AddPhaseTargetPickerProps {
  phaseIndex: number;
  /** Param names already present on the phase — filtered out of the dropdown. */
  existingParamNames: string[];
}

/** Sentinel select value meaning "user wants to type a custom param name". */
const CUSTOM_SENTINEL = "__custom__";

export function AddPhaseTargetPicker({
  phaseIndex,
  existingParamNames,
}: AddPhaseTargetPickerProps) {
  const { dispatch } = useRecipeDraft();
  const [open, setOpen] = React.useState(false);
  const [selection, setSelection] = React.useState<string>("");
  const [customName, setCustomName] = React.useState<string>("");
  const [valueText, setValueText] = React.useState<string>("");
  const [toleranceText, setToleranceText] = React.useState<string>("");
  const [unitText, setUnitText] = React.useState<string>("");

  const existingSet = React.useMemo(
    () => new Set(existingParamNames),
    [existingParamNames],
  );
  const available = React.useMemo(
    () => PARAM_CATALOG.filter((p) => !existingSet.has(p.name)),
    [existingSet],
  );

  const resetForm = () => {
    setSelection("");
    setCustomName("");
    setValueText("");
    setToleranceText("");
    setUnitText("");
  };

  // When the user picks a catalog entry, pre-fill unit + tolerance so
  // they only have to type the value — the catalog defaults are the
  // sensible band for the cannabis preset.
  const handleSelection = (next: string) => {
    setSelection(next);
    if (next === CUSTOM_SENTINEL || next === "") return;
    const entry = PARAM_CATALOG.find((p) => p.name === next);
    if (!entry) return;
    setUnitText(entry.unit);
    setToleranceText(String(entry.tolerance));
  };

  const paramName =
    selection === CUSTOM_SENTINEL ? customName.trim() : selection.trim();
  const valueParsed = (() => {
    const t = valueText.trim();
    if (t === "") return null;
    const n = Number(t);
    return Number.isFinite(n) ? n : null;
  })();
  const canAdd =
    paramName.length > 0 &&
    paramName.length <= 64 &&
    !existingSet.has(paramName) &&
    valueParsed !== null;

  const commit = () => {
    if (!canAdd || valueParsed === null) return;
    const tolerance = (() => {
      const t = toleranceText.trim();
      if (t === "") return null;
      const n = Number(t);
      return Number.isFinite(n) ? n : null;
    })();
    const unit = unitText.trim() === "" ? null : unitText.trim();
    dispatch({
      type: "ADD_PHASE_TARGET",
      phaseIndex,
      paramName,
      value: valueParsed,
      tolerance,
      unit,
    });
    resetForm();
    setOpen(false);
  };

  if (!open) {
    return (
      <Button
        size="sm"
        variant="outline"
        onClick={() => setOpen(true)}
        data-testid="add-phase-target-toggle"
        className="h-7 gap-1.5"
      >
        <Plus className="h-3.5 w-3.5" />
        Add parameter
        <ChevronDown className="h-3 w-3 opacity-60" />
      </Button>
    );
  }

  return (
    <div
      className="rounded-md border border-dashed border-border bg-card/60 p-2"
      data-testid="add-phase-target-form"
    >
      <div className="mb-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
        <div>
          <label className="mb-1 block text-2xs text-muted-foreground">
            Parameter
          </label>
          <Select
            aria-label="Parameter name"
            value={selection}
            onChange={(e) => handleSelection(e.target.value)}
            className="h-7 text-xs"
          >
            <option value="">Choose…</option>
            {available.map((p) => (
              <option key={p.name} value={p.name}>
                {p.label} ({p.name})
              </option>
            ))}
            <option value={CUSTOM_SENTINEL}>Custom param…</option>
          </Select>
          {selection === CUSTOM_SENTINEL ? (
            <Input
              aria-label="Custom parameter name"
              value={customName}
              onChange={(e) => setCustomName(e.target.value)}
              placeholder="param_name"
              maxLength={64}
              className="mt-1 h-7 px-2 text-xs"
            />
          ) : null}
        </div>
        <div className="grid grid-cols-3 gap-1.5">
          <div>
            <label className="mb-1 block text-2xs text-muted-foreground">
              Value
            </label>
            <Input
              aria-label="Value"
              inputMode="decimal"
              value={valueText}
              onChange={(e) => setValueText(e.target.value)}
              className="h-7 px-2 text-xs"
            />
          </div>
          <div>
            <label className="mb-1 block text-2xs text-muted-foreground">
              Tol
            </label>
            <Input
              aria-label="Tolerance"
              inputMode="decimal"
              value={toleranceText}
              onChange={(e) => setToleranceText(e.target.value)}
              placeholder="±"
              className="h-7 px-2 text-xs"
            />
          </div>
          <div>
            <label className="mb-1 block text-2xs text-muted-foreground">
              Unit
            </label>
            <Input
              aria-label="Unit"
              value={unitText}
              onChange={(e) => setUnitText(e.target.value)}
              className="h-7 px-2 text-xs"
            />
          </div>
        </div>
      </div>
      <div className="flex justify-end gap-2">
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            resetForm();
            setOpen(false);
          }}
          className="h-7"
        >
          Cancel
        </Button>
        <Button
          size="sm"
          onClick={commit}
          disabled={!canAdd}
          className="h-7"
          data-testid="add-phase-target-submit"
        >
          Add
        </Button>
      </div>
    </div>
  );
}
