"use client";

import * as React from "react";

import { cn } from "@/lib/utils";
import {
  PIN_SHAPE_BY_CATEGORY,
  PIN_TAILWIND_BY_CATEGORY,
  type ParamCategory,
} from "@/lib/planner/param-catalog";

/**
 * One day's override pin overlay.
 *
 * Renders up to three small shape markers stacked vertically above a
 * day on the timeline axis, then a `+N` badge when more than three
 * categories are pinned on the same day. Colour + shape encode the
 * param category per §7 of the planner spec; the textual list of
 * pinned params is exposed via the host `<button>`'s `title`
 * attribute so the operator can hover for the full breakdown.
 *
 * In P1 the click handler is wired but the host page does not yet
 * mount a side panel — clicking is a no-op the user can see. P2/P3
 * will surface the day inspector when this fires.
 */
export interface OverridePinMarkerProps {
  /** 1-based day on the cycle axis. */
  day: number;
  /** Pixel width of one day on the axis — keeps the marker centred. */
  pixelsPerDay: number;
  /**
   * The override categories present on this day, deduplicated and in
   * stable display order (env → light → irrigation → nutrient → other).
   * Length ≥ 1.
   */
  categories: ParamCategory[];
  /** The raw param names — surfaced in the hover `title`. */
  paramNames: string[];
  onClick?: () => void;
}

const CATEGORY_ORDER: ParamCategory[] = [
  "env",
  "light",
  "irrigation",
  "nutrient",
  "other",
];

/** Stable ordering helper — also lets snapshots stay deterministic. */
export function sortCategories(
  cats: Iterable<ParamCategory>,
): ParamCategory[] {
  const seen = new Set<ParamCategory>(cats);
  return CATEGORY_ORDER.filter((c) => seen.has(c));
}

/** One small shape — `data-shape` lets the test target it. */
function PinShape({ category }: { category: ParamCategory }) {
  const shape = PIN_SHAPE_BY_CATEGORY[category];
  const fill = PIN_TAILWIND_BY_CATEGORY[category];
  const base = "block h-2 w-2 border";
  switch (shape) {
    case "circle":
      return (
        <span
          data-shape="circle"
          data-category={category}
          className={cn(base, "rounded-full", fill)}
        />
      );
    case "triangle":
      // CSS triangle via clip-path. Tailwind has no shorthand; use
      // inline `clipPath` to avoid a custom class.
      return (
        <span
          data-shape="triangle"
          data-category={category}
          className={cn(base, "border-0", fill)}
          style={{ clipPath: "polygon(50% 0%, 100% 100%, 0% 100%)" }}
        />
      );
    case "square":
      return (
        <span
          data-shape="square"
          data-category={category}
          className={cn(base, fill)}
        />
      );
    case "diamond":
      return (
        <span
          data-shape="diamond"
          data-category={category}
          className={cn(base, "rotate-45", fill)}
        />
      );
    case "ring":
    default:
      return (
        <span
          data-shape="ring"
          data-category={category}
          className={cn(base, "rounded-full", fill)}
        />
      );
  }
}

export function OverridePinMarker({
  day,
  pixelsPerDay,
  categories,
  paramNames,
  onClick,
}: OverridePinMarkerProps) {
  const visible = categories.slice(0, 3);
  const overflow = Math.max(0, categories.length - 3);
  const title = `Day ${day}: ${paramNames.join(", ")}`;
  // Centre the marker over the day cell. The host renders pins above
  // the axis with `position: absolute` over a 0..cycleDayCount-wide
  // strip; we sit at `(day - 0.5) * pixelsPerDay`, so the centre of
  // the marker aligns with the centre of the day's column.
  const left = (day - 0.5) * pixelsPerDay;
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      data-testid="override-pin-marker"
      data-day={day}
      aria-label={`Override pin on day ${day} for ${paramNames.join(", ")}. Click to inspect day.`}
      className="absolute -translate-x-1/2 cursor-pointer rounded p-0.5 hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      style={{ left, bottom: 0 }}
    >
      <span className="flex flex-col items-center gap-0.5">
        {visible.map((cat, i) => (
          <PinShape key={`${cat}-${i}`} category={cat} />
        ))}
        {overflow > 0 ? (
          <span className="rounded bg-zinc-700/80 px-1 text-[8px] font-semibold leading-tight text-zinc-100">
            +{overflow}
          </span>
        ) : null}
      </span>
    </button>
  );
}
