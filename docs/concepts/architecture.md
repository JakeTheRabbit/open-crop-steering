# Architecture

Open Crop Steering is a single Docker container. Inside it, `s6-overlay`
supervises six long-running processes; they coordinate through a bundled
Postgres database using advisory locks so that exactly one of each loop is
active even if the API restarts.

## The container

```
┌─ HA add-on: open_crop_steering (single container, s6-overlay) ─────┐
│                                                                    │
│  s6 services:                                                      │
│  ┌──────────┐ ┌────────┐ ┌────────────┐ ┌──────────┐ ┌────────┐    │
│  │ postgres │ │ api    │ │ supervisor │ │ executor │ │ alerts │    │
│  │ embedded │ │FastAPI │ │ worker     │ │ worker   │ │ worker │    │
│  │          │ │+ UI    │ │ AI tick +  │ │ command  │ │ digest │    │
│  │          │ │(Ingress)│ │ tolerance  │ │ queue +  │ │+ daily │    │
│  │          │ │        │ │ + state    │ │ HA call +│ │ seal   │    │
│  │          │ │        │ │ machine    │ │ readback │ │ (seal) │    │
│  └────▲─────┘ └───▲────┘ └─────▲──────┘ └────▲─────┘ └───▲────┘    │
│       │           │            │             │           │         │
│       └───────────┴──────┬─────┴─────────────┴───────────┘         │
│                          │ all coordinate via Postgres advisory     │
│                          │ locks — one of each loop active at most  │
│                          ▼                                          │
│              Postgres (14+ tables)                                  │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ HA REST + WebSocket
                           │ (call_service only; readback verified)
                           ▼
                  Home Assistant Core
                  + Crop Steering integration (irrigation shots)
                  + climate / dehumidifier / CO₂ / fan / doser entities
                  + InfluxDB add-on (sensor history → Flux queries)

External: LiteLLM (LLM endpoint) · Telegram · off-box storage (audit seals)
```

## The six processes

| Process | s6 service | Responsibility |
|---|---|---|
| **Postgres** | `postgres` | The bundled database. All durable state. |
| **API** | `api` | FastAPI HTTP server + the static Next.js UI bundle. Serves through HA Ingress in add-on mode. |
| **Supervisor** | `supervisor` | The AI tick loop. Builds a sensor snapshot, runs the deterministic monitoring + state machine, calls the LLM, runs the guardrail validator. Holds the `supervisor_tick` advisory lock. |
| **Executor** | `executor` | Consumes the command queue. Calls Home Assistant, verifies the readback, retries on failure. Holds the `executor_consume` advisory lock. |
| **Alerts** | `alerts` | Routes events to the three Telegram severity tiers; builds the daily digest. |
| **Seal** | `seal` | Computes the daily audit seal, writes the off-box export, runs the `pg_dump` cold backup. Holds the `seal_daily` advisory lock. |

Why split processes? A crash or restart of the API must not stop the executor
mid-command, and two supervisor ticks must never run at once. Advisory locks
make duplication harmless: a second copy of any loop simply finds the lock held
and does nothing.

## The database

Postgres holds 15 tables (the Phase 1 baseline schema plus `room_runtime`):

| Table | Purpose |
|---|---|
| `recipe_revision` / `recipe_revision_param` | Immutable cultivation recipes. Frozen by a Postgres trigger once `approved`. |
| `runtime_adjustment` | Bounded, time-limited AI/operator overlays. They expire. |
| `effective_target` | A **materialized view**: `recipe value + sum(active overlays)`. |
| `command_queue` / `command_batch` | The outbox. Every HA service call, with an idempotency key and readback evidence. |
| `audit_event` | The tamper-evident HMAC chain. INSERT-only — UPDATE and DELETE are blocked by triggers. |
| `daily_seal` | One chain-head seal per day, exported off-box. |
| `event_log` | The operational event taxonomy (info → critical incident). |
| `pending_approval` | Open SFW proposals awaiting a human decision. |
| `telegram_user_map` | Telegram chat id → HA user mapping (gates Telegram approval authority). |
| `users` / `roles` / `user_roles` | RBAC. |
| `cumulative_delta` | The rolling 24h / 7d delta sums and cool-down state per parameter. |
| `sensor_snapshot` | The cached room state the AI reasoned about, by `snapshot_id`. |
| `llm_call_log` | Every prompt, response, and parse/validation result. |
| `room_runtime` | The one mutable per-room surface — rollout stage, cycle-start anchor, health state, mute/pause. |

## The control path

A change reaches Home Assistant only through this chain:

```
recipe_revision (approved, immutable)
        +  runtime_adjustment (bounded overlays, expires_at)
        =  effective_target  (materialized view)
        ↓
command_queue  (idempotency key, target entity, expected value)
        ↓  executor worker
HA call_service  (never set_state)
        ↓
readback: the entity actually became the value we wrote  ✓
        ↓
audit_event  (event_type, applied value, readback evidence, HMAC)
```

There is no path from the AI directly to a Home Assistant service call. The AI
proposes; the validator decides; the overlay is recorded; the executor applies
and reads back; the audit row captures it.

## Talking to Home Assistant

- **Control** is only ever `call_service`. The system never uses `set_state` to
  fake an entity into a value.
- Every command is **read back**: after the service call, the executor confirms
  the target entity actually holds the value. A mismatch retries, then
  escalates.
- **Current state** comes from the Home Assistant WebSocket, not InfluxDB.
  InfluxDB is used for *trends* (slopes, "has been above X for N minutes"), not
  for the live actuator state.
- The **lights switch** is a hardware trigger: the executor subscribes to the
  actual `switch.*` / `light.*` state change to apply the day's setpoints. An
  `input_datetime` helper is a fallback; a drift greater than two minutes
  between them is a critical incident.

## Modes: add-on and standalone

The same image runs both ways. It detects its mode from the `SUPERVISOR_TOKEN`
environment variable:

- **Add-on mode** — Home Assistant Supervisor is present. Identity comes from
  Ingress headers; HA calls use the Supervisor token.
- **Standalone mode** — no Supervisor. The app issues its own JWTs with a login
  UI; HA calls use a configured long-lived token.

See [Installation → Standalone Docker](../installation/standalone.md).
