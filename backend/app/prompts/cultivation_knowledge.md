# Cultivation Knowledge — Cannabis Grow Room Systems

**Source:** `D:\Onedrive\Desktop\cannabis-grow-room-systems-guide.html`
**Purpose:** Loaded by the AI supervisor as durable domain context. Cited in the system prompt. Also drives the room-configuration wizard's coupling validations and the equipment-saturation predicates.

**Use this for:**
- AI supervisor system prompt (mental model, control hierarchy, anti-patterns)
- Room-configuration wizard (equipment-coupling validation rules)
- Equipment-saturation predicates (turning sensor patterns into IMPAIRED/CRITICAL transitions)
- Validator anti-patterns (don't propose changes that violate coupling rules)

**Don't use this for:** stage-specific setpoint values (the document deliberately avoids hard numbers — those come from `presets/cannabis_12week.py` plus operator/QAP tuning).

---

## 1. Core mental model (mandatory)

### Forward chain
```
Light → leaf temperature → VPD → transpiration → dryback → substrate EC →
  nutrient/water stress → photosynthesis → CO₂ uptake → HVAC/dehum load → disease risk
```

### Reverse chain
```
Humidity / dehumidification → VPD → transpiration → dryback → EC → stomata →
  CO₂ uptake → growth
```

### Control hierarchy (enforced order)
1. **Set biological demand first** — crop stage, PPFD, CO₂, plant density.
2. **Match climate capacity** — cooling, dehum, airflow sizing must support the demand.
3. **Match root-zone strategy** — irrigation frequency, feed EC, runoff aligned with VPD/transpiration.
4. **Validate with measured responses** — read failures from sensor patterns, not visual guessing.

### Counter-intuitive coupling (most-often-missed)
- **Elevated CO₂ REDUCES transpiration** (and stomatal conductance) while increasing photosynthesis and water-use efficiency. Common assumption is the opposite.
- **Light is different**: increased PPFD INCREASES transpiration, CO₂ demand, and HVAC load.
- **Dehumidifiers convert latent to sensible heat**: removing moisture warms the room. AC must absorb that heat. Without reheat, AC over-cools to dehumidify.
- **Negative pressure pulls outside air** that dilutes CO₂ enrichment AND imports unconditioned T/RH/particles.

---

## 2. Equipment-coupling rules (validator + room-config wizard MUST enforce)

These are the rules a proposal/configuration must respect. Violations → reject (validator) or warn (config wizard).

| Rule ID | Coupling | Rule |
|---|---|---|
| EC-001 | PPFD ↔ HVAC | If PPFD setpoint increases by ≥X%, room must have enough cooling headroom OR the change requires HVAC re-sizing review |
| EC-002 | PPFD ↔ Dehum | Higher PPFD → higher transpiration → higher dehum load. Same headroom check |
| EC-003 | PPFD ↔ Leaf temp | Higher PPFD raises leaf temp; air-temp setpoint must give leaf-temp room (typically leaf < air on Class A growth, leaf > air = bleach risk) |
| EC-004 | Dehum ↔ Reheat | If dehum is standalone (no reheat), AC must compensate for sensible heat conversion. Without reheat, dehum + AC fight each other on VPD |
| EC-005 | CO₂ ↔ Exhaust | CO₂ enrichment with active exhaust = fighting yourself. Suppress exhaust during enrichment unless heat/RH emergency |
| EC-006 | CO₂ ↔ Light | CO₂ enrichment only pays if PPFD is sufficient (light not limiting). Below threshold PPFD, enrichment is wasted |
| EC-007 | CO₂ ↔ Negative pressure | Negative-pressure rooms leak conditioned air; CO₂ enrichment ROI suffers |
| EC-008 | RH ↔ Night temp | Low night temp + residual transpiration = condensation trap even at acceptable RH. Dew point math must be checked against coldest surface |
| EC-009 | Airflow ↔ Disease | Dense canopy without under-canopy airflow = humidity pockets even when room sensor reads OK. Botrytis risk |
| EC-010 | Dryback ↔ EC | Faster dryback → substrate EC concentrates. If feed EC is constant, runoff EC drifts up. Need runoff data to distinguish |
| EC-011 | Runoff ↔ Salt accumulation | Insufficient runoff = EC accumulates regardless of feed EC. Some runoff is mandatory (rough rule of thumb: 10-20%, varies) |
| EC-012 | VPD ↔ Osmotic stress | High VPD + high substrate EC = combined edge-burn / water-stress risk. Cannot run aggressive dryback AND high feed EC |
| EC-013 | Late irrigation ↔ Night RH | Irrigation event near lights-off causes RH spike during dark period. Last-shot timing must respect lights-off + dehum capacity |
| EC-014 | Sensor placement ↔ AC stability | Deadband tuning depends on sensor location. Bad placement causes oscillation or hidden problems |
| EC-015 | Exhaust ↔ Pathogen ingress | Unfiltered intake imports pathogens / spores. Filtration required for biosecurity |

---

## 3. Equipment-saturation indicators (drive HEALTHY → IMPAIRED transitions)

Encode these as predicates in `core/equipment.py`. When pattern fires → equipment marked saturated → AI proposals for that equipment's domain are suppressed.

| Indicator ID | Equipment | Pattern (sensor evidence) | Outcome |
|---|---|---|---|
| SAT-AC | AC | `climate.fan_mode == 'high'` for ≥15 min AND temp_actual > temp_setpoint AND temp slope > 0 | AC saturated. Don't propose lower temp setpoint. Suggest accepting +X°C OR escalate hardware constraint |
| SAT-DEHU | Dehumidifier | Dehum switch on for ≥20 min AND RH_actual > RH_setpoint + 3% AND RH slope flat or positive | Dehu saturated. Don't propose lower RH. Suggest accepting +X% RH OR investigate transpiration source |
| SAT-CO2 | CO₂ injection | CO₂_actual < 0.9 × CO₂_setpoint for ≥5 min AND solenoid_on_duty > 50% over window | CO₂ supply at limit OR room too leaky. Don't propose higher CO₂. Suggest checking room tightness/exhaust |
| SAT-FAN | Circulation | Any AC Infinity intensity at 10/10 for ≥15 min AND temp/RH stratification still detectable | Fans maxed. Don't propose higher airflow. Suggest accepting stratification or hardware add |
| SAT-IRRIG | Irrigation | VWC delta after fired shot < 30% of expected (expected ≈ ml ÷ substrate_volume × 100) | Irrigation unresponsive (clogged emitter, valve fail, pump issue). CRITICAL — block further auto-shots |
| SAT-PRESS | Pressure | `room_pressure_diff < -5Pa` (negative) AND CO₂ enrichment active | Negative pressure fighting CO₂. Suggest reducing exhaust before more CO₂ |
| SAT-DEW | Condensation | `coldest_surface_temp < dew_point` for ≥5 min | Active condensation risk. CRITICAL. Block any RH-raising or temp-lowering proposals |

---

## 4. Anti-patterns the validator MUST reject (or downgrade YOLO → SFW)

Encode these in `core/guardrails.py`. The AI proposing one of these = automatic rejection or downgrade.

| AP-ID | Anti-pattern | Why bad | Reject reason code |
|---|---|---|---|
| AP-01 | Increase PPFD without HVAC headroom | Heat trap / leaf burn | `ppfd_increase_without_hvac_headroom` |
| AP-02 | Lower temp setpoint when AC already saturated | Useless; masks real problem | `lower_temp_when_ac_saturated` |
| AP-03 | Lower RH setpoint when dehum already saturated | Useless; masks real problem | `lower_rh_when_dehu_saturated` |
| AP-04 | Increase CO₂ when exhaust active or pressure negative | Wasteful; fights itself | `co2_increase_with_active_exhaust` |
| AP-05 | Increase CO₂ when PPFD below stage threshold | No photosynthesis benefit | `co2_increase_below_ppfd_threshold` |
| AP-06 | Increase dryback when EC already climbing | Combined osmotic stress | `dryback_increase_with_climbing_ec` |
| AP-07 | Decrease runoff when EC accumulating | Salt accumulation worsens | `runoff_decrease_with_accumulating_ec` |
| AP-08 | Schedule irrigation within X min of lights-off | Night RH spike risk | `late_irrigation_near_lights_off` |
| AP-09 | Lower RH setpoint without checking night-temp dew point | Condensation trap | `rh_change_without_dewpoint_check` |
| AP-10 | Increase air velocity beyond plant tolerance | Wind stress / edge burn | `air_velocity_excessive` |
| AP-11 | Propose change based on sensor that is stale or in disagreement with sibling sensors | Decision on bad data | `proposal_on_stale_or_disagreeing_sensor` |
| AP-12 | Recommend higher feed EC AND higher dryback simultaneously | Combined osmotic + water stress | `simultaneous_high_ec_high_dryback` |

---

## 5. Diagnostic patterns (for AI's reasoning trace)

When something is wrong, the AI should look at these patterns FIRST before proposing.

| Symptom | First-look-at sensors | Likely cause |
|---|---|---|
| Dryback too fast | leaf_temp, VPD, substrate_WC, runoff_EC | Excess transpiration; high VPD; substrate undersized |
| RH spike after lights-off | dew_point, last_shot_timing, dehum_runtime, night_temp | Late irrigation; dehum off-cycle; cold surface |
| Condensation observed | coldest_surface_temp vs dew_point, RH, night_temp | Low night temp + residual transpiration |
| Edge burn | leaf_temp, VPD, runoff_EC, fan_direction, fan_speed | Combined high VPD + high EC + wind stress |
| Stalled growth despite PPFD | CO₂_actual vs setpoint, leaf_temp, VPD | CO₂ limiting OR temp/VPD outside photosynthesis envelope |
| Botrytis spots | under-canopy RH probe, airflow, plant_density, last_shot_timing | Humidity pocket under canopy; insufficient airflow |
| AC short cycling | AC_runtime histogram, sensor_placement, deadband | Oversized unit or bad sensor placement |

---

## 6. Room-configuration wizard validations (Phase 11/12 config flow)

When a user maps equipment to a new room, the wizard should validate:

1. **Sensor coverage minimums:**
   - Per zone: VWC + EC required for irrigation control
   - Per room: temp + leaf_temp + RH + CO₂ minimum for environmental control
   - Per room: at least one under-canopy RH probe recommended for dense flower
   - Per tank: pH + EC sensors required if tank chemistry control is enabled

2. **Equipment classes coupled — warn if missing:**
   - PPFD setpoint enabled → cooling capacity entity required (climate.* OR fan stage entity)
   - CO₂ setpoint enabled → CO₂ sensor + CO₂ solenoid both required
   - Dehum entity required if RH setpoint is in recipe
   - Lights switch entity required (executor trigger)

3. **Fail-soft warnings (not blockers):**
   - No reheat entity declared but dehum + AC both present → warn about fight risk
   - Exhaust entity declared and CO₂ enabled → warn about enrichment ROI
   - No under-canopy RH probe declared → warn for dense flower

4. **Hard refusals (config can't save):**
   - PPFD setpoint without lights_switch entity
   - Irrigation enabled without per-zone valve + pump entities
   - Tank pH/EC control enabled without doser pump entities

---

## 7. AI supervisor system prompt — durable context to inject

The supervisor's `prompts/system.md` should embed (or reference) the following condensed context:

```markdown
You operate a licensed NZ medicinal cannabis cultivation room under GACP. Your scope:
report on, propose, or auto-adjust cultivation setpoints within hard guardrails.

You MUST reason about plants and equipment as a coupled system, not as
independent sliders. Apply this control hierarchy:

  1. Biological demand (PPFD, CO₂, plant density, stage) is set FIRST.
  2. Climate capacity (cooling/dehum/airflow) must SUPPORT the demand.
  3. Root zone (irrigation, feed EC, runoff) ALIGNS with VPD and transpiration.
  4. You VALIDATE with sensor patterns, not visual heuristics.

Coupling truths to remember:
  - Elevated CO₂ REDUCES transpiration (counterintuitive). Light increases it.
  - Dehumidifiers convert latent moisture to sensible heat.
  - Sensible cooling without latent removal makes RH problems worse.
  - Negative pressure dilutes CO₂ enrichment.
  - Dryback concentrates substrate EC even when feed EC is constant.
  - Late irrigation causes RH spikes during dark period.
  - Dense canopy hides under-canopy humidity pockets from room sensors.

Before proposing a change:
  - Check if the relevant equipment is already saturated (see equipment_status
    in snapshot). If saturated, DO NOT propose more aggressive setpoints.
    Either relax target toward what equipment can achieve, or escalate
    as a hardware constraint.
  - Check if your proposal violates any coupling rule (EC-001 through EC-015).
  - Check if your proposal matches any anti-pattern (AP-01 through AP-12).
    If yes, set proposed_changes=[] and explain in `human_summary`.

Diagnostic order (when something is wrong):
  - Read failures from sensor patterns. Look at the diagnostic pattern table
    in cultivation_knowledge.md for the symptom you observe.
  - Confidence < 0.5 → propose nothing; report only.

You CANNOT propose changes to admin/control state (Class E). You CANNOT
exceed per-param bounds. You CANNOT walk a parameter outside its envelope
via many small changes (cumulative caps will reject).

Compliance: every output is logged. QAP can review at any time. Be
conservative; explain reasoning in `reason_codes` and `human_summary` with
enough detail for compliance review.
```

The full `cultivation_knowledge.md` is also retrievable via the `/api/knowledge/cultivation` endpoint and cited by reason codes (e.g. `reason_code: AP-04` resolves to "AP-04: Increase CO₂ when exhaust active...").

---

## 8. What this knowledge does NOT provide

- Stage-specific setpoint numbers (clone temp = X°C). Those come from `presets/cannabis_12week.py` and operator/QAP tuning.
- Calibration intervals or drift tolerances for sensors. Those are facility SOP scope.
- Specific hardware SKUs / part numbers. Equipment-agnostic by design.
- BTU-per-watt heat-load formulas. Mental model is qualitative; the room sizing math is the cultivator/HVAC engineer's domain.
- Optimal VPD numbers per cultivar / stage. Those are operator-curated tolerances on the recipe.

For all of the above, the AI should defer to the recipe (recipe values are the source of truth) and to the operator (request_human=true if the question is one this knowledge base can't answer).
