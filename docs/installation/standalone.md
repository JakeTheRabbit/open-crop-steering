# Install as standalone Docker

The same image that runs as a Home Assistant add-on also runs as a plain Docker
container. The image **detects its mode** by the presence of the
`SUPERVISOR_TOKEN` environment variable: when it is absent, the app runs in
standalone mode.

Use standalone mode for development, for a non-production test instance (e.g.
the regression instance used when bumping a pinned version), or when Home
Assistant is reachable over the network but the controller runs elsewhere.

!!! note "Add-on vs standalone — the differences"
    | | Add-on | Standalone |
    |---|---|---|
    | Identity | HA Ingress headers (`X-Remote-User-Id`) | JWT — the app's own users + login UI |
    | HA auth | `SUPERVISOR_TOKEN`, injected | `HA_TOKEN` long-lived access token |
    | Config | HA add-on options form | Environment variables |
    | Postgres | Bundled in the container | Bundled, or point `DATABASE_URL` elsewhere |

## Run it

```bash
docker run -d \
  --name open-crop-steering \
  -e HA_URL=https://homeassistant.example.com:8123 \
  -e HA_TOKEN=eyJ... \
  -e INFLUX_URL=http://influx:8086 \
  -e INFLUX_TOKEN=... \
  -e INFLUX_ORG=your-org \
  -e INFLUX_BUCKET=homeassistant \
  -e DATABASE_URL=postgresql+asyncpg://ocs:ocs@127.0.0.1:5432/open_crop_steering \
  -e LLM_BASE_URL=http://litellm:4000 \
  -e LLM_API_KEY=sk-... \
  -e LLM_MODEL=claude-opus-4-7 \
  -e HMAC_KEY_1=<64-hex-char audit key> \
  -e HMAC_KEY_ID_CURRENT=1 \
  -e TELEGRAM_BOT_TOKEN=... \
  -e TELEGRAM_CHAT_ID=... \
  -p 8099:8099 \
  -v ocs_data:/data \
  ghcr.io/JakeTheRabbit/open-crop-steering:latest
```

Then open `http://<host>:8099`.

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `HA_URL` | Yes | Home Assistant base URL. |
| `HA_TOKEN` | Yes | Long-lived access token from an HA user. |
| `DATABASE_URL` | No | Defaults to the bundled Postgres. SQLAlchemy async URL. |
| `INFLUX_URL` / `INFLUX_TOKEN` / `INFLUX_ORG` / `INFLUX_BUCKET` | Yes | InfluxDB 2.x connection. No auto-discovery. |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | For AI modes | OpenAI-compatible endpoint. |
| `HMAC_KEY_<n>` | Yes | Audit HMAC key, 64 hex chars (32 bytes). One or more; `<n>` is the key id. |
| `HMAC_KEY_ID_CURRENT` | Yes | Which `HMAC_KEY_<n>` signs new audit rows. Older keys remain for verification. |
| `SEAL_EXPORT_TARGET` | No | Off-box target for daily seal exports (`s3://…`, `b2://…`). |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | No | Telegram alerts + approvals. |
| `API_PORT` | No | Defaults to `8099`. |
| `LOG_LEVEL` | No | `info` default. |
| `SUPERVISOR_TICK_SECONDS` | No | AI tick interval, default `300`. |

!!! danger "The audit HMAC key"
    In standalone mode **you** generate and supply `HMAC_KEY_1`. Generate it
    with `openssl rand -hex 32`. Losing it makes the audit chain
    unverifiable. Leaking it lets someone forge audit rows. Store it in a
    secret manager, not in a committed `.env`.

## The `/data` volume

Mount a persistent volume at `/data`. It holds:

- `postgres/` — the database data directory.
- `postgres-backups/` — `pg_dump` cold backups.
- `audit-seal-exports/` — daily sealed audit exports.
- `influx-cache/` — cached InfluxDB schema metadata.

Back this volume up. In a regulated facility, restore is a tested procedure —
see the private repo's `validation/08_backup_restore_test.md`.

## Health checks

| Endpoint | Meaning |
|---|---|
| `GET /healthz` | Process is up. Touches nothing. |
| `GET /readyz` | Process is ready. Touches Postgres; reports mode + DB status. |

Wire `/readyz` into your container orchestrator's health probe.

## Standalone parity

Standalone mode runs the full flow — recipes, overlays, command queue,
audit chain, AI supervisor. The one functional difference is identity: instead
of trusting HA Ingress headers, the app issues and validates its own JWTs and
presents a login UI. Everything else — including the audit guarantees — is
identical to add-on mode.
