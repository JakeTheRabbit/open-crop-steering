"use client";

import * as React from "react";
import { Trash2, Split as SplitIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

import { AddPhaseTargetPicker } from "./add-phase-target-picker";
import { PhaseTargetRow } from "./phase-target-row";
import { useRecipeDraft } from "./recipe-draft-context";

/**
 * The selected-phase editor side panel.
 *
 * Renders the form spec'd in §3 of `planner-redesign.md`:
 *
 *  - Phase name (text)
 *  - Duration in days (numeric; re-stamps subsequent phases via the
 *    reducer's prefix-sum pass — no drag-to-resize in P2)
 *  - Light cycle (hoursOn / hoursOff pair)
 *  - Environmental band (min/max per temperature, humidity, co2, vpd)
 *  - Param-default table (one `<PhaseTargetRow>` per entry)
 *  - "+ Add parameter" picker
 *  - `[Split phase here…]` (disabled in P2; P4 wires the dialog)
 *  - `[Delete]` (wired — `DELETE_PHASE` action merges this phase's
 *    days into the previous one)
 *
 * Inputs are debounced (250ms) for the same reason the target rows
 * are — fast typists shouldn't blow up the undo stack with one entry
 * per keystroke.
 */
export interface PhaseEditorProps {
  phaseIndex: number;
  /** Optional override of the input debounce — handy for tests. */
  debounceMs?: number;
}

const DEFAULT_DEBOUNCE_MS = 250;
const ENV_KEYS = ["temperature", "humidity", "co2", "vpd"] as const;
type EnvKey = (typeof ENV_KEYS)[number];

const ENV_LABEL: Record<EnvKey, string> = {
  temperature: "Temperature",
  humidity: "Humidity",
  co2: "CO2",
  vpd: "VPD",
};

function parseFiniteNumber(text: string): number | null {
  const t = text.trim();
  if (t === "") return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

/**
 * Debounced text input that commits its value on a delay or on blur.
 *
 * Pulled out so the four scalar inputs (name, duration, light hours
 * on/off) and the eight env-band cells don't each repeat the
 * useState / useEffect / useRef plumbing.
 */
function DebouncedInput({
  ariaLabel,
  value,
  onCommit,
  inputMode,
  debounceMs,
  className,
  type,
  min,
  step,
}: {
  ariaLabel: string;
  value: string;
  onCommit: (next: string) => void;
  inputMode?: "decimal" | "numeric" | "text";
  debounceMs: number;
  className?: string;
  type?: "text" | "number";
  min?: number;
  step?: number;
}) {
  const [draft, setDraft] = React.useState<string>(value);
  React.useEffect(() => {
    setDraft(value);
  }, [value]);

  const draftRef = React.useRef(draft);
  draftRef.current = draft;

  const timerRef = React.useRef<number | null>(null);
  const schedule = React.useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
    }
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      onCommit(draftRef.current);
    }, debounceMs);
  }, [debounceMs, onCommit]);

  React.useEffect(
    () => () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
      }
    },
    [],
  );

  return (
    <Input
      aria-label={ariaLabel}
      type={type}
      inputMode={inputMode}
      value={draft}
      min={min}
      step={step}
      className={className}
      onChange={(e) => {
        setDraft(e.target.value);
        schedule();
      }}
      onBlur={() => {
        if (timerRef.current !== null) {
          window.clearTimeout(timerRef.current);
          timerRef.current = null;
        }
        onCommit(draftRef.current);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          (e.target as HTMLInputElement).blur();
        }
      }}
    />
  );
}

export function PhaseEditor({
  phaseIndex,
  debounceMs = DEFAULT_DEBOUNCE_MS,
}: PhaseEditorProps) {
  const { state, dispatch } = useRecipeDraft();
  const phase = state.recipe.phases[phaseIndex];

  if (!phase) {
    return null;
  }

  const env = phase.environmentalTargets ?? null;
  const lightCycle = phase.lightCycle ?? null;
  const targets = phase.targets ?? {};
  const targetNames = Object.keys(targets).sort();

  // --- name -----------------------------------------------------------
  const commitName = React.useCallback(
    (next: string) => {
      const trimmed = next.trim();
      if (trimmed === "" || trimmed === phase.phaseName) return;
      dispatch({ type: "RENAME_PHASE", phaseIndex, name: trimmed });
    },
    [dispatch, phaseIndex, phase.phaseName],
  );

  // --- duration -------------------------------------------------------
  const commitDuration = React.useCallback(
    (next: string) => {
      const n = parseFiniteNumber(next);
      if (n === null) return;
      const days = Math.max(1, Math.floor(n));
      if (days === Math.floor(phase.durationDays)) return;
      dispatch({ type: "SET_PHASE_DURATION", phaseIndex, days });
    },
    [dispatch, phaseIndex, phase.durationDays],
  );

  // --- light cycle ----------------------------------------------------
  const currentHoursOn = lightCycle?.hoursOn ?? 0;
  const currentHoursOff = lightCycle?.hoursOff ?? 0;
  const commitLightCycle = React.useCallback(
    (which: "on" | "off", next: string) => {
      const n = parseFiniteNumber(next);
      if (n === null) return;
      const hoursOn = which === "on" ? n : currentHoursOn;
      const hoursOff = which === "off" ? n : currentHoursOff;
      if (hoursOn === currentHoursOn && hoursOff === currentHoursOff) return;
      dispatch({
        type: "SET_PHASE_LIGHT_CYCLE",
        phaseIndex,
        hoursOn,
        hoursOff,
      });
    },
    [dispatch, phaseIndex, currentHoursOn, currentHoursOff],
  );

  // --- env band -------------------------------------------------------
  const commitEnvBand = React.useCallback(
    (key: EnvKey, which: "min" | "max", next: string) => {
      const n = parseFiniteNumber(next);
      if (n === null) return;
      const existing = env?.[key] ?? { min: 0, max: 0 };
      const min = which === "min" ? n : existing.min;
      const max = which === "max" ? n : existing.max;
      if (min === existing.min && max === existing.max) return;
      dispatch({
        type: "SET_PHASE_ENV_BAND",
        phaseIndex,
        key,
        min,
        max,
      });
    },
    [dispatch, phaseIndex, env],
  );

  const handleDelete = () => {
    // The reducer refuses to delete the only remaining phase, so we
    // gate the button on the same condition; no confirm prompt — the
    // operator can undo immediately.
    if (state.recipe.phases.length <= 1) return;
    dispatch({ type: "DELETE_PHASE", phaseIndex });
  };

  const dayRange = `d${phase.startDay ?? 1}-${phase.endDay ?? Math.floor(phase.durationDays)}`;
  return (
    <Card data-testid="phase-editor" data-phase-index={phaseIndex}>
      <CardContent className="space-y-3 p-3 text-xs">
        <div className="flex items-baseline justify-between gap-2">
          <h3 className="text-sm font-semibold">SELECTED PHASE</h3>
          <span className="text-2xs text-muted-foreground">{dayRange}</span>
        </div>

        <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_120px]">
          <label className="block">
            <span className="mb-1 block text-2xs text-muted-foreground">
              Name
            </span>
            <DebouncedInput
              ariaLabel="Phase name"
              value={phase.phaseName}
              onCommit={commitName}
              debounceMs={debounceMs}
              className="h-7 px-2 text-xs"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-2xs text-muted-foreground">
              Duration (days)
            </span>
            <DebouncedInput
              ariaLabel="Phase duration in days"
              type="number"
              min={1}
              step={1}
              value={String(Math.floor(phase.durationDays))}
              onCommit={commitDuration}
              debounceMs={debounceMs}
              className="h-7 px-2 text-xs"
            />
          </label>
        </div>

        <fieldset
          data-testid="phase-light-cycle"
          className="rounded-md border border-border/60 px-2 pb-2 pt-1"
        >
          <legend className="px-1 text-2xs text-muted-foreground">
            Light cycle
          </legend>
          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="mb-1 block text-2xs text-muted-foreground">
                Hours on
              </span>
              <DebouncedInput
                ariaLabel="Hours on"
                inputMode="decimal"
                value={String(currentHoursOn)}
                onCommit={(text) => commitLightCycle("on", text)}
                debounceMs={debounceMs}
                className="h-7 px-2 text-xs"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-2xs text-muted-foreground">
                Hours off
              </span>
              <DebouncedInput
                ariaLabel="Hours off"
                inputMode="decimal"
                value={String(currentHoursOff)}
                onCommit={(text) => commitLightCycle("off", text)}
                debounceMs={debounceMs}
                className="h-7 px-2 text-xs"
              />
            </label>
          </div>
        </fieldset>

        <fieldset
          data-testid="phase-env-band"
          className="rounded-md border border-border/60 px-2 pb-2 pt-1"
        >
          <legend className="px-1 text-2xs text-muted-foreground">
            Environmental band
          </legend>
          <div className="space-y-1.5">
            {ENV_KEYS.map((key) => {
              const cell = env?.[key] ?? null;
              return (
                <div
                  key={key}
                  className="grid grid-cols-[80px_1fr_1fr] items-center gap-2"
                  data-testid={`env-row-${key}`}
                >
                  <span className="text-2xs font-medium text-muted-foreground">
                    {ENV_LABEL[key]}
                  </span>
                  <DebouncedInput
                    ariaLabel={`${ENV_LABEL[key]} min`}
                    inputMode="decimal"
                    value={cell ? String(cell.min) : ""}
                    onCommit={(text) => commitEnvBand(key, "min", text)}
                    debounceMs={debounceMs}
                    className="h-7 px-2 text-xs"
                  />
                  <DebouncedInput
                    ariaLabel={`${ENV_LABEL[key]} max`}
                    inputMode="decimal"
                    value={cell ? String(cell.max) : ""}
                    onCommit={(text) => commitEnvBand(key, "max", text)}
                    debounceMs={debounceMs}
                    className="h-7 px-2 text-xs"
                  />
                </div>
              );
            })}
          </div>
        </fieldset>

        <div data-testid="phase-targets" className="space-y-2">
          <h4 className="text-2xs uppercase tracking-wide text-muted-foreground">
            Parameter defaults
          </h4>
          {targetNames.length === 0 ? (
            <p className="text-2xs text-muted-foreground">
              No phase defaults — every day in this phase will need a pin to
              have an effective target.
            </p>
          ) : (
            <table className="w-full border-separate border-spacing-y-1">
              <thead>
                <tr className="text-2xs uppercase tracking-wide text-muted-foreground">
                  <th className="text-left">Parameter</th>
                  <th className="text-left">Value</th>
                  <th className="text-left">Tol</th>
                  <th className="text-left">Unit</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {targetNames.map((paramName) => {
                  const target = targets[paramName];
                  if (!target) return null;
                  return (
                    <PhaseTargetRow
                      key={paramName}
                      phaseIndex={phaseIndex}
                      paramName={paramName}
                      target={target}
                      debounceMs={debounceMs}
                    />
                  );
                })}
              </tbody>
            </table>
          )}
          <AddPhaseTargetPicker
            phaseIndex={phaseIndex}
            existingParamNames={targetNames}
          />
        </div>

        <div className="flex justify-between gap-2 border-t border-border/60 pt-2">
          <Button
            size="sm"
            variant="ghost"
            disabled
            title="Phase split lands in P4"
            data-testid="phase-split-button"
            className="h-7 gap-1.5"
          >
            <SplitIcon className="h-3.5 w-3.5" />
            Split phase here…
          </Button>
          <Button
            size="sm"
            variant="destructive"
            onClick={handleDelete}
            disabled={state.recipe.phases.length <= 1}
            data-testid="phase-delete-button"
            className="h-7 gap-1.5"
          >
            <Trash2 className="h-3.5 w-3.5" />
            Delete
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
