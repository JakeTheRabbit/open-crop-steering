# Open Crop Steering — add-on documentation

AI-supervised cannabis cultivation control plane, packaged as a Home
Assistant add-on. This page is shown in the add-on's **Documentation**
tab.

> **Cannabis cultivation tool.** This add-on is for licensed medicinal
> cannabis cultivation (NZ GACP context). See the repository README for
> the regulatory framing.

## What it does

The add-on runs a single container with six s6-supervised services:

| Service | Role |
|---|---|
| `postgres` | Bundled PostgreSQL 16 — recipes, command queue, audit chain. |
| `api` | FastAPI app + the static planner UI, served under HA Ingress. |
| `supervisor` | The AI tick loop (Report / SFW / bounded auto-adjust). |
| `executor` | Drains the command queue into HA service calls + readback. |
| `alerts` | Severity-routed notifications + the daily digest. |
| `seal` | Daily tamper-evident audit seal + cold backup. |

The same image also runs standalone (outside HA) via
`docker-compose.standalone.yml` in the repo root — mode is detected at
runtime from the presence of `SUPERVISOR_TOKEN`.

## Installation

1. In Home Assistant: **Settings -> Add-ons -> Add-on Store**, open the
   menu (top-right), **Repositories**, and add this repository URL.
2. Install **Open Crop Steering** from the store.
3. Configure it (see **Options** below) — at minimum the InfluxDB and
   LiteLLM connection details.
4. Start the add-on. Open the UI from the sidebar (**Grow Control**).

## Options

All options are set in the add-on **Configuration** tab. No
auto-discovery — every integration is configured explicitly.

| Option | Required | Notes |
|---|---|---|
| `log_level` | no | `trace`/`debug`/`info`/`notice`/`warning`/`error`/`fatal`. |
| `llm_base_url` | yes | LiteLLM-compatible endpoint, e.g. `http://litellm:4000`. |
| `llm_api_key` | maybe | Bearer key for the LLM endpoint, if it requires one. |
| `llm_model` | no | Model name; defaults to `claude-opus-4-7`. |
| `influx_url` | yes | InfluxDB 2.x URL. |
| `influx_token` | yes | InfluxDB API token. |
| `influx_org` | yes | InfluxDB organisation. |
| `influx_bucket` | no | Bucket name; defaults to `homeassistant`. |
| `telegram_bot_token` | no | Telegram bot token; omit for dashboard-only alerts. |
| `telegram_chat_id` | no | Destination chat id. |
| `hmac_key_current` | no | Audit-chain HMAC key (hex). **Leave blank** — one is generated on first boot and persisted to `/data/hmac_key`. |
| `hmac_key_id_current` | no | Key id for new audit rows; defaults to `1`. |
| `seal_export_target` | no | Off-box seal export target (`s3://...` / `b2://...`). |

> **HMAC key.** If you leave `hmac_key_current` blank, a fresh 32-byte
> key is generated on first start and stored in `/data/hmac_key`. Do not
> rotate it casually — rotation must go through the in-app admin action
> so historical audit rows stay verifiable.

## Storage

- `/data` (add-on persistent volume, included in HA backups):
  - `postgres/` — the bundled cluster's data directory.
  - `postgres-backups/` — `pg_dump` cold backups.
  - `audit-seal-exports/` — nightly sealed audit exports.
  - `influx-cache/` — cached InfluxDB schema metadata.
- `/backup` — the pre-backup `pg_dump` hook lands a consistent dump here
  before Home Assistant snapshots the add-on.

## Backups

Before HA takes an add-on backup, run a cold dump so the snapshot
captures a consistent database rather than a live data directory:

```
python -m app.cli cold-backup
```

This is the pre-HA-backup hook (`pg_dump --format=directory --jobs=4`).

## Identity & roles

In add-on mode the UI is behind HA Ingress; the operator's identity comes
from the `X-Remote-User-Id` header HA injects. Roles
(operator / cultivator / QAP / admin) are enforced server-side.

## Verifying after install

Hitting the Ingress URL, end-to-end identity capture, the lights-switch
trigger, and the InfluxDB schema test all require a real HA host with the
configured hardware — those checks are part of the Phase 14 facility
rollout, not something the build can self-verify.
