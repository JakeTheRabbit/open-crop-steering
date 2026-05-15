# Coupling rules (EC-001 … EC-015)

A **coupling rule** encodes the truth that grow-room equipment is not a set of
independent sliders — changing one setpoint loads, or fights, another piece of
equipment. The validator (`core/coupling_rules.py`) runs all fifteen checks
against every AI-proposed change. They are also the rules the
room-configuration wizard enforces when you map equipment to a room.

## Hard refusal vs fail-soft

The knowledge base draws a line the system honours:

- **Hard refusal** — the proposal (or configuration) cannot proceed.
- **Fail-soft warning** — allowed, but flagged. It is a cost/ROI concern, not a
  safety one.

The Phase 9 guardrail validator **rejects the AI's proposal on either kind** —
but the `hard` flag is preserved in the rejection detail, and it is what the
room-configuration wizard keys off: hard couplings block a save, fail-soft
couplings save with a banner.

## How a violation is reported

A coupling violation carries the `EC-*` id in `reason_codes` and a
human-readable reason. The validator rejects the proposal and starts a cool-down
on that parameter.

## The fifteen coupling rules

| ID | Coupling | Kind | Rule |
|---|---|---|---|
| **EC-001** | PPFD ↔ HVAC | Hard | A PPFD increase ≥10 % of the current setpoint needs a declared HVAC-headroom source, or an HVAC re-sizing review. |
| **EC-002** | PPFD ↔ Dehum | Hard | A PPFD increase ≥10 % needs a declared dehumidification-headroom source — higher PPFD raises transpiration and dehum load. |
| **EC-003** | PPFD ↔ Leaf temp | Hard | A PPFD increase while leaf temp is already at/above air temp is a bleach risk — the air-temp setpoint must leave leaf-temp room. |
| **EC-004** | Dehum ↔ Reheat | Fail-soft | Lowering RH in a dehumidifier + AC room with no reheat coil — the dehumidifier's sensible-heat conversion makes the AC over-cool; the two fight on VPD. |
| **EC-005** | CO₂ ↔ Exhaust | Hard | Raising CO₂ while the exhaust is active — enrichment is vented as fast as it is injected. Suppress the exhaust first. |
| **EC-006** | CO₂ ↔ Light | Fail-soft | Raising CO₂ while PPFD is below the stage threshold — light is limiting; enrichment ROI is poor. |
| **EC-007** | CO₂ ↔ Negative pressure | Fail-soft | Raising CO₂ in a negative-pressure room (`SAT-PRESS`) — conditioned, enriched air leaks out. |
| **EC-008** | RH ↔ Night temp | Hard | Lowering the night-temp setpoint while active condensation is flagged (`SAT-DEW`) — a lower night temp drops the coldest surface further below the dew point. |
| **EC-009** | Airflow ↔ Disease | Fail-soft | Changing RH in a room with no under-canopy airflow — humidity pockets under a dense canopy are invisible to the room sensor; botrytis risk. |
| **EC-010** | Dryback ↔ EC | Fail-soft | Raising dryback with no runoff-EC data — faster dryback concentrates substrate EC; runoff EC is needed to confirm it is safe. |
| **EC-011** | Runoff ↔ Salt accumulation | Hard | A drain/runoff cut whose resulting value falls below the rough 10 % mandatory minimum — salt accumulates regardless of feed EC. |
| **EC-012** | VPD ↔ Osmotic stress | Hard | Raising feed EC while VPD is already high — combined osmotic + water stress. You cannot run aggressive dryback and high feed EC together. |
| **EC-013** | Late irrigation ↔ Night RH | Hard | A change that pushes an irrigation shot within ~30 min of lights-off — RH spikes during the dark period. |
| **EC-014** | Sensor placement ↔ AC stability | Fail-soft | A temp/RH setpoint change in a room whose climate-sensor placement is not validated — deadband tuning on a badly-placed sensor oscillates or hides problems. |
| **EC-015** | Exhaust ↔ Pathogen ingress | Fail-soft | A change that increases fresh-air exchange in a room with an exhaust but no intake filter — pathogen / spore ingress; filtration is a biosecurity requirement. |

## What the checks read

Each coupling check reads three things:

1. **The proposed change** — the parameter, direction and delta.
2. **The sensor snapshot** — equipment-saturation flags, current sensor values,
   current setpoints, `exhaust_active`, `lights_off_in_minutes`, etc.
3. **The room config** — the static equipment / coupling map the wizard
   recorded: whether a reheat coil exists, whether the intake is filtered,
   whether there is under-canopy airflow, the declared headroom sources, the
   PPFD increase ceiling, whether sensor placement is validated.

A room that is only partially configured fails **soft**: a missing config field
takes its safe default rather than crashing a check.

## Worked example

The AI proposes raising `ppfd` by 120 µmol·m⁻²·s⁻¹. The current PPFD setpoint
is 800, so the increase is 15 % — above the 10 % review threshold.

`check_ec001` looks at the room config. No `hvac_headroom_source` is declared.
The proposal is **rejected** as a hard coupling violation: the PPFD increase
demands an HVAC re-sizing review that has not been done. `reason_codes`
carries `EC-001`. A cool-down starts on `ppfd`.

The same proposal also trips `check_ec002` (no `dehu_headroom_source`). The
validator rejects on the first violation; the proposal does not get re-evaluated
once rejected.

## Public defaults vs facility overrides

`PPFD_HEADROOM_REVIEW_FRACTION = 0.10`, `RUNOFF_MIN_PCT = 10.0`,
`LATE_SHOT_MIN = 30` are conservative public defaults. A facility tightens them
in its private `guardrails_overrides.py`. The validation pack's
`validation/11_ai_guardrail_test.md` has a test case per coupling rule.
