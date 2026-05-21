# HA-Irrigation-Strategy Integration — Design Spec

Spec produced by the Plan agent on 2026-05-22 for integrating
**HA-Irrigation-Strategy** (HA-IS — the user's production crop-steering
controller running on Home Assistant + AppDaemon) with **Open Crop
Steering** (OCS — the new Python/FastAPI/Postgres AI supervisor that
ships as an HA add-on or standalone Docker).

This is the contract for the implementing agent(s). Defaults are picked;
alternatives are listed only in §10 where a judgment call is genuinely
open.

---

## 1. Bottom-line summary

**OCS becomes HA-IS's supervisor; HA-IS stays its doer.** The 4-phase
state machine, dryback detection, hardware sequencing, and safety gates
remain in `appdaemon/apps/crop_steering/master_crop_steering_app.py`
because that code is battle-tested on real plants and the user is
feeding crops with it today. OCS reads the room state every tick, runs
the Legacy Ag playbook through the supervisor LLM, and *only* adjusts
the **setpoints** HA-IS already exposes — the cultivator-intent slider,
EC targets, P1/P2 VWC thresholds, P0 dryback drop percentages.
Discrete shot firing stays inside HA-IS via the existing
`crop_steering.execute_irrigation_shot` and `crop_steering.custom_shot`
services; OCS proposes shots only on the SFW / bounded-auto-adjust
paths and the executor enqueues them as commands targeting those
services.

The metaphor: **OCS is the cultivation manager, HA-IS is the head
grower on the floor.** OCS reads sensors, consults the playbook, sets
targets, and writes the audit chain. HA-IS executes within those
targets, owns the millisecond-scale hardware sequencing, and refuses
unsafe shots regardless of what OCS asks for. The HA-IS L0 LLM report
builder is deprecated in favour of the OCS supervisor (one LLM, with
the playbook already baked into the system prompt and a full
audit/guardrail/approval surface).

---

## 2. What HA-IS actually is — architecture summary

### Components

| Layer | Path | Role |
|---|---|---|
| HA custom component | `custom_components/crop_steering/` | Creates ~100 entities (numbers, switches, selects, sensors), exposes 5 services, fires events. **No hardware contact.** |
| HA packages | `packages/irrigation/`, `packages/rootsense/00_recorder.yaml` | YAML wiring — template sensors, automations, recorder includes. |
| AppDaemon legacy app | `appdaemon/apps/crop_steering/master_crop_steering_app.py` (6,263 lines) | The brain. Reads entity states, runs the phase loop, sequences hardware. **The only path to a valve.** |
| AppDaemon support | `phase_state_machine.py`, `advanced_dryback_detection.py`, `intelligent_sensor_fusion.py`, `ml_irrigation_predictor.py`, `intelligent_crop_profiles.py` | Libraries the master app composes. |
| AppDaemon RootSense v3 pillars | `appdaemon/apps/crop_steering/intelligence/*` | Optional adaptive intelligence — root zone, adaptive irrigation, agronomic, orchestrator, anomaly, LLM report builder. Each gated by a `switch.crop_steering_intelligence_*_enabled`, default OFF. |
| Config | `crop_steering.env`, `config_flow.py`, `env_parser.py` | Entity mapping (1-6 zones; VWC/EC front-back per zone; pump, main valve, zone valves). |

### Data flow

```
crop_steering.env  →  config_flow.py  →  entity registry
                                              ↓
hardware sensors  →  HA entities  →  AppDaemon master_crop_steering_app
                                              ↓
                                       phase decisions
                                              ↓
                              crop_steering.execute_irrigation_shot
                                       (fires event)
                                              ↓
                                  AppDaemon hardware sequencer
                                              ↓
                       pump → main valve → zone valve → irrigate
```

### The 4-phase state machine (per-zone, daily)

| Phase | Trigger | Behaviour | Exit |
|---|---|---|---|
| **P0** Morning Dryback | Lights ON | No watering; record peak VWC | VWC dropped 15 %+ from peak; or 120 min elapsed; or `min_wait_time` met + dryback rate stalls; flush shot if `EC ratio > 2.5×` |
| **P1** Ramp-Up | P0 exit | Progressive shots `initial + (increment × count)` capped at `max_shot_size`, every `time_between_shots` minutes; EC-adjusted | VWC ≥ `p1_target_vwc` AND ≥ `min_shots`; or `max_shots` reached |
| **P2** Maintenance | P1 exit | Fixed `p2_shot_size`% shot when VWC < `p2_vwc_threshold`; EC-adjusted up (flush) or down (conserve) | ML/time predictor says not enough time to dryback before lights off |
| **P3** Pre-Lights-Off | P2 exit | No irrigation; emergency rescue at `p3_emergency_vwc_threshold` (40 %); flush if EC ratio > 2.0× | Next morning → P0 |

### Services it exposes (the integration's wire surface)

| Service | Inputs | What it does |
|---|---|---|
| `crop_steering.transition_phase` | `target_phase`, optional `reason`, `forced` | Updates `select.crop_steering_irrigation_phase`, fires `crop_steering_phase_transition` event |
| `crop_steering.execute_irrigation_shot` | `zone`, `duration_seconds`, optional `shot_type` | Fires `crop_steering_irrigation_shot` event — AppDaemon picks up and runs the pump→main→valve sequence |
| `crop_steering.check_transition_conditions` | — | Evaluates state, fires `crop_steering_transition_check` event |
| `crop_steering.set_manual_override` | `zone`, optional `timeout_minutes`, `enable` | Sets `switch.crop_steering_zone_{N}_manual_override` |
| `crop_steering.custom_shot` | `target_zone`, `intent`, `volume_ml`, optional `target_runoff_pct`, `tag` | Fires `crop_steering_custom_shot` event — RootSense `IrrigationOrchestrator` picks up, gates, converts mL→duration, calls `execute_irrigation_shot` |

### Key entities it owns (the "setpoints" OCS will steer)

Steering bias and phase targets:
- `number.crop_steering_steering_intent` (-100 generative … +100 vegetative — the *one* lever)
- `number.crop_steering_veg_p0_dryback_drop_pct`, `..._gen_p0_dryback_drop_pct`
- `number.crop_steering_p1_target_vwc`, `..._p2_vwc_threshold`
- `number.crop_steering_p2_shot_size`, `..._p2_ec_high_threshold`, `..._p2_ec_low_threshold`
- `number.crop_steering_ec_target_veg_p{0..3}`, `..._gen_p{0..3}`, `..._ec_target_flush`

Per-zone overrides:
- `switch.crop_steering_zone_{N}_enabled`, `..._manual_override`
- `number.crop_steering_zone_{N}_max_daily_volume`, `..._shot_size_multiplier`
- `select.crop_steering_zone_{N}_phase_override` (Auto / P0..P3)

System control:
- `switch.crop_steering_system_enabled`, `..._auto_irrigation_enabled`

Read-only state HA-IS publishes:
- `sensor.crop_steering_vwc_zone_{N}`, `..._ec_zone_{N}`, `..._zone_{N}_status`
- `sensor.crop_steering_ec_ratio`, `..._dryback_percentage`, `..._current_phase`
- RootSense v3: `sensor.crop_steering_zone_{N}_field_capacity_observed`, `..._dryback_velocity_pct_per_hr`, `..._substrate_porosity_estimate_ml_per_pct`, `..._ec_stack_index`
- `binary_sensor.crop_steering_anomaly_active`

### The LLM advisor seed (HEAD commit `59f9b4a`)

`appdaemon/apps/crop_steering/intelligence/llm/report_builder.py` is
the **Phase L0** LLM advisor. It is explicitly *not* an LLM call — it
builds a ~300-token JSON snapshot every 15 min and publishes it as
`sensor.crop_steering_rootsense_report_latest` (state = local-rule
triage tag; attributes = full payload) plus an HA event
`crop_steering_rootsense_report` and a bus event `report.ready`.

The schema includes `ts`, `phase`, `intent`, `recipe_phase`,
`recipe_day`, `climate` (temp/rh/vpd/leaf_vpd/co2 with `_target` and
`_status`), `substrate` (per-zone vwc/ec/dryback/fc),
`deltas_15m_ago`, `active_anomalies`, `triage`, `estimated_tokens`.
The module is **guarded by a test** that asserts no LLM client
library is importable, so by construction L0 cannot call an LLM. The
gate switch is `switch.crop_steering_intelligence_llm_report_enabled`
(default OFF).

This is the perfect input pipe to OCS — see §8.

---

## 3. Overlap & conflict map

| Concern | HA-IS owns | OCS owns | Conflict? |
|---|---|---|---|
| **Hardware actuation** | `master_crop_steering_app.py` pump→main→valve sequencer (the *only* code that calls `switch.turn_on` on a valve) | `backend/app/workers/executor.py` calls `HAClient.call_service` then verifies readback | **Yes if both touch valves.** Resolve: OCS executor only ever calls HA-IS services (`crop_steering.execute_irrigation_shot`, `crop_steering.custom_shot`, `crop_steering.transition_phase`) and HA `input_number.set_value` on HA-IS setpoint entities — never `switch.turn_on` on a valve. |
| **Phase state machine** | `appdaemon/apps/crop_steering/phase_state_machine.py` + the legacy app's loop | OCS has `cycle_day` in `room_runtime` + phase-bound recipes in `grow_recipes.phases` — these are *cultivation phases* (Early Veg, Late Veg, Flower Stretch…), **not** daily P0-P3. | **No, they're different concepts.** Document explicitly: OCS phases = cultivation stages (84-day timeline); HA-IS phases = daily irrigation cycle (P0-P3 inside one calendar day). They compose. |
| **Recipe / setpoint truth** | `number.crop_steering_*` entities, `crop_steering.env`, RootSense intent slider | `recipe_revision` + `runtime_adjustment` → `effective_target` materialised view | **Yes.** Resolve: OCS owns the canonical recipe and the audit chain; HA-IS `number.*` entities become *projections* of OCS's `effective_target`, written by the executor's `on_lights_on` and bounded-auto-adjust batches via `input_number.set_value` calls. HA-IS still reads its own entities — it never asks OCS for a value. |
| **Audit trail** | AppDaemon logs to `appdaemon.log`; RootSense SQLite at `appdaemon/apps/crop_steering/state/rootsense.db` (shot history, dryback episodes) | HMAC-chained `audit_event` table; INSERT-only triggers; daily `daily_seal` rows exported off-box | **HA-IS audit is partial / not compliance-grade.** Resolve: OCS is the authoritative audit chain. HA-IS continues to log its internal SQLite as a debug surface, but every *controlled* state change (setpoint write, shot fire, phase transition) flows through OCS's `controlled_adjustment` audit row by virtue of going through the executor's command queue. |
| **LLM advisor** | `appdaemon/apps/crop_steering/intelligence/llm/report_builder.py` (L0 — no LLM call, just the snapshot) | `backend/app/workers/supervisor.py` calls the LLM with `full_system_prompt()` = `system.md` ++ `grow-room-agent-playbook.md`, parses to `ocs.llm_decision.v1`, routes by per-class rollout stage | **Two LLMs would be wrong.** Resolve: OCS supervisor is authoritative. HA-IS L0 report builder is **deprecated** (see §8) — its switch defaults OFF and the snapshot it produces is replaced by `build_snapshot()` in `backend/app/core/snapshot.py`. |
| **Anomaly detection** | `appdaemon/apps/crop_steering/intelligence/anomaly.py` + `binary_sensor.crop_steering_anomaly_active` | OCS's `app.core.tolerance` + `event_log` + saturation predicates (`SAT-*`) | **Two anomaly sources is acceptable** — HA-IS catches substrate-side anomalies (emitter blockage, EC drift, peer-zone deviation) that OCS has no visibility into. Pipe `binary_sensor.crop_steering_anomaly_active` (and the per-zone `active_codes`) into the OCS snapshot so the supervisor LLM sees both. |
| **Shot proposals** | `IrrigationOrchestrator.custom_shot` event handler (gates anomaly-suppressed zones, mL→duration conversion via flow-rate entity) | OCS supervisor proposes setpoint deltas; the bounded-auto-adjust path goes through `app.core.guardrails.validate_all` and enqueues a `command_queue` batch | **Resolve by giving each its layer.** OCS proposes *setpoints* (slider, EC targets, thresholds). HA-IS continues to decide *when* to fire individual shots based on those setpoints. The only time OCS proposes a discrete *shot* is on the SFW path (a human approves) or for explicit rescue/flush via `custom_shot` — see §6 P6. |
| **Manual override** | `switch.crop_steering_zone_{N}_manual_override`, `crop_steering.set_manual_override` service | OCS `room_runtime.paused` + `.muted` flags + no-touch windows | **Compose:** OCS paused → OCS supervisor skips ticks. HA-IS manual override → AppDaemon blocks shots regardless of what arrives. Both gates respected independently. |

### Specific file citations of the overlap

- **OCS executor's HA contact:** `backend/app/workers/executor.py:430-448` builds commands as `domain="input_number", service="set_value", target_entity=f"input_number.{room_id}_setpoint_{param_name}"`. This **assumes setpoint entities named `input_number.{room_id}_setpoint_*`**. They don't exist in HA-IS. The integration pass redirects to `number.crop_steering_*` entities (see §6 P2).
- **HA-IS LLM seed at `appdaemon/apps/crop_steering/intelligence/llm/report_builder.py:223-247`** — the schema OCS supervisor will consume. The HA payload becomes the input to `app.core.snapshot.build_snapshot`.
- **HA-IS orchestrator at `appdaemon/apps/crop_steering/intelligence/orchestration.py:50-109`** — already listens to `crop_steering_custom_shot` HA events. OCS's executor enqueues that exact service call when the supervisor proposes a rescue/flush shot. No HA-IS change needed.

---

## 4. Integration options

### Option A. OCS as supervisor + HA-IS as doer (RECOMMENDED)

**How:** OCS subscribes to HA state, calls the LLM every 5 min, and
writes setpoints to HA-IS `number.*` entities via `input_number.set_value`-style
service calls (re-targeted to `number.set_value`). Phase logic, shot
firing, and hardware sequencing stay 100 % in HA-IS. The OCS supervisor's
`proposed_changes` list contains only HA-IS-controlled setpoints
(intent slider, EC targets, VWC thresholds, dryback %), never raw valve
calls.

**Repo changes:**
- **HA-IS:** essentially zero. Maybe one small addition — a manifest sensor
  that exposes "OCS supervisor connected: yes/no/last-tick-age" for the
  dashboard.
- **OCS:** retarget executor commands from `input_number.{room_id}_setpoint_*`
  to `number.crop_steering_*`; add a `CropSteeringSnapshotSource` that
  pulls HA-IS's RootSense report and zone sensors instead of (or as
  well as) building from InfluxDB.

**Day-to-day:** User opens HA, sees the same crop_steering dashboards.
Opens OCS UI, sees per-room status, AI reports, pending approvals,
audit trail. Setpoints in HA reflect what OCS recommended; HA-IS still
fires shots in real time without waiting for OCS.

**Failure modes:** OCS down → setpoints freeze at their last value, HA-IS
keeps running its phase cycle unchanged. HA down → OCS stops getting
readback, supervisor times out gracefully (already handled). Either can
restart without coordinating with the other.

**Risk profile: low.** The production irrigation path is untouched; OCS is
additive.

### Option B. OCS absorbs HA-IS logic

**How:** Port the entire 4-phase state machine, dryback detection,
sensor fusion, EC ratio logic, shot-sizing, emergency rescue into OCS
workers. Retire AppDaemon. OCS executor calls HA `switch.turn_on` /
`switch.turn_off` directly on the valve entities, with its own pump
prime / main valve / zone valve sequencing.

**Repo changes:**
- **HA-IS:** delete `appdaemon/apps/crop_steering/master_crop_steering_app.py`
  (6,263 lines) and all the intelligence modules.
- **OCS:** add ~7,000 lines of phase-state-machine, shot-sizing,
  dryback detector, sensor fusion, ML predictor, hardware sequencer
  with all the existing safety gates re-implemented.

**Day-to-day:** Single UI. But every existing HA-IS automation, package,
dashboard, blueprint, and template sensor that names a `crop_steering.*`
entity breaks unless rebuilt.

**Failure modes:** A bug in the port stops irrigation. The user has six
zones of cannabis depending on the legacy code running correctly.

**Risk profile: very high.** Big-bang cutover of a production crop
controller. Reimplementation cost is enormous and the only thing gained
is "one repo instead of two".

### Option C. HA-IS becomes the OCS executor

**How:** OCS's `command_queue` dispatcher stops calling raw HA
`input_number.set_value` and instead calls HA-IS service IDs
(`crop_steering.execute_irrigation_shot`,
`crop_steering.transition_phase`, `crop_steering.custom_shot`). OCS
becomes the "what setpoint, when phase transition" decider; HA-IS
remains the "shot timing, hardware sequencing" doer. The boundary
between the two is at the *phase transition*, not at the setpoint.

**Repo changes:**
- **HA-IS:** AppDaemon's phase state machine is *suppressed* — phases
  are now externally driven via `crop_steering.transition_phase`. Shot
  sizing keeps running.
- **OCS:** supervisor produces phase transitions and per-shot decisions,
  not setpoints. The LLM's `proposed_changes` becomes
  `{"action":"transition_phase","target":"P2","reason":"..."}` instead
  of `{"param_name":"p1_target_vwc","delta":+2}`.

**Day-to-day:** OCS dictates phase changes. HA-IS only fires shots
within OCS-blessed phases.

**Failure modes:** OCS down → HA-IS doesn't know when to transition →
phases stall. The user's plants get watered or not at the wrong times.

**Risk profile: medium-high.** Couples OCS *too* tightly to the
sub-second irrigation cycle; loses the property that HA-IS keeps
running unchanged when OCS is offline.

### Option D. Mutual integration via a defined contract (HTTP + MQTT)

**How:** Both keep their surfaces. Introduce an HA-IS-side HTTP server
(or use the existing HA REST API) that exposes a `/snapshot` endpoint
and a `/setpoints` PUT. OCS publishes setpoint adjustments + recipe
context back to HA-IS via that contract. HA-IS publishes
phase/shot/sensor events to OCS via MQTT or HA events.

**Repo changes:**
- **HA-IS:** add an HTTP server inside AppDaemon, plus event publishers
  for every phase transition / shot fire.
- **OCS:** add a client for the HA-IS HTTP contract on top of the
  existing HA WebSocket client.

**Day-to-day:** Same as Option A, but with two extra protocols to debug.

**Failure modes:** Same as Option A *plus* the HTTP/MQTT contract can
desync from the entity shapes.

**Risk profile: medium.** Functionally identical to Option A but with
more moving parts. Option A reuses the HA REST + WS API the user
already has; this option adds another contract layer for no
operational gain.

### Comparison

| Dimension | A | B | C | D |
|---|---|---|---|---|
| Code surface owner | HA-IS keeps everything; OCS adds setpoint writes | OCS owns it all | OCS owns phase + shot logic; HA-IS owns hardware sequencing | Both own halves; contract owns the seam |
| Failure (HA down) | OCS supervisor reports timeout; resumes when HA is back | OCS executor can't actuate | OCS executor can't actuate | OCS supervisor reports timeout |
| Failure (OCS down) | HA-IS unaffected, runs on last setpoints | N/A — OCS *is* the controller | HA-IS phases stall (no transition signals) | HA-IS unaffected, runs on last setpoints |
| Audit completeness | Setpoint writes audited in OCS; shot decisions audited in HA-IS internal log | Fully in OCS | Fully in OCS | Fully in OCS for setpoints; HA-IS for shots |
| AI advisor scope | Setpoints, no shots (except SFW/explicit rescue) | Everything | Phases + shots | Setpoints |
| Schema translation cost | Low (number entities ↔ effective_target) | Very high (every HA-IS entity gets remodeled) | Medium (action types differ) | Low (same as A) |
| Production risk | **Low** | **Very high** | Medium-high | Medium |

---

## 5. Recommendation

**Pick Option A.** It is the only option that satisfies the user's hard
constraint — production irrigation must keep running between
integration passes. HA-IS contains 6,263 lines of working
hardware-control code with empirical safety properties (the user's
plants are alive); reimplementing it (Option B) burns months and risks
plant loss for no functional gain over an integration layer. Option C
makes OCS sit on the critical real-time path; if OCS hangs for one tick
the plants pay. Option D adds protocols (HTTP + MQTT) for no benefit
over A which uses HA's existing REST+WS.

Option A also matches the *shape* of OCS perfectly: OCS already
produces `proposed_changes` against parameter names, validates them
through `app.core.guardrails`, applies them as bounded overlays, and
writes setpoints through the executor's `input_number.set_value`
pattern. The only delta is the entity name (`number.crop_steering_*`
instead of `input_number.{room_id}_setpoint_*`) and the snapshot source
(read HA-IS's RootSense report instead of querying InfluxDB for raw
sensors). Both are small, mechanical changes.

The OCS supervisor stays in **Report mode** for the first integration
pass and earns its way up the rollout (`report_only` →
`supervised_approval` → `bounded_auto_adjust`) on a per-parameter-class
basis. This is exactly the rollout pattern OCS already implements;
HA-IS integration is a customer of that pattern, not a special case.

---

## 6. Concrete integration plan

Each pass is one PR, ships independently, leaves HA-IS production
irrigation running. Rollback = revert the PR; OCS supervisor stays in
Report mode by default so a code revert never strands a "live" overlay.

| Pass | Scope | HA-IS changes | OCS changes | Depends on | Verification |
|---|---|---|---|---|---|
| **P1: HA-IS room registration in OCS** | Register the HA-IS install as one OCS room. Wire the existing `crop_steering.env` zone list as OCS `locations` under the room. Map every VWC/EC sensor entity into the OCS `sensors` table with `external_id = HA entity_id`. Map valves/pump into `equipment` table with `type="irrigation"`. No supervisor ticks yet, no overlay writes — just a populated registry. | none | New: `backend/app/integrations/ha_irrigation/registry.py` — one-shot bootstrap script that reads HA via WebSocket `config/entity_registry/list`, finds `crop_steering_*` entities, infers zone count, inserts `sites`/`locations`/`sensors`/`equipment` rows. Idempotent on `external_id` + `room_id`. Surfaced as a config-wizard step in the existing `backend/app/api/config_wizard.py`. | none | Run wizard. Verify Postgres has one room, N locations (one per zone), 2×N sensors (vwc+ec per zone), N+2 equipment rows (N valves + pump + main valve). All `external_id` values point to real HA entity IDs. |
| **P2: Setpoint write retarget + dry-run executor** | none | New: `backend/app/integrations/ha_irrigation/setpoints.py` — maps OCS param names to HA-IS `number.*` entity ids (table below). Modify `backend/app/workers/executor.py:_setpoint_command` to delegate to this mapper instead of the hardcoded `input_number.{room_id}_setpoint_{param}` convention. Add a `dry_run=True` setting that logs the intended `call_service` payload without sending it. | P1 | Trigger a fake supervisor decision with a setpoint change for one param. Confirm the executor builds a `domain="number", service="set_value", target_entity="number.crop_steering_p1_target_vwc"` command. With `dry_run=True`, confirm no HA call lands. |
| **P3: HA-IS report builder → OCS snapshot source** | Set `switch.crop_steering_intelligence_llm_report_enabled` ON via the migration doc (operator step). | New: `backend/app/integrations/ha_irrigation/snapshot_source.py` — implements a `CropSteeringSnapshotSource` that subscribes to the `crop_steering_rootsense_report` HA event via `HAClient.subscribe_events`, caches the latest payload, and exposes it via `build_snapshot()`. Extend `backend/app/core/snapshot.py:build_snapshot` to prefer the HA-IS report payload over raw InfluxDB queries when a room's `room_context.source == "ha_irrigation"`. | P1, P2 | Wait one full 15-min HA-IS report cycle. Run `Supervisor.tick()` for the room. Confirm `snapshot.payload` contains the HA-IS report's `climate`, `substrate` (per zone), `active_anomalies`, `triage`. No LLM call yet (use a fake `LLMClientProtocol` that asserts the snapshot shape and returns a `report_only` decision with empty `proposed_changes`). |
| **P4: Supervisor live in Report mode** | none | Replace fake LLM with the real LiteLLM client. Confirm rollout stage stays at `report_only` for every parameter class. Wire alerts to Telegram (existing path). | P3 | Let the supervisor run a full lights-on → lights-off day. Inspect `llm_call_log` rows in Postgres. Confirm every tick produces a recorded prompt + response + parse outcome. Confirm zero `runtime_adjustment` rows are inserted. Confirm `event_log` shows one report per non-nominal tick. **HA-IS irrigation continues running unchanged.** |
| **P5: Setpoint sync on lights-on (SFW)** | none | Promote the rollout stage for *one* parameter class (`steering_intent`) from `report_only` to `supervised_approval`. The first time the supervisor proposes a change to `number.crop_steering_steering_intent`, a `pending_approval` row appears in OCS; an admin approves it; the executor enqueues the `number.set_value` write; the readback confirms; an audit row lands. Drop `dry_run=False` for approved commands only. | P4 | Force a non-nominal snapshot (e.g. lower a VPD target to provoke a recommendation). Confirm an `pending_approval` row appears. Approve via the OCS UI. Confirm a command queue row is created, the executor calls HA, `number.crop_steering_steering_intent` actually changes value, the audit row chain advances. **HA-IS picks up the new intent on its next phase tick and adapts shot sizes** (validate via `sensor.crop_steering_p0_dryback_drop_pct_current`). |
| **P6: Bounded auto-adjust on safe parameter classes** | none | Promote `steering_intent`, `ec_target_*`, and `p2_vwc_threshold` to `bounded_auto_adjust`. The guardrail validator already caps deltas per class; runtime adjustments expire at lights-off by default. Verify no parameter that controls a *discrete* shot (e.g. emergency thresholds) is in bounded mode — keep those in SFW. | P5 | Run for one week of autonomous operation. Audit chain shows N controlled-adjustment rows, all within class caps, all expired at lights-off. **No critical incidents.** |
| **P7: Explicit rescue / flush proposals via custom_shot** | none | Extend `app.core.action_set.build_action_set` to include a `custom_shot` action type when the snapshot carries an anomaly code that warrants it (`ec_drift_high`, `dryback_too_fast`). The LLM may propose `{"action":"custom_shot","target_zone":N,"intent":"rebalance_ec","volume_ml":M}`. The executor enqueues this as `domain="crop_steering", service="custom_shot"` — HA-IS's `IrrigationOrchestrator` (already wired) gates and fires it. Always SFW; never auto. | P6 | Manually inject an EC-drift-high anomaly via the test harness. Confirm the supervisor proposes a flush; confirm the SFW pending row appears; approve; confirm `crop_steering.custom_shot` is called with the right payload; confirm `IrrigationOrchestrator.custom_shot` runs the gate and fires the shot. |
| **P8: Deprecate HA-IS L0 report builder; collapse to OCS as sole LLM** | Flip `switch.crop_steering_intelligence_llm_report_enabled` to *internal-debug* (still emits the snapshot for OCS to consume, but no longer renders a triage tag on the dashboard — OCS owns that surface now). Update `dashboards/legacyag/30_intelligence.yaml` LLM-Advisor view to embed an iframe of the OCS room view. Add deprecation banner pointing users to OCS for AI reports. | none | P7 | Open the HA dashboard, see the deprecation banner. Confirm the existing report sensor still updates (OCS needs it). Open OCS UI, see the LLM report rendered there with full audit + approval chain. |

---

## 7. Data-model translation

| HA-IS concept | OCS concept | Recommended representation |
|---|---|---|
| HA-IS install (one HA instance) | `sites` row + `buildings` row + `rooms` row | One `room_id` per HA-IS install. The user runs single-tenant, so `org_id` is the boot-stamped value. |
| HA-IS zone (1-6, from `crop_steering.env`) | `locations` rows under the room, `location_type="zone"` | One location per zone. Use `ZONE_{N}` as the `code`. Persist HA-IS's `PLANT_COUNT`, `MAX_DAILY_VOLUME`, `SHOT_MULTIPLIER` as location attributes. |
| HA-IS VWC sensor (`sensor.crop_steering_vwc_zone_{N}` or the front/back pair `sensor.vwc_zone_{N}_front/back`) | `sensors` row with `type="moisture"`, `data_unit="%"`, `external_id="sensor.crop_steering_vwc_zone_{N}"`, `location_id={zone}` | The averaged HA-IS entity is canonical (HA-IS already does sensor fusion). Front/back raw sensors get their own `sensors` rows with `parent_external_id` reference for diagnostics only. |
| HA-IS EC sensor | `sensors` row, `type="ec"`, `data_unit="mS/cm"` | Same pattern. |
| HA-IS pump (`switch.irrigation_pump` or per env) | `equipment` row, `type="irrigation"`, `external_id={pump_switch}` | One row, `location_id=null` (room-level). |
| HA-IS main valve | `equipment` row, `type="irrigation"`, `external_id={main_valve_switch}`, `location_id=null` | Room-level. |
| HA-IS zone valves (1 per zone) | `equipment` row per zone, `type="irrigation"`, `external_id={zone_N_switch}`, `location_id={zone_N}` | One per zone. |
| HA-IS daily phase (P0/P1/P2/P3) | **Does NOT map to** OCS `grow_recipes.phases` | Phases in OCS are cultivation stages (84-day timeline); HA-IS phases are intra-day. Represent HA-IS phase as a per-room runtime attribute only — a new `room_runtime.intra_day_phase` enum column (P0/P1/P2/P3/unknown), refreshed from `sensor.crop_steering_current_phase` on each supervisor tick. Don't store it in the recipe. |
| HA-IS cultivator-intent slider (`number.crop_steering_steering_intent`, -100..+100) | OCS *steered parameter* `steering_intent` with class **B** (recipe-derived setpoint) | First-class OCS param. Its phase-default lives on the cultivation phase (`Late Veg` defaults to 0, `Stretch` defaults to -20, `Bulk` defaults to -50, etc.). The supervisor proposes delta; executor writes via `number.set_value`. |
| HA-IS EC targets (`number.crop_steering_ec_target_{veg,gen}_p{0..3}`) | OCS *steered parameters* `ec_target_veg_p0` … `ec_target_gen_p3` (8 params) | Each maps one-to-one. The recipe author sets phase defaults; the supervisor adjusts within class caps. |
| HA-IS phase target VWC (`p1_target_vwc`, `p2_vwc_threshold`) | OCS *steered parameters* `vwc_target_p1`, `vwc_target_p2` | Same. |
| HA-IS dryback drop (`veg_p0_dryback_drop_pct`, `gen_p0_dryback_drop_pct`) | OCS *steered parameters* `dryback_drop_veg`, `dryback_drop_gen` | Same. |
| HA-IS cycle day (none — implicit, daily) | OCS `cycle_day` from `room_runtime.cycle_start_date` | OCS owns cultivation cycle day; HA-IS doesn't need to know. The supervisor's snapshot includes it; the LLM uses it to look up the cultivation-stage recipe defaults. |
| HA-IS shot (one valve-open event) | OCS `audit_event` of type `controlled_adjustment` with `params.shot={zone,duration_s,intent}` | When HA-IS fires a shot via `crop_steering.execute_irrigation_shot`, the OCS executor's command queue carries the audit row. For shots HA-IS fires *autonomously* (P1 ramp, P2 maintenance, P3 rescue), HA-IS publishes `crop_steering_irrigation_shot` events; an OCS-side listener writes an `info_event` row so the chain is complete — the shot wasn't *initiated* by OCS but is captured for the audit timeline. |

### The setpoint mapping table (P2)

```python
# backend/app/integrations/ha_irrigation/setpoints.py
SETPOINT_MAP: dict[str, tuple[str, str]] = {
    # param_name -> (HA domain, HA entity_id)
    "steering_intent":     ("number", "number.crop_steering_steering_intent"),
    "dryback_drop_veg":    ("number", "number.crop_steering_veg_p0_dryback_drop_pct"),
    "dryback_drop_gen":    ("number", "number.crop_steering_gen_p0_dryback_drop_pct"),
    "vwc_target_p1":       ("number", "number.crop_steering_p1_target_vwc"),
    "vwc_target_p2":       ("number", "number.crop_steering_p2_vwc_threshold"),
    "shot_size_p2":        ("number", "number.crop_steering_p2_shot_size"),
    "ec_target_veg_p0":    ("number", "number.crop_steering_ec_target_veg_p0"),
    "ec_target_veg_p1":    ("number", "number.crop_steering_ec_target_veg_p1"),
    "ec_target_veg_p2":    ("number", "number.crop_steering_ec_target_veg_p2"),
    "ec_target_veg_p3":    ("number", "number.crop_steering_ec_target_veg_p3"),
    "ec_target_gen_p0":    ("number", "number.crop_steering_ec_target_gen_p0"),
    "ec_target_gen_p1":    ("number", "number.crop_steering_ec_target_gen_p1"),
    "ec_target_gen_p2":    ("number", "number.crop_steering_ec_target_gen_p2"),
    "ec_target_gen_p3":    ("number", "number.crop_steering_ec_target_gen_p3"),
    "ec_target_flush":     ("number", "number.crop_steering_ec_target_flush"),
    # zone-level overrides — index suffix appended at call time
    "zone_shot_multiplier": ("number", "number.crop_steering_zone_{zone}_shot_size_multiplier"),
    "zone_max_daily_volume": ("number", "number.crop_steering_zone_{zone}_max_daily_volume"),
}

# Special: discrete shot proposals route through custom_shot, not set_value
CUSTOM_SHOT_PARAM = "custom_shot"  # synthetic param the action_set may include
```

---

## 8. AI advisor integration

### What HA-IS's L0 advisor does today

`appdaemon/apps/crop_steering/intelligence/llm/report_builder.py` is a
~400-line `IntelligenceApp` that:

1. Subscribes to `anomaly.detected` on the in-process `RootSenseBus`.
2. Every 15 min (configurable), calls `build_report(now=...)` to assemble a
   ~300-token JSON snapshot.
3. Computes a local-rule "triage" tag (`anomaly:<code>` > `drift:<metric>`
   > `heartbeat` > `ok`).
4. Publishes the snapshot as `sensor.crop_steering_rootsense_report_latest`
   (state = triage; attributes = full payload), as an HA event
   `crop_steering_rootsense_report`, and onto the bus as `report.ready`.
5. **Never calls an LLM.** The guard test
   `tests/intelligence/test_llm_report_builder.py` asserts that no LLM
   client library is even importable in the module's namespace — by
   construction, L0 cannot call one.

The whole point of L0 is to bake the schema and triage rules
*before* any tokens get spent. It's the perfect handoff point for OCS.

### How OCS's supervisor relates

`backend/app/workers/supervisor.py` runs every 5 min, builds a snapshot
via `app.core.snapshot.build_snapshot`, and (for non-nominal rooms)
calls the LLM with:

- `system_content = full_system_prompt()` = `prompts/system.md` ++
  `prompts/grow_room_agent_playbook.md` — the **same playbook** that
  HA-IS's L0 schema was designed around. The Legacy Ag playbook is
  already loaded into the system prompt every tick.
- `user_message` containing the snapshot, the breaches, and the
  deterministic `allowed_action_set`.

The supervisor parses the response strictly against
`schemas/llm_decision.py:LLMDecision` (`ocs.llm_decision.v1`), writes
an `llm_call_log` row with the full prompt + raw response + parse
outcome, and routes by per-parameter-class rollout stage
(`report_only` → `supervised_approval` → `bounded_auto_adjust`). Every
action is gated by `app.core.guardrails.validate_all`.

### The decision

**OCS supervisor is the only LLM caller after Pass 8.** HA-IS L0
report builder is deprecated in the sense that:

- It keeps building the snapshot, because OCS subscribes to that
  snapshot as its richest source of substrate-side data (per-zone VWC,
  EC, dryback velocity, field-capacity-observed, EC stack index). OCS
  doesn't have InfluxDB-aware substrate analytics; HA-IS does.
- It no longer publishes a *triage tag* meant for human dashboard
  consumption — OCS owns that surface, with the full audit + approval
  + guardrail chain.
- The HA dashboard "LLM Advisor" view in
  `dashboards/legacyag/30_intelligence.yaml` is replaced with a link
  (or iframe) into the OCS room dashboard.
- The `switch.crop_steering_intelligence_llm_report_enabled` switch
  stays ON because OCS needs the snapshot. Its label changes from
  "LLM Advisor" to "OCS snapshot publisher".

The L0 module is **not deleted**. Two LLMs in the loop would be wrong;
two snapshot builders, one of which feeds the other, is exactly right.

---

## 9. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | **Breaking production irrigation.** The user is feeding plants with HA-IS today; an integration bug that stops shots, fires unwanted shots, or corrupts a setpoint affects living crops. | Option A keeps HA-IS *unchanged* through Pass 7. Every OCS-side change defaults to `report_only`, then `dry_run=True`, then per-class promotion. No "switch the integration on" moment. P5-P7 each promote *one parameter class* at a time. Rollback any pass by reverting the OCS PR; HA-IS keeps running on the last setpoints. |
| R2 | **OCS writes a setpoint outside HA-IS's valid range.** `number.crop_steering_p1_target_vwc` validates 30-95; OCS's guardrails don't know about that. | The setpoint mapper carries the HA-IS-side range (from `ENTITIES.md`) and clamps before enqueueing. A clamp event is a `system_warning` so the supervisor sees its proposal was reshaped. Plus the readback verification catches any HA-side rejection. |
| R3 | **Setpoint write succeeds but HA-IS ignores it because system_enabled is OFF or zone is in manual override.** OCS would report success while irrigation is paused. | The snapshot includes `switch.crop_steering_system_enabled` and per-zone `manual_override` state. If OFF, the supervisor short-circuits to `report_only` regardless of rollout stage (mirrors the existing `room_runtime.paused` skip-reason). The integration registry pass surfaces these switches as OCS `equipment.status` modifiers. |
| R4 | **HA-IS RootSense L0 report becomes stale (e.g., AppDaemon crash).** OCS supervisor would reason over outdated substrate state. | `CropSteeringSnapshotSource` tracks the report's `ts` field. A report older than 2× the configured interval (default 30 min) flips the room to `IMPAIRED`, which suppresses LLM ticks (see `Supervisor._skip_reason` — `not_healthy`). |
| R5 | **HA-IS phases (P0-P3) confused with OCS recipe phases (Early Veg, Late Veg…).** A future contributor edits the OCS recipe planner thinking P0/P1/P2/P3 are cultivation phases. | Document explicitly in `docs/concepts/ha-irrigation-strategy-integration.md` §7. Add a code-level comment in `room_runtime.intra_day_phase` field definition pointing to this section. Use distinct names everywhere — `intra_day_phase` for HA-IS, `cultivation_phase` for OCS recipes. |
| R6 | **The bounded-auto-adjust path writes a setpoint, but HA-IS's RootSense IntentResolver overwrites it on the next tick.** The intent slider drives derived setpoints; if OCS sets `p1_target_vwc` directly *and* sets `steering_intent`, the resolver wins. | Only one of `{steering_intent}` or `{p1_target_vwc, p2_vwc_threshold, dryback_drop_*, p1_initial_shot_size, ec_target_flush}` is in the steered-param set. Default: OCS owns `steering_intent` (the single slider HA-IS designed for external control) and lets HA-IS derive the rest. The per-phase EC targets are independent — OCS can steer them directly without conflict. |
| R7 | **Telegram approval pile-up.** If the supervisor produces a setpoint proposal every tick during a long disturbance, every approval bot user gets spammed. | OCS already has SFW-pending TTL (90 min) + `expire_stale_pending` sweep. Add: cap pending approvals per room to 1 — the supervisor's `_skip_reason` already says "skip if open pending approval". Verified for free. |
| R8 | **HA-IS irrigation continues with a stale OCS-applied setpoint after OCS goes offline.** An overlay should expire, but the *projected* `number.crop_steering_*` entity does not auto-revert. | The OCS executor's existing pattern handles this: overlays expire at lights-off by default. Add a lights-off command batch that re-applies the recipe baseline value to every steered entity when its overlay expires. This mirrors `on_lights_on`; call it `on_lights_off`. |

---

## 10. Open questions for the user

| # | Question | Recommended default |
|---|---|---|
| Q1 | Should OCS run as the HA add-on inside the user's HA install, or as standalone Docker on a separate host? | **HA add-on, single-tenant.** OCS already supports both modes (`SUPERVISOR_TOKEN` detection); add-on mode reduces moving parts and lives next to the existing AppDaemon add-on. |
| Q2 | After Pass 8, do we keep the HA-IS L0 report builder running (as OCS's snapshot source) or replace it with a direct OCS-side InfluxDB scrape? | **Keep the L0 builder.** It encapsulates HA-IS-specific substrate analytics (RootSense `dryback_velocity`, `field_capacity_observed`, `ec_stack_index`) that OCS doesn't compute. Replacing it would duplicate working code. The switch's label changes from "LLM Advisor" to "OCS snapshot publisher". |
| Q3 | When the supervisor proposes a discrete *shot* (rescue / flush), should it go through `crop_steering.custom_shot` (HA-IS gated path) or directly to `crop_steering.execute_irrigation_shot`? | **`custom_shot`.** It's already gated by anomaly suppression, manual-override respect, flow-rate conversion, and the operator-facing intent label. `execute_irrigation_shot` is the lower-level event channel; we want the gates. |
| Q4 | What's the cycle-start anchor for the OCS `room_runtime.cycle_start_date` field? HA-IS has no per-cycle marker. | **Operator picks a date during the P1 registration wizard step.** Default to "today" with a calendar picker. Cycle restart is a manual operator step in OCS (already true). |
| Q5 | Should the integration support multiple HA-IS instances (e.g. a flower room and a veg room each with its own HA-IS install)? | **No, single-instance for v1.** The user runs one HA-IS install today. Multi-instance can be added later by giving each HA WS URL its own `room_id` in OCS. |
| Q6 | Should HA-IS continue exposing `binary_sensor.crop_steering_anomaly_active` once OCS is the LLM caller, or should the anomaly scanner move into OCS? | **Keep it in HA-IS.** Same logic as Q2 — substrate-side anomaly detection benefits from being co-located with the sensor fusion code that runs every 60 s. OCS subscribes to the binary sensor and the per-zone `active_codes` attribute. |
| Q7 | Should the OCS UI embed the HA-IS dashboards, or just link to them? | **Link.** Embedding HA dashboards in OCS requires CORS / ingress reverse-proxy headaches. Each tool keeps its native UI; OCS has a "Open HA dashboard" button per room. |
| Q8 | What's the cutover plan for the user's existing v3.x apps.yaml? Do we need a migration script? | **No script.** Pass 1 just inserts OCS-side rows (idempotent registration). Pass 3 requires the operator to flip one HA-IS switch (`switch.crop_steering_intelligence_llm_report_enabled`) ON. All subsequent passes are OCS-only changes. Document the one switch-flip in `MIGRATION.md` addendum. |

---

### Critical files for implementation

- `C:\Github\open-crop-steering\backend\app\workers\executor.py` (lines 430-448 — `_setpoint_command`)
- `C:\Github\open-crop-steering\backend\app\workers\supervisor.py` (lines 446-583 — `_run_llm_report`; snapshot consumption)
- `C:\Github\open-crop-steering\backend\app\core\snapshot.py` (add HA-IS report passthrough)
- `C:\Github\HA-Irrigation-Strategy\appdaemon\apps\crop_steering\intelligence\llm\report_builder.py` (the snapshot source OCS consumes)
- `C:\Github\HA-Irrigation-Strategy\appdaemon\apps\crop_steering\intelligence\orchestration.py` (lines 50-109 — `_on_custom_shot_event`, the rescue/flush path)
- `C:\Github\HA-Irrigation-Strategy\custom_components\crop_steering\services.py` (the integration service surface OCS calls into)
