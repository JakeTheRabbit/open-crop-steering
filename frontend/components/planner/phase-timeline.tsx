"use client";

import * as React from "react";
import { ZoomIn, ZoomOut } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { phaseColourFor } from "@/lib/planner/colours";
import { paramCategory, type ParamCategory } from "@/lib/planner/param-catalog";
import type { DayOverride, GrowRecipePhase } from "@/lib/types";
import { PhaseBlock } from "./phase-block";
import { OverridePinMarker, sortCategories } from "./override-pin-marker";

/**
 * Horizontal Gantt-style timeline for one grow recipe.
 *
 * Renders the day axis, every phase block end-to-end, and the override
 * pin markers above the axis. Owns horizontal scroll and a CSS-based
 * `pixelsPerDay` zoom (4-28 px/day) — no canvas, no virtualisation;
 * 84 days × 28 px/day = ~2,350 px wide, easily fits in a DOM ribbon.
 *
 * Selection is read-only in P1: `selected` is honoured for the ring
 * highlight, and the click handlers fire via the `onSelectPhase` /
 * `onSelectDay` callbacks, but the host page does not yet mount a side
 * panel. P2/P3 wrap this component in `<RecipeDraftProvider>` and
 * route the selection events through the reducer.
 */
export interface Selection {
  kind: "phase" | "day";
  /** When `kind === "phase"`, the 0-based phase index. */
  phaseIndex?: number;
  /** When `kind === "day"`, the 1-based day on the cycle axis. */
  day?: number;
}

export interface PhaseTimelineProps {
  phases: GrowRecipePhase[];
  cycleDayCount: number;
  overrides: DayOverride[];
  selected?: Selection | null;
  onSelectPhase?: (phaseIndex: number) => void;
  onSelectDay?: (day: number) => void;
}

/** Min / max / default values for the px-per-day zoom slider. */
const MIN_PX_PER_DAY = 4;
const MAX_PX_PER_DAY = 28;
const DEFAULT_PX_PER_DAY = 14;

/**
 * Pick a sensible axis tick interval given the current zoom.
 *
 * Goal: at most ~12 labelled ticks on a typical 84-day cycle so they
 * don't collide. Below 8 px/day the labels would overlap; we step up
 * to weekly / two-weekly ticks instead.
 */
function tickStep(pixelsPerDay: number, cycleDayCount: number): number {
  if (cycleDayCount <= 14) return 1;
  if (pixelsPerDay >= 22) return 7;
  if (pixelsPerDay >= 12) return 14;
  return 21;
}

/**
 * Bucket the overrides by day and project them to a per-day list of
 * unique param categories + their raw param names (for the hover
 * tooltip). The categories list is order-stable per `sortCategories`.
 */
interface PinBucket {
  day: number;
  categories: ParamCategory[];
  paramNames: string[];
}

function bucketByDay(overrides: DayOverride[]): PinBucket[] {
  const byDay = new Map<number, { cats: Set<ParamCategory>; params: string[] }>();
  for (const o of overrides) {
    let entry = byDay.get(o.day);
    if (!entry) {
      entry = { cats: new Set(), params: [] };
      byDay.set(o.day, entry);
    }
    entry.cats.add(paramCategory(o.paramName));
    entry.params.push(o.paramName);
  }
  return Array.from(byDay.entries())
    .sort((a, b) => a[0] - b[0])
    .map(([day, { cats, params }]) => ({
      day,
      categories: sortCategories(cats),
      // Dedupe + sort the param names for a stable tooltip / aria label.
      paramNames: Array.from(new Set(params)).sort(),
    }));
}

export function PhaseTimeline({
  phases,
  cycleDayCount,
  overrides,
  selected = null,
  onSelectPhase,
  onSelectDay,
}: PhaseTimelineProps) {
  const [pixelsPerDay, setPixelsPerDay] = React.useState<number>(
    DEFAULT_PX_PER_DAY,
  );

  const ribbonWidth = cycleDayCount * pixelsPerDay;
  const pins = React.useMemo(() => bucketByDay(overrides), [overrides]);
  const step = tickStep(pixelsPerDay, cycleDayCount);

  // Pre-compute the day axis tick days — week-aligned (so day 1 is
  // always labelled when step > 1; subsequent ticks land on day
  // 1 + n*step).
  const tickDays = React.useMemo<number[]>(() => {
    const days: number[] = [];
    for (let d = 1; d <= cycleDayCount; d += step) days.push(d);
    if (days[days.length - 1] !== cycleDayCount) days.push(cycleDayCount);
    return days;
  }, [cycleDayCount, step]);

  const zoomIn = () =>
    setPixelsPerDay((p) => Math.min(MAX_PX_PER_DAY, p + 2));
  const zoomOut = () =>
    setPixelsPerDay((p) => Math.max(MIN_PX_PER_DAY, p - 2));

  return (
    <div data-testid="phase-timeline" className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-2xs text-muted-foreground">
          {cycleDayCount}-day cycle · {phases.length} phases · {overrides.length}{" "}
          day-overrides
        </p>
        <div
          className="flex items-center gap-1"
          role="group"
          aria-label="Timeline zoom"
        >
          <Button
            size="icon"
            variant="ghost"
            onClick={zoomOut}
            disabled={pixelsPerDay <= MIN_PX_PER_DAY}
            aria-label="Zoom out timeline"
            className="h-7 w-7"
          >
            <ZoomOut className="h-3.5 w-3.5" />
          </Button>
          <span className="font-mono text-2xs text-muted-foreground">
            {pixelsPerDay}px/day
          </span>
          <Button
            size="icon"
            variant="ghost"
            onClick={zoomIn}
            disabled={pixelsPerDay >= MAX_PX_PER_DAY}
            aria-label="Zoom in timeline"
            className="h-7 w-7"
          >
            <ZoomIn className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      <div className="overflow-x-auto rounded-lg border border-border bg-card">
        <div
          className="relative"
          style={{
            width: ribbonWidth,
            minWidth: ribbonWidth,
          }}
        >
          {/* Pin overlay — sits above the day axis, ribbon-wide. */}
          <div
            data-testid="pin-overlay"
            className="relative h-9 border-b border-border/60"
            aria-hidden={pins.length === 0}
          >
            {pins.map((p) => (
              <OverridePinMarker
                key={p.day}
                day={p.day}
                pixelsPerDay={pixelsPerDay}
                categories={p.categories}
                paramNames={p.paramNames}
                onClick={() => onSelectDay?.(p.day)}
              />
            ))}
          </div>

          {/* Day axis. */}
          <div
            data-testid="day-axis"
            className="relative h-6 border-b border-border/60 text-2xs text-muted-foreground"
          >
            {tickDays.map((d) => {
              const isLast = d === cycleDayCount;
              const left = isLast
                ? (d - 1) * pixelsPerDay + pixelsPerDay
                : (d - 1) * pixelsPerDay + pixelsPerDay / 2;
              return (
                <button
                  key={d}
                  type="button"
                  onClick={() => onSelectDay?.(d)}
                  data-testid="day-axis-tick"
                  data-day={d}
                  className={cn(
                    "absolute top-0 -translate-x-1/2 pt-1 font-mono leading-none transition-colors hover:text-foreground",
                    selected?.kind === "day" && selected.day === d
                      ? "text-foreground"
                      : null,
                  )}
                  style={{ left }}
                  aria-label={`Day ${d}`}
                >
                  {d}
                </button>
              );
            })}
          </div>

          {/* Phase blocks. */}
          <div
            data-testid="phase-strip"
            className="relative h-14"
            style={{ width: ribbonWidth }}
          >
            {phases.map((phase, i) => {
              const colour = phaseColourFor(phase.phaseName);
              const isSelected =
                selected?.kind === "phase" && selected.phaseIndex === i;
              return (
                <PhaseBlock
                  key={`${phase.order}-${phase.phaseName}-${i}`}
                  phase={phase}
                  pixelsPerDay={pixelsPerDay}
                  colour={colour}
                  isSelected={isSelected}
                  onClick={() => onSelectPhase?.(i)}
                />
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
