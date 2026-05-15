# Anti-patterns (AP-01 … AP-12)

An **anti-pattern** is a proposed change the cultivation knowledge base flags as
counter-productive. The guardrail validator (`core/anti_patterns.py`) runs all
twelve checks against every change the AI proposes. A hit means the proposal is
**rejected** in YOLO mode (and would fail the re-check on SFW approval).

Each check is a pure function: it reads the proposed change and the sensor
snapshot the AI reasoned about, and either returns a hit or nothing.

## How a hit is reported

When an anti-pattern fires, the rejection's `reason_codes` carries the `AP-*`
id. When the anti-pattern is **saturation-coupled** — it only matters because a
piece of equipment is already saturated — the reason codes carry **both** ids,
the `SAT-*` first: e.g. lowering the temperature setpoint while the AC is
saturated rejects with `["SAT-AC", "AP-02"]`.

A run of the **same** anti-pattern is treated as a signal in itself: three or
more `guardrail_rejection` events citing the same `AP-*` id for one room within
an hour escalates to a `formal_deviation` — it suggests the model keeps trying a
known-bad action (drift or a context misunderstanding). See
[Deviations](deviations.md).

## The twelve anti-patterns

### AP-01 — Increase PPFD without HVAC headroom

Raising PPFD raises transpiration, leaf temperature and HVAC load. If the AC is
already saturated (`SAT-AC`), the room has no cooling headroom and a PPFD
increase is a heat trap / leaf-burn risk.

> **Fires when:** the change raises a PPFD parameter **and** `equipment_status.ac`
> is a `SAT-*` code. Reason codes: `[SAT-AC, AP-01]`.

### AP-02 — Lower the temp setpoint when the AC is saturated

The canonical saturation + anti-pattern combination. If `SAT-AC` has fired the
AC is flat-out and losing ground; cutting the setpoint cannot be met and only
masks a hardware problem.

> **Fires when:** the change lowers a temp parameter **and** the AC is saturated.
> Reason codes: `[SAT-AC, AP-02]`.

### AP-03 — Lower the RH setpoint when the dehumidifier is saturated

If `SAT-DEHU` has fired the dehumidifier is maxed and RH is still high. Cutting
the setpoint cannot be met — investigate the transpiration source instead.

> **Fires when:** the change lowers an RH parameter **and** the dehumidifier is
> saturated. Reason codes: `[SAT-DEHU, AP-03]`.

### AP-04 — Increase CO₂ with an active exhaust or negative pressure

CO₂ enrichment while the exhaust is active, or in a negative-pressure room
(`SAT-PRESS`), is wasteful — the enrichment is diluted by outside air as fast as
it is injected.

> **Fires when:** the change raises a CO₂ parameter **and** the snapshot reports
> `exhaust_active` or pressure saturation. Reason codes: `[AP-04]` or
> `[SAT-PRESS, AP-04]`.

### AP-05 — Increase CO₂ when PPFD is below the stage threshold

CO₂ enrichment only pays when light is *not* the limiting factor. Below the
stage's PPFD threshold the extra CO₂ buys no extra photosynthesis.

> **Fires when:** the change raises CO₂ **and** current PPFD is below the stage's
> `ppfd_co2_threshold` (public floor 400 if the snapshot omits one).

### AP-06 — Increase dryback when substrate EC is climbing

Faster dryback concentrates substrate EC further. Doing it while EC is already
rising stacks osmotic stress on the plant.

> **Fires when:** the change raises `dryback_pct` **and** any substrate / runoff
> EC sensor is trending up (or the snapshot flags `ec_trend: rising`).

### AP-07 — Decrease runoff when EC is accumulating

Runoff is what flushes accumulated salt. Cutting it while EC rises — or while
runoff is already below the rough 10 % mandatory minimum — makes accumulation
worse.

> **Fires when:** the change lowers `drain_pct` / `runoff_pct` **and** EC is
> rising or runoff is already below 10 %.

### AP-08 — Schedule irrigation within ~30 minutes of lights-off

An irrigation event close to lights-off spikes RH during the dark period — the
dehumidifier may be off-cycle and surfaces are cooling.

> **Fires when:** the change raises an irrigation-timing parameter **and** the
> snapshot's `lights_off_in_minutes` is 30 or less.

### AP-09 — Lower RH without checking the night-temp dew point

Changing an RH setpoint without confirming the coldest surface stays above the
dew point can drive condensation. If `SAT-DEW` has fired, any RH change made
blind is unsafe.

> **Fires when:** the change moves an RH parameter **and** condensation is
> flagged (`SAT-DEW`). Reason codes: `[SAT-DEW, AP-09]`.

### AP-10 — Increase air velocity beyond plant tolerance

Air velocity above the plant-tolerance ceiling causes wind stress / edge burn.

> **Fires when:** the change raises `air_velocity` **and** the resulting value
> would exceed the tolerance ceiling (public default 1.0 m/s).

### AP-11 — Propose a change on a stale or disagreeing sensor

A decision is only as good as its inputs. If the parameter's backing sensor is
flagged stale, or sibling sensors disagree, the proposal rests on bad data.

> **Fires when:** the change is non-zero **and** a stale sensor matches the
> changed parameter, or the snapshot flags `disagreeing_sensors`.

### AP-12 — Higher feed EC and higher dryback at the same time

High feed EC and aggressive dryback both raise root-zone osmotic stress.
Together they combine osmotic and water stress.

> **Fires when:** the change raises a feed-EC parameter **and** the snapshot
> flags `dryback_high` (or `dryback_pct` ≥ 50).

## Worked example

The supervisor builds a snapshot in which the room is 1.5 °C above its
temperature setpoint and `equipment_status.ac` reads `SAT-AC` — the AC fan has
been high for 20 minutes and the temperature is still rising.

The LLM, asked for an assessment, proposes "lower `temp_day` by 0.5 °C".

The validator runs the anti-pattern checks. `check_ap02` matches: a temperature
*decrease* while the AC is saturated. The proposal is **rejected**. The audit
row is a `guardrail_rejection` with `reason_codes = ["SAT-AC", "AP-02"]` and a
human-readable detail explaining the AC cannot reach the current target, so the
constraint is hardware capacity, not a setpoint. A 60-minute cool-down starts on
`temp_day` for that room.

The correct AI behaviour — and what the system prompt instructs — is to propose
nothing, set `requires_human=true`, and explain in `human_summary` that the AC
is saturated.

## Public defaults vs facility overrides

The thresholds above (`LATE_IRRIGATION_MIN = 30`, `AIR_VELOCITY_TOLERANCE =
1.0`, the PPFD/CO₂ floor, etc.) are conservative **public defaults**. A facility
tightens them in its private `guardrails_overrides.py`. The validation pack's
`validation/11_ai_guardrail_test.md` has a test case per anti-pattern.
