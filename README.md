# Open Crop Steering

> **⚠️ Reference / Unvalidated** — not for use as the sole controller in regulated cultivation without local validation evidence. See [`docs/compliance/`](docs/compliance/) for what "validated" looks like.

A Home Assistant add-on (with standalone Docker fallback) that turns cultivation recipes into immutable, versioned plans; layers AI-driven runtime overlays bounded by hard guardrails; and produces tamper-evident audit records suitable for regulated medicinal cannabis facilities.

**Status:** v0.1 in development.

## What it is

- **HA add-on** — install via Supervisor, runs in your HA. Standalone Docker mode also supported.
- **AI-supervised cultivation control plane** — three modes (Report / SFW / YOLO) bounded by hard guardrails the AI can't override.
- **Recipe-as-truth, overlays for runtime nudges** — canonical recipes are immutable; AI corrections are time-bounded `runtime_adjustment` overlays that expire automatically.
- **Tamper-evident audit log** — Postgres `audit_event` table with HMAC chain + key rotation + off-box daily seal. Inspector-friendly export.
- **Equipment-aware** — knows when AC is saturated, when CO₂ can't keep up, when irrigation didn't deliver.
- **Cannabis-explicit** — cultivation knowledge baked in (12 anti-patterns, 15 equipment-coupling rules, 7 saturation indicators). 12-week preset ships with the integration.
- **Two-track distribution** — public reference here; facility-specific configs and validation packs live in private downstream repos.

## Architecture

```
┌─ HA add-on: open_crop_steering (single container, s6 services) ────┐
│  postgres   api   supervisor   executor   alerts   seal            │
│      └──────┴──────┬─────┴─────────┴────────┴───────┘              │
│              Postgres (recipes, overlays, command queue,            │
│              audit, RBAC, deltas, snapshots, llm logs)              │
└──────────────────┬─────────────────────────────────────────────────┘
                   │ HA REST + WS  (call_service only; readback verified)
                   ▼
        Home Assistant Core
        + Crop Steering integration (1 instance, 6 zones, auto OFF)
        + AC Infinity / IntesisHome / ESPHome dosers / etc.
        + InfluxDB add-on (sensor history → Flux queries)

External: LiteLLM + Telegram + off-box immutable storage (audit seals)
```

## Stack

| Layer | Tech |
|---|---|
| Container | HA add-on base image + s6-overlay + bundled Postgres 16 |
| Backend | Python 3.12 · FastAPI · SQLAlchemy 2.0 · Alembic · pgcrypto · python-telegram-bot · InfluxDB 2.x client · httpx |
| Frontend | Next.js 15 (`output: 'export'`) · React 19 · TanStack Query · Tailwind · shadcn/ui |
| AI | OpenAI-compatible API (default: LiteLLM + Claude Opus 4.7) · strict JSON-schema response · deterministic action-set validator |
| Audit | Postgres `audit_event` table · HMAC chain · `key_id` rotation · daily seal exported off-box |
| RBAC | 4 roles (operator / cultivator / QAP / admin) · HA ingress identity in add-on mode · JWT in standalone |

## Install

### As HA add-on (recommended)

1. Settings → Add-ons → Add-on Store → ⋮ → Repositories → add `https://github.com/JakeTheRabbit/open-crop-steering`
2. Install **Open Crop Steering**, configure rooms / zones / equipment / Telegram / LLM endpoint via the UI form
3. Start. Open via the side-panel link (HA Ingress, no separate auth).

See [`docs/installation/addon.md`](docs/installation/addon.md) for the full walkthrough.

### As standalone Docker

```bash
docker run -d \
  -e HA_URL=https://homeassistant.example.com:8123 \
  -e HA_TOKEN=eyJ... \
  -e INFLUX_URL=http://influx:8086 -e INFLUX_TOKEN=... \
  -e INFLUX_ORG=... -e INFLUX_BUCKET=homeassistant \
  -e DATABASE_URL=postgresql://... \
  -e LLM_BASE_URL=http://litellm:4000 -e LLM_API_KEY=sk-... \
  -e LLM_MODEL=claude-opus-4-7 \
  -e TELEGRAM_BOT_TOKEN=... -e TELEGRAM_CHAT_ID=... \
  -p 8099:8099 \
  ghcr.io/JakeTheRabbit/open-crop-steering:latest
```

See [`docs/installation/standalone.md`](docs/installation/standalone.md).

## How AI control works (the safety story)

The AI is bounded by **architecture**, not by restricting what it can suggest:

1. **Recipes are immutable.** AI never patches them. AI nudges are `runtime_adjustment` overlays that expire (default: at lights-off).
2. **Effective target = recipe + sum(active overlays).** Executor materializes this and writes via the command queue.
3. **Command queue with idempotency + readback verification.** Every HA service call has an idempotency key. We confirm the entity actually became what we wrote. Failures retry then escalate.
4. **Bounded action validator.** Deterministic engine pre-computes `allowed_action_set`. AI proposes free-form. If proposal ∈ set → apply. If ∉ set but within bounds → apply with `novel_proposal: true` + halve cumulative budget. If outside bounds → reject + cool-down.
5. **Cumulative caps + no-touch windows + cool-down.** Per-param `max_delta_24h` and `_7d`. Configurable cron-style no-touch windows (sampling, harvest prep, scheduled QA visits). 60-min cool-down on guardrail rejection (2h for tank chemistry).
6. **Equipment-saturation aware.** AC saturated? AI doesn't propose lower temp. Dehumidifier maxed? AI doesn't propose lower RH.
7. **Param risk classes A-E.** Class E (admin / control state — roles, rollout stage, guardrail bounds) — AI never writes.
8. **Tamper-evident audit.** Every change recorded with HMAC-chained row, key_id, off-box daily seal. Inspector-friendly export.

See [`docs/concepts/`](docs/concepts/) for the full mental model.

## Status

- [ ] Phase 0 — Repo bootstrap + HA contract validation
- [ ] Phase 1 — Backend skeleton + 14-table schema
- [ ] Phase 2 — HA + InfluxDB clients
- [ ] Phase 3 — Recipe revisions + overlays + cannabis preset
- [ ] Phase 4 — Command queue + executor worker
- [ ] Phase 5 — Audit + RBAC + ingress
- [ ] Phase 6 — Deterministic monitoring + event taxonomy
- [ ] Phase 7 — LLM Report mode (no control writes)
- [ ] Phase 8 — SFW workflow (UI + Telegram)
- [ ] Phase 9 — Bounded auto-adjust
- [ ] Phase 10 — Next.js UI
- [ ] Phase 11 — Add-on packaging
- [ ] Phase 12 — Standalone parity
- [ ] Phase 13 — Docs + validation pack templates
- [ ] Phase 14 — Facility deploy + 8-week graduated rollout

## License

MIT. See [LICENSE](LICENSE).

## Credits + soft dependencies

- Builds on top of (but does not replace) [JakeTheRabbit/HA-Irrigation-Strategy](https://github.com/JakeTheRabbit/HA-Irrigation-Strategy) ("Crop Steering"). We use it for shot mechanics; we don't fork it. Pin the tested version.
- Cultivation knowledge derived from internal field guide; see [`docs/concepts/cultivation_knowledge.md`](docs/concepts/cultivation_knowledge.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). PRs welcome; please open an issue first for non-trivial changes.

## Security

See [SECURITY.md](SECURITY.md) for vulnerability disclosure.
