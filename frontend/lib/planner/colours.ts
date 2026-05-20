/**
 * Phase colour palette by canonical cultivation stage.
 *
 * Maps a free-form `phase_name` (operator-supplied — anything goes)
 * onto one of a small set of pre-baked Tailwind class pairs (`fill` for
 * the block background, `border` for the left/right edges).
 *
 * The match is a case-insensitive keyword scan; the first keyword that
 * appears in the phase name wins, with a neutral grey fallback when
 * nothing matches. Order matters — more specific tokens are checked
 * before broader ones (e.g. "late veg" before plain "veg") because
 * matchers run top-to-bottom.
 *
 * The full palette is from §7 of `docs/planner-redesign.md`.
 */

/** One stage's class bundle. */
export interface PhaseColour {
  /** Background fill, e.g. `bg-emerald-700/30`. */
  fill: string;
  /** Border accent for the block's left + right edges. */
  border: string;
  /** Stable identifier — used for snapshot tests and aria hints. */
  key: PhaseColourKey;
}

export type PhaseColourKey =
  | "propagation"
  | "veg-early"
  | "veg-late"
  | "stretch"
  | "flower"
  | "ripening"
  | "flush"
  | "fallback";

interface Matcher {
  key: PhaseColourKey;
  /** Lowercase keywords; the first hit wins. */
  keywords: string[];
}

/**
 * Match order — more specific first so e.g. "Late Veg" picks
 * `veg-late` rather than the broader `veg-early`. "flush" beats
 * "flower" because a flush phase still mentions "flower-flush" in some
 * recipes.
 */
const MATCHERS: Matcher[] = [
  { key: "flush", keywords: ["flush"] },
  { key: "ripening", keywords: ["ripen", "swell"] },
  { key: "propagation", keywords: ["propag", "clone", "seedling", "mother"] },
  { key: "veg-late", keywords: ["late veg", "veg 2", "bulking"] },
  { key: "veg-early", keywords: ["early veg", "veg 1"] },
  { key: "stretch", keywords: ["stretch", "transition"] },
  // "flower" / "bloom" must come AFTER "ripen" + "flush" because a
  // ripening or flush phase often still says "flower-ripen" etc.
  { key: "flower", keywords: ["flower", "bloom", "bulk"] },
];

const PALETTE: Record<PhaseColourKey, PhaseColour> = {
  propagation: {
    key: "propagation",
    fill: "bg-emerald-700/30",
    border: "border-emerald-500",
  },
  "veg-early": {
    key: "veg-early",
    fill: "bg-green-700/30",
    border: "border-green-500",
  },
  "veg-late": {
    key: "veg-late",
    fill: "bg-lime-700/30",
    border: "border-lime-500",
  },
  stretch: {
    key: "stretch",
    fill: "bg-amber-700/30",
    border: "border-amber-500",
  },
  flower: {
    key: "flower",
    fill: "bg-orange-700/30",
    border: "border-orange-500",
  },
  ripening: {
    key: "ripening",
    fill: "bg-red-700/30",
    border: "border-red-500",
  },
  flush: {
    key: "flush",
    fill: "bg-rose-900/30",
    border: "border-rose-500",
  },
  fallback: {
    key: "fallback",
    fill: "bg-zinc-700/30",
    border: "border-zinc-500",
  },
};

/** Pick a `PhaseColour` for an operator-supplied phase name. */
export function phaseColourFor(phaseName: string): PhaseColour {
  const lower = phaseName.toLowerCase();
  for (const m of MATCHERS) {
    if (m.keywords.some((k) => lower.includes(k))) {
      return PALETTE[m.key];
    }
  }
  return PALETTE.fallback;
}
