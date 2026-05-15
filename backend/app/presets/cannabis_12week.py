"""Canonical 12-week (84-day) cannabis cultivation recipe.

This is the public repo's flagship example recipe — a credible
seed/clone -> harvest progression for indoor medicinal cannabis. It is
*structured Python data*: :func:`build_cannabis_12week` returns the flat
``params`` list that :func:`app.core.recipe_store.create_revision`
expects.

Agronomic basis
---------------
The progression and the coupling logic are grounded in
``plans/cultivation_knowledge.md`` (the distilled coupled-systems mental
model). That document deliberately avoids hard setpoint numbers — it
states only the *relationships*. The numbers below are conventional
indoor-cannabis starting points chosen to *respect* those relationships;
they are explicitly expected to be tuned by the operator / QAP per
cultivar (cultivation_knowledge.md Section 8).

Three stages over 12 weeks (cultivation_knowledge.md Section 1, control
hierarchy: set biological demand first, then match climate, then root
zone):

* **Early veg — weeks 1-3 (days 1-21).** Rooting clones / young plants.
  18 h photoperiod. Low PPFD (200 -> 450) so the small canopy is not a
  heat trap (EC-001/EC-003: leaf temp must stay below air temp). Warm,
  humid (RH 65-70 %) -> *low* VPD (~0.8 kPa) because immature root
  systems cannot transpire hard. No CO2 enrichment: below the PPFD
  threshold, enrichment is wasted (EC-006 / AP-05). Substrate kept wet
  with shallow drybacks; low feed EC so salts do not concentrate in a
  small root mass (EC-010/EC-012).

* **Late veg — weeks 4-6 (days 22-42).** Vegetative bulking, still
  18 h. PPFD ramps 500 -> 750; CO2 enrichment switched on (~900 ppm)
  once PPFD clears the light-limiting threshold (EC-006). RH steps down
  to ~60 % and VPD rises toward ~1.1 kPa as transpiration capacity
  grows. Feed EC and dryback both increase, gradually, never together
  aggressively (AP-12 / EC-012). Note: elevated CO2 *reduces*
  transpiration (cultivation_knowledge.md Section 1 counter-intuitive
  coupling) which partly offsets the higher PPFD load.

* **Flower — weeks 7-12 (days 43-84).** Flip to 12 h photoperiod on
  day 43. PPFD peaks (~850-950) early/mid bloom then eases for ripening.
  CO2 held high (~1000-1200 ppm) while PPFD supports it, then withdrawn
  in the final week. RH driven down progressively (60 % -> 45 %) to
  suppress botrytis on dense flower (EC-009); night RH kept a notch
  lower than day, and night temp is *not* dropped so far that the
  coldest surface crosses the dew point (EC-008 / AP-09). VPD climbs to
  ~1.3-1.5 kPa. Feed EC peaks mid-bloom then a deliberate flush-style
  taper in the final ~2 weeks; drybacks deepen for generative steering
  but stay bounded so substrate EC does not run away (EC-010 / AP-06).

Per-day parameters emitted (units in parentheses):

================  ====  ============================================
param_name        unit  meaning
================  ====  ============================================
temp_day          C     air temp, lights-on
temp_night        C     air temp, lights-off
rh_day            %     relative humidity, lights-on
rh_night          %     relative humidity, lights-off
co2_day           ppm   CO2 setpoint, lights-on (ambient ~420 = off)
vpd_day           kPa   vapour-pressure deficit target, lights-on
ppfd              umol  canopy PPFD, lights-on
photoperiod_hours h     hours of light per 24 h
vwc_target        %     substrate volumetric water content target
ec_target         mS/cm feed EC target
dryback_pct       %     target overnight substrate dryback
================  ====  ============================================

Tolerances are stage defaults (plan locked decision #16: v0.1 uses
stage-default tolerances; per-day editing is a v0.2 UI feature).
"""

from __future__ import annotations

from typing import TypedDict

CYCLE_DAYS = 84
WEEKS = 12
DAYS_PER_WEEK = 7

# Parameters every day must carry, in display order.
REQUIRED_PARAMS: tuple[str, ...] = (
    "temp_day",
    "temp_night",
    "rh_day",
    "rh_night",
    "co2_day",
    "vpd_day",
    "ppfd",
    "photoperiod_hours",
    "vwc_target",
    "ec_target",
    "dryback_pct",
)


class _StageWeek(TypedDict):
    """Setpoints anchored at a single week of the cycle.

    Daily rows are linearly interpolated *between* consecutive week
    anchors so the recipe ramps smoothly rather than stepping. Day 1 of
    week N uses week N's anchor exactly; intermediate days blend toward
    week N+1.
    """

    week: int
    temp_day: float
    temp_night: float
    rh_day: float
    rh_night: float
    co2_day: float
    vpd_day: float
    ppfd: float
    photoperiod_hours: float
    vwc_target: float
    ec_target: float
    dryback_pct: float


# Week anchors. Weeks 1-3 early veg, 4-6 late veg, 7-12 flower.
# A trailing week-13 anchor lets week-12 days interpolate without
# clamping; it is never emitted (loop stops at day 84).
_WEEK_ANCHORS: list[_StageWeek] = [
    # ---- Early veg (18 h photoperiod) -----------------------------------
    {
        "week": 1,
        "temp_day": 25.0,
        "temp_night": 23.0,
        "rh_day": 70.0,
        "rh_night": 70.0,
        "co2_day": 420.0,
        "vpd_day": 0.80,
        "ppfd": 220.0,
        "photoperiod_hours": 18.0,
        "vwc_target": 62.0,
        "ec_target": 1.4,
        "dryback_pct": 6.0,
    },
    {
        "week": 2,
        "temp_day": 25.5,
        "temp_night": 23.0,
        "rh_day": 68.0,
        "rh_night": 68.0,
        "co2_day": 420.0,
        "vpd_day": 0.85,
        "ppfd": 320.0,
        "photoperiod_hours": 18.0,
        "vwc_target": 60.0,
        "ec_target": 1.6,
        "dryback_pct": 8.0,
    },
    {
        "week": 3,
        "temp_day": 26.0,
        "temp_night": 22.5,
        "rh_day": 65.0,
        "rh_night": 64.0,
        "co2_day": 420.0,
        "vpd_day": 0.95,
        "ppfd": 450.0,
        "photoperiod_hours": 18.0,
        "vwc_target": 58.0,
        "ec_target": 1.8,
        "dryback_pct": 10.0,
    },
    # ---- Late veg (18 h photoperiod, CO2 enrichment on) -----------------
    {
        "week": 4,
        "temp_day": 26.5,
        "temp_night": 22.0,
        "rh_day": 62.0,
        "rh_night": 60.0,
        "co2_day": 900.0,
        "vpd_day": 1.05,
        "ppfd": 550.0,
        "photoperiod_hours": 18.0,
        "vwc_target": 55.0,
        "ec_target": 2.0,
        "dryback_pct": 12.0,
    },
    {
        "week": 5,
        "temp_day": 27.0,
        "temp_night": 21.5,
        "rh_day": 60.0,
        "rh_night": 58.0,
        "co2_day": 950.0,
        "vpd_day": 1.10,
        "ppfd": 650.0,
        "photoperiod_hours": 18.0,
        "vwc_target": 53.0,
        "ec_target": 2.2,
        "dryback_pct": 14.0,
    },
    {
        "week": 6,
        "temp_day": 27.0,
        "temp_night": 21.0,
        "rh_day": 58.0,
        "rh_night": 56.0,
        "co2_day": 1000.0,
        "vpd_day": 1.15,
        "ppfd": 750.0,
        "photoperiod_hours": 18.0,
        "vwc_target": 52.0,
        "ec_target": 2.4,
        "dryback_pct": 16.0,
    },
    # ---- Flower (12 h photoperiod from day 43) --------------------------
    {
        "week": 7,
        "temp_day": 27.0,
        "temp_night": 20.5,
        "rh_day": 58.0,
        "rh_night": 55.0,
        "co2_day": 1100.0,
        "vpd_day": 1.20,
        "ppfd": 850.0,
        "photoperiod_hours": 12.0,
        "vwc_target": 52.0,
        "ec_target": 2.6,
        "dryback_pct": 18.0,
    },
    {
        "week": 8,
        "temp_day": 26.5,
        "temp_night": 20.0,
        "rh_day": 55.0,
        "rh_night": 52.0,
        "co2_day": 1200.0,
        "vpd_day": 1.25,
        "ppfd": 920.0,
        "photoperiod_hours": 12.0,
        "vwc_target": 50.0,
        "ec_target": 2.8,
        "dryback_pct": 20.0,
    },
    {
        "week": 9,
        "temp_day": 26.0,
        "temp_night": 19.5,
        "rh_day": 52.0,
        "rh_night": 49.0,
        "co2_day": 1200.0,
        "vpd_day": 1.30,
        "ppfd": 920.0,
        "photoperiod_hours": 12.0,
        "vwc_target": 48.0,
        "ec_target": 2.8,
        "dryback_pct": 22.0,
    },
    {
        "week": 10,
        "temp_day": 25.5,
        "temp_night": 19.0,
        "rh_day": 50.0,
        "rh_night": 47.0,
        "co2_day": 1100.0,
        "vpd_day": 1.35,
        "ppfd": 880.0,
        "photoperiod_hours": 12.0,
        "vwc_target": 46.0,
        "ec_target": 2.6,
        "dryback_pct": 24.0,
    },
    {
        "week": 11,
        "temp_day": 24.5,
        "temp_night": 18.5,
        "rh_day": 47.0,
        "rh_night": 45.0,
        "co2_day": 900.0,
        "vpd_day": 1.40,
        "ppfd": 800.0,
        "photoperiod_hours": 12.0,
        "vwc_target": 44.0,
        "ec_target": 2.0,
        "dryback_pct": 26.0,
    },
    {
        "week": 12,
        "temp_day": 23.5,
        "temp_night": 18.0,
        "rh_day": 45.0,
        "rh_night": 43.0,
        "co2_day": 420.0,
        "vpd_day": 1.45,
        "ppfd": 700.0,
        "photoperiod_hours": 12.0,
        "vwc_target": 42.0,
        "ec_target": 1.2,
        "dryback_pct": 28.0,
    },
    # Trailing anchor — week-12 days interpolate toward this; not emitted.
    {
        "week": 13,
        "temp_day": 23.5,
        "temp_night": 18.0,
        "rh_day": 45.0,
        "rh_night": 43.0,
        "co2_day": 420.0,
        "vpd_day": 1.45,
        "ppfd": 700.0,
        "photoperiod_hours": 12.0,
        "vwc_target": 42.0,
        "ec_target": 1.2,
        "dryback_pct": 28.0,
    },
]

# Stage-default tolerance bands per parameter (plan locked decision #16).
# Wider on noisy/slow params (CO2, VWC), tighter on tightly-held ones.
_TOLERANCES: dict[str, float] = {
    "temp_day": 0.5,
    "temp_night": 0.5,
    "rh_day": 3.0,
    "rh_night": 3.0,
    "co2_day": 75.0,
    "vpd_day": 0.10,
    "ppfd": 40.0,
    "photoperiod_hours": 0.0,
    "vwc_target": 2.0,
    "ec_target": 0.2,
    "dryback_pct": 2.0,
}

_UNITS: dict[str, str] = {
    "temp_day": "C",
    "temp_night": "C",
    "rh_day": "%",
    "rh_night": "%",
    "co2_day": "ppm",
    "vpd_day": "kPa",
    "ppfd": "umol/m2/s",
    "photoperiod_hours": "h",
    "vwc_target": "%",
    "ec_target": "mS/cm",
    "dryback_pct": "%",
}

# Parameters that step rather than ramp — interpolating a photoperiod
# would emit nonsensical fractional-hour days around the flower flip.
_STEP_PARAMS: frozenset[str] = frozenset({"photoperiod_hours"})


def _interpolate(start: float, end: float, frac: float) -> float:
    """Linear blend ``start`` -> ``end`` at fraction ``frac`` in [0, 1]."""
    return start + (end - start) * frac


def _round_for(param_name: str, value: float) -> float:
    """Round to a sensible precision for the parameter."""
    if param_name in ("vpd_day",):
        return round(value, 2)
    if param_name in ("ec_target",):
        return round(value, 2)
    if param_name in ("temp_day", "temp_night"):
        return round(value, 1)
    if param_name == "photoperiod_hours":
        return round(value, 1)
    return round(value, 0)


def build_cannabis_12week() -> list[dict[str, object]]:
    """Build the 84-day cannabis recipe as a ``create_revision`` params list.

    Expands the 12 week anchors into one row per ``(day_index,
    param_name)``. Continuous parameters are linearly interpolated
    between consecutive week anchors so the recipe ramps smoothly;
    stepped parameters (photoperiod) hold their week value.

    Returns:
        A flat list of ``{day_index, param_name, value, tolerance,
        unit}`` dicts — exactly the shape
        :func:`app.core.recipe_store.create_revision` consumes. Length is
        ``84 * len(REQUIRED_PARAMS)`` (924 rows). ``day_index`` is
        1-based.
    """
    anchors_by_week = {a["week"]: a for a in _WEEK_ANCHORS}
    params: list[dict[str, object]] = []

    for day_index in range(1, CYCLE_DAYS + 1):
        # Week 1 covers days 1-7, week 2 days 8-14, ...
        week = (day_index - 1) // DAYS_PER_WEEK + 1
        day_in_week = (day_index - 1) % DAYS_PER_WEEK  # 0..6
        frac = day_in_week / DAYS_PER_WEEK

        anchor = anchors_by_week[week]
        next_anchor = anchors_by_week[week + 1]

        for param_name in REQUIRED_PARAMS:
            start = float(anchor[param_name])  # type: ignore[literal-required]
            if param_name in _STEP_PARAMS:
                value = start
            else:
                end = float(next_anchor[param_name])  # type: ignore[literal-required]
                value = _interpolate(start, end, frac)

            params.append(
                {
                    "day_index": day_index,
                    "param_name": param_name,
                    "value": _round_for(param_name, value),
                    "tolerance": _TOLERANCES[param_name],
                    "unit": _UNITS[param_name],
                }
            )

    return params
