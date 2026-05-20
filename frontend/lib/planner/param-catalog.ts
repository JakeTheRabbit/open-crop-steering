/**
 * Canonical parameter catalog for the planner.
 *
 * Mirrors `REQUIRED_PARAMS`, `_UNITS` and `_TOLERANCES` from
 * `backend/app/presets/cannabis_12week.py`. The same 11 parameters
 * drive the cannabis preset's per-day values, so the UI uses them as
 * the typeahead / pin-marker source of truth.
 *
 * Each entry carries:
 *
 * - `name`     — the wire-format `paramName` the backend persists.
 * - `category` — `env` | `light` | `irrigation` | `nutrient` | `other`,
 *                used to pick the pin-marker colour + shape per §7.
 * - `unit`     — default display unit (operator can still override on
 *                a per-cell basis).
 * - `tolerance` — sensible default tolerance band, sourced from
 *                `cannabis_12week._TOLERANCES`.
 * - `label`    — short human label (snake_case is fine for power users
 *                but the dropdown shows the prettified form).
 *
 * Soft-constrained: per Q2 in the spec, custom params are still
 * allowed; they come through as `category: "other"` with a hollow-pin
 * marker. P1 only consumes `category` for pin rendering — the rest is
 * here for the P2 editor.
 */

/** The colour/shape category for a parameter. */
export type ParamCategory =
  | "env"
  | "light"
  | "irrigation"
  | "nutrient"
  | "other";

/** Pin marker shape, set per category per §7 of the spec. */
export type ParamPinShape = "circle" | "triangle" | "square" | "diamond" | "ring";

export interface ParamCatalogEntry {
  name: string;
  label: string;
  category: ParamCategory;
  unit: string;
  tolerance: number;
}

/** Pin-marker shape for a category, used by the timeline overlay. */
export const PIN_SHAPE_BY_CATEGORY: Record<ParamCategory, ParamPinShape> = {
  env: "circle",
  light: "triangle",
  irrigation: "square",
  nutrient: "diamond",
  other: "ring",
};

/**
 * Pin-marker Tailwind class (background + border) for a category.
 *
 * Solid swatches for the four known categories, a transparent hollow
 * circle for "other" so a custom param is visually distinct from a
 * known one.
 */
export const PIN_TAILWIND_BY_CATEGORY: Record<ParamCategory, string> = {
  env: "bg-sky-400 border-sky-400",
  light: "bg-yellow-400 border-yellow-400",
  irrigation: "bg-blue-400 border-blue-400",
  nutrient: "bg-violet-400 border-violet-400",
  other: "bg-transparent border-zinc-300",
};

/**
 * The 11 canonical parameters used by the `cannabis_12week` preset.
 *
 * Order matches `cannabis_12week.REQUIRED_PARAMS` so a side-by-side
 * comparison with the backend file is trivial. Categories follow §7
 * of the spec.
 */
export const PARAM_CATALOG: readonly ParamCatalogEntry[] = [
  {
    name: "temp_day",
    label: "Temp (day)",
    category: "env",
    unit: "C",
    tolerance: 0.5,
  },
  {
    name: "temp_night",
    label: "Temp (night)",
    category: "env",
    unit: "C",
    tolerance: 0.5,
  },
  {
    name: "rh_day",
    label: "RH (day)",
    category: "env",
    unit: "%",
    tolerance: 3.0,
  },
  {
    name: "rh_night",
    label: "RH (night)",
    category: "env",
    unit: "%",
    tolerance: 3.0,
  },
  {
    name: "co2_day",
    label: "CO2 (day)",
    category: "env",
    unit: "ppm",
    tolerance: 75.0,
  },
  {
    name: "vpd_day",
    label: "VPD (day)",
    category: "env",
    unit: "kPa",
    tolerance: 0.1,
  },
  {
    name: "ppfd",
    label: "PPFD",
    category: "light",
    unit: "umol/m2/s",
    tolerance: 40.0,
  },
  {
    name: "photoperiod_hours",
    label: "Photoperiod",
    category: "light",
    unit: "h",
    tolerance: 0.0,
  },
  {
    name: "vwc_target",
    label: "VWC target",
    category: "irrigation",
    unit: "%",
    tolerance: 2.0,
  },
  {
    name: "ec_target",
    label: "EC target",
    category: "nutrient",
    unit: "mS/cm",
    tolerance: 0.2,
  },
  {
    name: "dryback_pct",
    label: "Dryback",
    category: "irrigation",
    unit: "%",
    tolerance: 2.0,
  },
] as const;

const _BY_NAME: Map<string, ParamCatalogEntry> = new Map(
  PARAM_CATALOG.map((p) => [p.name, p]),
);

/**
 * Look up a catalog entry by wire-format name.
 *
 * Returns `undefined` for unknown / custom params — the caller should
 * treat that as `category: "other"`.
 */
export function paramCatalogEntry(
  name: string,
): ParamCatalogEntry | undefined {
  return _BY_NAME.get(name);
}

/**
 * Resolve the `ParamCategory` for any param name, known or custom.
 *
 * Unknown params (i.e. anything outside the cannabis-preset 11) are
 * categorised as `"other"` so the pin-marker still renders.
 */
export function paramCategory(name: string): ParamCategory {
  return _BY_NAME.get(name)?.category ?? "other";
}
