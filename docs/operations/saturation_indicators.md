# Saturation indicators (SAT-*)

A piece of equipment is **saturated** when it is running flat-out and *still*
not holding its target. The deterministic monitoring (`core/equipment.py`)
evaluates seven saturation predicates, one per equipment class, against
InfluxDB sensor history. When a predicate fires:

- the equipment is marked saturated in the room's `equipment_status`;
- the room transitions toward `IMPAIRED` (or `CRITICAL` for the two critical
  indicators);
- the AI is no longer allowed to propose a more aggressive setpoint for that
  equipment's domain — the correct move is to relax the target or escalate a
  hardware constraint.

The point of saturation detection: if your AC is already maxed and the room is
still warming, lowering the temperature setpoint is meaningless. The system
recognises that and stops the AI from doing it.

## Severity

Five indicators are **warning** severity — the equipment is maxed but the room
is not being damaged, so the response is to relax the target. Two are
**critical** because they imply direct crop or process harm:

- **`SAT-IRRIG`** — irrigation is not delivering. Further auto-shots must be
  blocked.
- **`SAT-DEW`** — water is condensing on a surface. Any RH-raising or
  temp-lowering proposal must be blocked.

Every result embeds its `SAT-*` id in `reason_code` so a supervisor or a
rejection can cite it directly.

## The seven predicates

### SAT-AC — air conditioner saturated

> **Fires when:** the climate fan has read "high" for **≥15 minutes**, *and*
> measured air temperature is **above setpoint**, *and* the temperature slope is
> **positive** (still climbing). All three together mean the AC is doing
> everything it can and losing ground.

Severity: warning. Outcome: do not propose a lower temp setpoint (see
[AP-02](anti_patterns.md#ap-02-lower-the-temp-setpoint-when-the-ac-is-saturated)).

### SAT-DEHU — dehumidifier saturated

> **Fires when:** the dehumidifier has been on for **≥20 minutes**, *and*
> measured RH is **above setpoint + 3 %RH**, *and* the RH slope is flat or
> positive (not recovering).

Severity: warning. Outcome: do not propose a lower RH setpoint; investigate the
transpiration source.

### SAT-CO2 — CO₂ injection at its limit

> **Fires when:** CO₂ has held **below 0.9× setpoint** for **≥5 minutes**, *and*
> the CO₂ solenoid's duty cycle over that window **exceeds 50 %**.

Severity: warning. Outcome: the supply is maxed or the room is too leaky — do
not propose higher CO₂; check room tightness and the exhaust.

### SAT-FAN — circulation fans maxed

> **Fires when:** any circulation-fan intensity has sat at **10/10** for
> **≥15 minutes**, *and* temp/RH **stratification is still measurable** (a
> sensor pair still differs by more than 0.5 units).

Severity: warning. Outcome: do not propose higher airflow.

### SAT-IRRIG — irrigation unresponsive (critical)

> **Fires when:** for a shot fired this cycle, the measured VWC rise in the
> 10-minute window after the shot is **below 30 % of the expected rise**
> (expected ≈ shot volume ÷ substrate volume × 100). No VWC data after the shot
> also fires it.

Severity: **critical**. Outcome: a clogged emitter, failed valve or pump
problem — **further auto-shots are blocked** for that zone.

### SAT-PRESS — negative pressure fighting CO₂

> **Fires when:** the room differential pressure has held at/below **−5 Pa**,
> *and* CO₂ enrichment is active. Negative pressure pulls in outside air that
> dilutes the enrichment.

Severity: warning. Outcome: reduce the exhaust before adding more CO₂.

### SAT-DEW — active condensation risk (critical)

> **Fires when:** the coldest tracked surface temperature has sat **at or below
> the room dew point** for **≥5 minutes**. Water is condensing on that surface.

Severity: **critical**. Outcome: **block any RH-raising or temp-lowering
proposal** (see
[AP-09](anti_patterns.md#ap-09-lower-rh-without-checking-the-night-temp-dew-point)
and [EC-008](coupling_rules.md)).

## How a predicate is evaluated

Each predicate depends only on four InfluxDB query helpers:

- `current_value(entity)` — the latest reading.
- `trend_slope(entity, window)` — the slope over a window.
- `at_threshold_for(entity, op, threshold, duration)` — "has been ⋛ X for N
  minutes".
- `delta_post_event(entity, event_ts, window)` — the change after a timestamped
  event (used by `SAT-IRRIG`).

If a room does not have the entities a predicate needs, that predicate is
**skipped** — it returns an "unevaluated" result rather than firing. The caller
still sees the full seven-indicator picture.

## Public defaults vs facility overrides

The thresholds — `AC_FAN_HIGH_MIN = 15`, `DEHU_ON_MIN = 20`,
`CO2_DEFICIT_FRACTION = 0.9`, `IRRIG_RESPONSE_FRACTION = 0.3`,
`PRESS_NEGATIVE_PA = −5.0`, `DEW_SUSTAINED_MIN = 5`, and the rest — are
centralised constants and are conservative public defaults. A facility may
tighten them. The validation pack's `validation/11_ai_guardrail_test.md`
includes a test that each predicate fires correctly against a synthetic InfluxDB
fixture.
