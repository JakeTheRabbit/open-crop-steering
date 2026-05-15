"use client";

import * as React from "react";

import { cn, fmtNum } from "@/lib/utils";
import type { RecipeParamCell } from "@/lib/types";

/**
 * The recipe planner grid.
 *
 * A dense day x parameter spreadsheet of an 84-day cultivation recipe —
 * a React port of the original setpoints planner canvas. Rows are
 * parameters, columns are cycle days; each cell is an editable numeric
 * value. Tolerance bands are shown read-only beneath the value (the
 * band editor is a v0.2 deliverable per the plan).
 *
 * State model
 * -----------
 * The grid is *controlled*: the parent owns the working copy as a
 * `Map<"day:param", number>` of edited values and the original cells.
 * `onEdit` fires on every committed cell change; the parent diffs
 * against the loaded revision to decide whether Save is enabled.
 */

/** Stable cell key. */
export function cellKey(dayIndex: number, paramName: string): string {
  return `${dayIndex}:${paramName}`;
}

export interface PlannerGridProps {
  /** Distinct parameter names, in display order (rows). */
  paramNames: string[];
  /** Cycle day count (columns; days are 1-indexed for display). */
  dayCount: number;
  /** Unit + tolerance per parameter, keyed by param name. */
  paramMeta: Record<string, { unit: string | null; tolerance: number | null }>;
  /** Current working values, keyed by {@link cellKey}. */
  values: Map<string, number>;
  /** Original loaded values, keyed by {@link cellKey} — for change marks. */
  original: Map<string, number>;
  /** Fired when a cell value is committed (blur / Enter). */
  onEdit: (dayIndex: number, paramName: string, value: number) => void;
  /** When true, cells are not editable (e.g. while saving). */
  readOnly?: boolean;
}

/** Build the grid model from a flat list of recipe param cells. */
export function buildGridModel(cells: RecipeParamCell[]): {
  paramNames: string[];
  paramMeta: PlannerGridProps["paramMeta"];
  values: Map<string, number>;
} {
  const paramNames: string[] = [];
  const paramMeta: PlannerGridProps["paramMeta"] = {};
  const values = new Map<string, number>();
  for (const cell of cells) {
    if (!paramNames.includes(cell.param_name)) {
      paramNames.push(cell.param_name);
      paramMeta[cell.param_name] = {
        unit: cell.unit,
        tolerance: cell.tolerance,
      };
    }
    values.set(cellKey(cell.day_index, cell.param_name), cell.value);
  }
  paramNames.sort();
  return { paramNames, paramMeta, values };
}

function GridCell({
  dayIndex,
  paramName,
  value,
  changed,
  readOnly,
  onEdit,
}: {
  dayIndex: number;
  paramName: string;
  value: number | undefined;
  changed: boolean;
  readOnly: boolean;
  onEdit: PlannerGridProps["onEdit"];
}) {
  const [draft, setDraft] = React.useState<string>(
    value == null ? "" : String(value),
  );
  React.useEffect(() => {
    setDraft(value == null ? "" : String(value));
  }, [value]);

  const commit = () => {
    const parsed = Number(draft);
    if (draft.trim() !== "" && Number.isFinite(parsed) && parsed !== value) {
      onEdit(dayIndex, paramName, parsed);
    } else {
      setDraft(value == null ? "" : String(value));
    }
  };

  return (
    <td className="border border-border p-0">
      <input
        aria-label={`day ${dayIndex} ${paramName}`}
        className={cn(
          "tabular h-7 w-16 bg-transparent px-1 text-center text-xs outline-none focus:bg-primary/15 focus:ring-1 focus:ring-ring",
          changed && "bg-accent/20 font-semibold text-accent-foreground",
          readOnly && "cursor-not-allowed text-muted-foreground",
        )}
        value={draft}
        readOnly={readOnly}
        inputMode="decimal"
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            (e.target as HTMLInputElement).blur();
          }
        }}
      />
    </td>
  );
}

export function PlannerGrid({
  paramNames,
  dayCount,
  paramMeta,
  values,
  original,
  onEdit,
  readOnly = false,
}: PlannerGridProps) {
  const days = React.useMemo(
    () => Array.from({ length: dayCount }, (_, i) => i + 1),
    [dayCount],
  );

  return (
    <div className="overflow-auto rounded-lg border border-border">
      <table className="border-collapse" data-testid="planner-grid">
        <thead className="sticky top-0 z-10 bg-card">
          <tr>
            <th className="sticky left-0 z-20 min-w-36 border border-border bg-card px-2 py-1.5 text-left text-2xs uppercase tracking-wide text-muted-foreground">
              Parameter
            </th>
            {days.map((d) => (
              <th
                key={d}
                className="border border-border px-1 py-1.5 text-2xs font-medium text-muted-foreground"
              >
                D{d}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {paramNames.map((param) => {
            const meta = paramMeta[param];
            return (
              <tr key={param}>
                <th className="sticky left-0 z-10 border border-border bg-card px-2 py-1 text-left">
                  <div className="text-xs font-medium">{param}</div>
                  <div className="text-2xs text-muted-foreground">
                    {meta?.unit ? `${meta.unit} · ` : ""}
                    {meta?.tolerance != null
                      ? `±${fmtNum(meta.tolerance)}`
                      : "no band"}
                  </div>
                </th>
                {days.map((d) => {
                  const key = cellKey(d, param);
                  const v = values.get(key);
                  const o = original.get(key);
                  return (
                    <GridCell
                      key={key}
                      dayIndex={d}
                      paramName={param}
                      value={v}
                      changed={v !== o}
                      readOnly={readOnly}
                      onEdit={onEdit}
                    />
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
