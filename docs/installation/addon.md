# Install as a Home Assistant add-on

This is the recommended way to run Open Crop Steering. The add-on is a single
Docker container managed by the Home Assistant Supervisor. It bundles its own
Postgres database and runs every background worker under `s6-overlay`.

!!! note "What you need first"
    - Home Assistant OS or Supervised (the add-on store is required —
      HA Container does not have a Supervisor).
    - An InfluxDB 2.x instance with Home Assistant sensor history flowing into
      it (the InfluxDB add-on is fine).
    - An OpenAI-compatible LLM endpoint (e.g. LiteLLM). Optional for Report
      mode evidence-gathering, required before any AI control.
    - A Telegram bot token + chat id if you want alerts and approvals over
      Telegram.

## 1. Add the repository

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Open the **⋮** menu (top right) → **Repositories**.
3. Add: `https://github.com/JakeTheRabbit/open-crop-steering`
4. Close the dialog. **Open Crop Steering** now appears in the store.

## 2. Install

1. Click **Open Crop Steering → Install**. First install pulls the image and
   may take several minutes.
2. Do **not** start it yet — configure it first.

## 3. Configure

Open the add-on's **Configuration** tab. The form is driven by the add-on's
`config.yaml` schema. The fields you must set:

| Field | What it is |
|---|---|
| `influx_url` | URL of your InfluxDB 2.x instance, e.g. `http://a0d7b954-influxdb:8086`. |
| `influx_token` | InfluxDB API token with read access to the HA bucket. |
| `influx_org` | InfluxDB organisation. |
| `influx_bucket` | Bucket HA sensor data lands in (default `homeassistant`). |
| `llm_base_url` | OpenAI-compatible endpoint, e.g. `http://192.168.x.x:4000`. |
| `llm_api_key` | API key for that endpoint. Store via HA secrets, never inline. |
| `llm_model` | Model id (default `claude-opus-4-7`). |
| `telegram_bot_token` | Telegram bot token (optional). |
| `telegram_chat_id` | Default Telegram chat id (optional). |
| `log_level` | `info` for normal operation; `debug` while setting up. |

The HMAC audit key is **generated on first start** — you do not supply it. Key
rotation is an admin action later. Back up your HA snapshot encryption key:
that, plus the `/data` volume, is what protects the audit key at rest.

!!! warning "Secrets"
    Put `influx_token`, `llm_api_key` and `telegram_bot_token` in Home
    Assistant `secrets.yaml` and reference them. Never type secrets directly
    into the add-on options — they end up in `/data/options.json`.

## 4. Start

1. **Save** the configuration, then **Start** the add-on.
2. Watch the **Log** tab. A clean start shows: Postgres init, Alembic
   migrations applied, each `s6` worker (`api`, `supervisor`, `executor`,
   `alerts`, `seal`) coming up, and `Uvicorn running`.
3. The side panel gains a **Grow Control** link. Open it — this is the UI,
   served through HA Ingress, so it uses your HA login (no separate password).

## 5. Configure rooms and equipment

The first time the UI loads it runs the **room-configuration wizard**. For each
room you map:

- Lights switch entity (the executor triggers on its state change).
- Climate / dehumidifier / CO₂ / circulation-fan entities.
- Per-zone VWC and EC sensors.
- Per-tank pH and EC sensors and doser-pump entities.
- Role assignments (which HA users are operator / cultivator / QAP / admin).

The wizard enforces the equipment-coupling rules — see
[Operations → Coupling rules](../operations/coupling_rules.md). Some missing
mappings are **hard refusals** (the config will not save); others are
**fail-soft warnings** (saved with a banner).

## 6. Seed your recipes

If you are migrating from an existing day-by-day setpoint table, the
`tools/seed_from_week_data.py` helper imports it as the first immutable
approved recipe revision per room. Otherwise, build the recipe in the planner
and have a QAP approve it.

## 7. Verify before relying on it

Before the system controls anything:

- The InfluxDB **Test connection + schema** button (in the wizard) passes.
- A test write to a setpoint helper is read back correctly.
- The lights-switch trigger fires the executor within a few seconds.

The facility-side deployment runbook and the 8-week graduated rollout gates are
in the private repo's validation pack (`validation/14_sop_grow_control.md`).
The first production cutover should follow that runbook, not this page.

## Updating

Add-on updates are a **controlled change** in a regulated facility — the pinned
version is bumped through the change-control procedure, not by clicking
"Update" on a whim. See [Compliance → Overview](../compliance/overview.md).
