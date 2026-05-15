# Cultivation knowledge

This is the cultivation domain knowledge that Open Crop Steering reasons with.
It is not stage-by-stage setpoint numbers — those come from your recipe. It is
the **coupled-systems mental model**: how the pieces of a grow room load and
fight each other.

The same knowledge has three jobs in the system:

1. It is embedded in the AI supervisor's system prompt, so the AI reasons about
   plants and equipment as a coupled system.
2. It drives the **room-configuration wizard** — the equipment-coupling checks
   that warn or refuse when a room is mis-mapped.
3. It drives the **guardrail validator** — the anti-patterns, coupling rules and
   saturation predicates the AI's proposals are checked against.

The full text is also served live at `GET /api/knowledge/cultivation` so the AI
can pull a complete rule definition when it explains a reason code.

!!! note "What this does NOT provide"
    Stage-specific setpoint values, sensor calibration intervals, hardware part
    numbers, BTU-per-watt heat-load formulas, optimal VPD numbers per cultivar.
    Those come from the recipe (the source of truth for values), from facility
    SOPs, and from the cultivator. When a question needs a number this
    knowledge base does not hold, the AI sets `requires_human=true`.

## The core mental model

### The forward chain

```
Light → leaf temperature → VPD → transpiration → dryback → substrate EC →
  nutrient/water stress → photosynthesis → CO₂ uptake → HVAC/dehum load →
  disease risk
```

### The reverse chain

```
Humidity / dehumidification → VPD → transpiration → dryback → EC → stomata →
  CO₂ uptake → growth
```

### The control hierarchy

Decisions are made in this order — never the other way round:

1. **Set biological demand first** — crop stage, PPFD, CO₂, plant density.
2. **Match climate capacity** — cooling, dehumidification and airflow sizing
   must support that demand.
3. **Match the root-zone strategy** — irrigation frequency, feed EC and runoff
   align with the resulting VPD and transpiration.
4. **Validate with measured responses** — read failures from sensor patterns,
   not from looking at the plants.

### Counter-intuitive coupling (the most-often-missed truths)

- **Elevated CO₂ *reduces* transpiration** (and stomatal conductance) while
  raising photosynthesis and water-use efficiency. The common assumption is the
  opposite.
- **Light is the opposite**: more PPFD *increases* transpiration, CO₂ demand and
  HVAC load.
- **Dehumidifiers convert latent moisture to sensible heat** — removing moisture
  *warms* the room. Without a reheat stage the AC over-cools to dehumidify.
- **Negative room pressure pulls in outside air** that dilutes CO₂ enrichment
  and imports unconditioned temperature, humidity and particles.

## Equipment-coupling rules (EC-001 … EC-015)

Cultivation equipment is not a set of independent sliders. Changing one setpoint
loads — or fights — another piece of equipment. These fifteen rules encode that.
They are checked by the validator and by the room-configuration wizard.

Full detail, hard-refusal vs fail-soft, and worked examples are in
[Operations → Coupling rules](../operations/coupling_rules.md). In brief:

| ID | Coupling |
|---|---|
| EC-001 | PPFD up needs cooling headroom (or an HVAC re-sizing review). |
| EC-002 | PPFD up raises dehumidification load — same headroom check. |
| EC-003 | PPFD raises leaf temp; the air-temp setpoint must leave leaf-temp room. |
| EC-004 | A standalone dehumidifier (no reheat) makes the AC over-cool — they fight on VPD. |
| EC-005 | CO₂ enrichment with an active exhaust fights itself. |
| EC-006 | CO₂ enrichment only pays when PPFD is not the limiting factor. |
| EC-007 | Negative-pressure rooms leak conditioned, enriched air. |
| EC-008 | Low night temp + residual transpiration is a condensation trap. |
| EC-009 | A dense canopy without under-canopy airflow hides humidity pockets. |
| EC-010 | Faster dryback concentrates substrate EC even at constant feed EC. |
| EC-011 | Insufficient runoff accumulates salt regardless of feed EC. |
| EC-012 | High VPD + high substrate EC is combined edge-burn / water stress. |
| EC-013 | A late irrigation shot spikes RH during the dark period. |
| EC-014 | Sensor placement drives deadband tuning — bad placement oscillates. |
| EC-015 | An unfiltered intake imports pathogens — filtration is a biosecurity rule. |

## Equipment-saturation indicators (SAT-*)

When a piece of equipment is running flat-out and *still* not holding its
target, it is **saturated**. The system detects seven saturation patterns from
sensor evidence. When one fires, the AI stops being allowed to propose a more
aggressive setpoint for that equipment — the right move is to relax the target
or escalate a hardware constraint, not to ask the equipment for more.

Full detail in
[Operations → Saturation indicators](../operations/saturation_indicators.md). In
brief:

| ID | Equipment | Fires when |
|---|---|---|
| SAT-AC | Air conditioner | Fan high ≥15 min, temp above setpoint, still climbing. |
| SAT-DEHU | Dehumidifier | On ≥20 min, RH above setpoint + 3 %, not recovering. |
| SAT-CO2 | CO₂ injection | CO₂ below 0.9× setpoint ≥5 min, solenoid duty > 50 %. |
| SAT-FAN | Circulation fans | A fan at 10/10 ≥15 min, stratification still measurable. |
| SAT-IRRIG | Irrigation | VWC rise after a shot below 30 % of expected. **Critical.** |
| SAT-PRESS | Room pressure | Differential pressure below −5 Pa while CO₂ enrichment is active. |
| SAT-DEW | Condensation | Coldest surface at/below the dew point ≥5 min. **Critical.** |

## Anti-patterns (AP-01 … AP-12)

An **anti-pattern** is a proposed change the knowledge base flags as
counter-productive — useless (it masks a real problem), wasteful (it fights
itself), or actively harmful (combined crop stress). The AI proposing one is an
automatic rejection (in YOLO) or a downgrade to SFW.

Full detail and worked examples in
[Operations → Anti-patterns](../operations/anti_patterns.md). In brief:

| ID | Anti-pattern |
|---|---|
| AP-01 | Increase PPFD without HVAC headroom. |
| AP-02 | Lower the temp setpoint when the AC is already saturated. |
| AP-03 | Lower the RH setpoint when the dehumidifier is already saturated. |
| AP-04 | Increase CO₂ when the exhaust is active or pressure is negative. |
| AP-05 | Increase CO₂ when PPFD is below the stage threshold. |
| AP-06 | Increase dryback when substrate EC is already climbing. |
| AP-07 | Decrease runoff when EC is accumulating. |
| AP-08 | Schedule irrigation within ~30 min of lights-off. |
| AP-09 | Lower the RH setpoint without checking the night-temp dew point. |
| AP-10 | Increase air velocity beyond plant tolerance. |
| AP-11 | Propose a change based on a stale or disagreeing sensor. |
| AP-12 | Recommend higher feed EC and higher dryback at the same time. |

## Diagnostic patterns

When something is wrong, the right move is to read the failure from sensor
patterns before proposing anything. For a given symptom, look at these sensors
first:

| Symptom | Look first at | Likely cause |
|---|---|---|
| Dryback too fast | leaf_temp, VPD, substrate WC, runoff EC | Excess transpiration; high VPD; substrate undersized. |
| RH spike after lights-off | dew_point, last-shot timing, dehum runtime, night_temp | Late irrigation; dehum off-cycle; cold surface. |
| Condensation observed | coldest surface temp vs dew_point, RH, night_temp | Low night temp + residual transpiration. |
| Edge burn | leaf_temp, VPD, runoff EC, fan direction/speed | Combined high VPD + high EC + wind stress. |
| Stalled growth despite PPFD | CO₂ actual vs setpoint, leaf_temp, VPD | CO₂ limiting, or temp/VPD outside the photosynthesis envelope. |
| Botrytis spots | under-canopy RH probe, airflow, plant density, last-shot timing | Humidity pocket under the canopy; insufficient airflow. |
| AC short cycling | AC runtime histogram, sensor placement, deadband | Oversized unit or bad sensor placement. |

## The room-configuration wizard

When you map equipment to a room, the wizard checks coverage and coupling.

**Sensor coverage minimums:**

- Per zone: VWC + EC required for irrigation control.
- Per room: temp + leaf_temp + RH + CO₂ minimum for environmental control.
- Per room: at least one under-canopy RH probe recommended for dense flower.
- Per tank: pH + EC required if tank chemistry control is enabled.

**Hard refusals** — the configuration cannot be saved:

- PPFD setpoint enabled without a lights-switch entity.
- Irrigation enabled without per-zone valve + pump entities.
- Tank pH/EC control enabled without doser-pump entities.

**Fail-soft warnings** — saved, but with a banner:

- Dehumidifier + AC both present but no reheat entity declared.
- An exhaust entity declared and CO₂ enabled.
- No under-canopy RH probe declared.

The coupling notes the wizard records are passed to the AI supervisor so it
knows what hardware constraints apply to that room.
