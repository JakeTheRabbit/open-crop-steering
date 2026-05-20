"use client";

import * as React from "react";

import { cn } from "@/lib/utils";
import type { PhaseColour } from "@/lib/planner/colours";
import type { GrowRecipePhase } from "@/lib/types";

/**
 * One coloured phase block on the timeline.
 *
 * Width = `(endDay - startDay + 1) * pixelsPerDay`. The block carries
 * the phase name, day range, and duration as visible text — colour is
 * supplementary, never the only signal (cf. §8 colour-blind notes).
 *
 * `isSelected` adds a stronger ring; in P1 the host page doesn't yet
 * surface a side editor when the click handler fires, so selection is
 * purely visual today (the prop is still drilled through so P2 can
 * wire it up to the reducer without touching this file).
 */
export interface PhaseBlockProps {
  phase: GrowRecipePhase;
  pixelsPerDay: number;
  colour: PhaseColour;
  isSelected?: boolean;
  onClick?: () => void;
}

export function PhaseBlock({
  phase,
  pixelsPerDay,
  colour,
  isSelected = false,
  onClick,
}: PhaseBlockProps) {
  // start_day / end_day are populated by the backend's post-validate
  // hook. Defensive fallbacks let the component render in tests that
  // build fixtures by hand without re-running the validator.
  const startDay = phase.startDay ?? 1;
  const endDay = phase.endDay ?? startDay + Math.floor(phase.durationDays) - 1;
  const days = endDay - startDay + 1;
  const width = days * pixelsPerDay;
  const left = (startDay - 1) * pixelsPerDay;
  const dayRangeLabel = days === 1 ? `d${startDay}` : `d${startDay}–${endDay}`;

  return (
    <button
      type="button"
      onClick={onClick}
      data-testid="phase-block"
      data-phase-name={phase.phaseName}
      data-start-day={startDay}
      data-end-day={endDay}
      aria-label={`Phase ${phase.phaseName}, days ${startDay} to ${endDay}, ${days} days. Click to edit.`}
      className={cn(
        "absolute top-0 flex h-full flex-col justify-center overflow-hidden rounded-sm border-l border-r px-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        colour.fill,
        colour.border,
        isSelected ? "ring-2 ring-primary" : "hover:brightness-110",
      )}
      style={{ left, width }}
    >
      <span className="block truncate text-xs font-semibold text-foreground">
        {phase.phaseName}
      </span>
      <span className="block truncate text-2xs text-muted-foreground">
        {dayRangeLabel}{" "}
        <span className="text-muted-foreground/70">({days}d)</span>
      </span>
    </button>
  );
}
