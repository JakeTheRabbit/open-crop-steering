# API reference

Open Crop Steering exposes an HTTP API served by the `api` worker. In add-on
mode it is reached through Home Assistant Ingress; in standalone mode directly
on the configured port (default `8099`).

This page documents the routes implemented in v0.1. The API is internal — it
backs the Next.js UI and the Telegram bot — and is not a public, versioned,
stable contract.

## Authentication and identity

- **Add-on mode** — identity comes from Home Assistant Ingress headers
  (`X-Remote-User-Id`, `X-Remote-User-Name`, `X-Remote-User-Display-Name`).
  The header user is mapped to a role via the `user_roles` table.
- **Standalone mode** — the app issues and validates its own JWTs and presents
  a login UI.

Every non-health route is **role-gated**. The roles, lowest to highest:

| Role | Capabilities |
|---|---|
| `operator` | Read-only — view approvals, deviations, rollout state. |
| `cultivator` | + draft recipe revisions, approve/reject SFW proposals. |
| `qap` | + advance rollout, acknowledge deviations, view and export the audit log. |
| `admin` | + manage users, role assignments, the Telegram map, equipment maps, guardrail bounds. |

A route's gate is its **minimum** role — a higher role always satisfies it.

## Health

| Method | Path | Role | Description |
|---|---|---|---|
| `GET` | `/healthz` | none | Liveness. Process is up; touches no dependency. |
| `GET` | `/readyz` | none | Readiness. Touches Postgres; returns `status`, `mode`, `db`. |

## Audit

All audit routes require **`qap`** or higher. The audit log is the
tamper-evident HMAC chain — see [The audit chain](../concepts/audit-chain.md).

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/audit/events` | Paginated audit log, newest first. Query: `limit` (1–500), `offset`, `event_type`, `room_id`. |
| `GET` | `/api/audit/verify` | Verify the HMAC chain. Recomputes every row's HMAC and checks linkage. Query: `start_id`, `end_id`. Returns `ok`, `rows_checked`, `first_bad_id`, `message`. |
| `GET` | `/api/audit/export` | Export the audit log over a time range. Query: `format` (`csv` \| `pdf`), `start`, `end`. **`csv` is fully supported; `pdf` returns HTTP 501** (tracked work). The export writes its own `audit_export` audit row. |

## Approvals (SFW)

The UI side of the supervised-approval workflow. The Telegram bot drives the
same `core/sfw` lifecycle.

| Method | Path | Role | Description |
|---|---|---|---|
| `GET` | `/api/approvals` | `operator` | List pending approvals. Query: `include_decided`, `room_id`. |
| `GET` | `/api/approvals/{id}` | `operator` | One approval with its proposal and originating snapshot. |
| `POST` | `/api/approvals/{id}/approve` | `cultivator` | Approve a proposal. It is re-checked, then applied as overlays + a command batch. Optional body: `{ "notes": "…" }`. |
| `POST` | `/api/approvals/{id}/reject` | `cultivator` | Reject a proposal — no overlay, no command, one audit row. Optional body: `{ "notes": "…" }`. |

A decided approval that is no longer `open` returns HTTP 409.

## Rollout and deviations

The QAP-facing surface for the graduated rollout and the formal-deviation
lifecycle. See [AI control modes](../concepts/ai-control-modes.md) and
[Deviations](../operations/deviations.md).

| Method | Path | Role | Description |
|---|---|---|---|
| `GET` | `/api/rollout` | `operator` | Every room's rollout state + advance-gate status, plus the rollout ladder. |
| `POST` | `/api/rollout/{room_id}/advance` | `qap` | Advance a room one rollout stage. Blocked (HTTP 409) by any unresolved formal deviation, or if already at the final stage. Optional body: `{ "notes": "…" }`. |
| `GET` | `/api/deviations` | `operator` | List open (unacknowledged) formal deviations. Query: `room_id`. |
| `POST` | `/api/deviations/{event_id}/ack` | `qap` | Acknowledge a formal deviation — clears the rollout block, writes a `deviation_acknowledged` audit row. Optional body: `{ "notes": "…" }`. |

## Admin

User, role and Telegram-map management. Every route requires **`admin`**. Role
grants and Telegram-map changes write `user_role_changed` audit events — an
unaudited RBAC change would itself be a deviation.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/admin/users` | List every user with their roles. |
| `POST` | `/api/admin/users` | Create or update a user. The `roles` list fully replaces the user's current roles; each add/remove writes an audit event. |
| `GET` | `/api/admin/telegram-map` | List every Telegram chat-id → HA-user mapping. |
| `POST` | `/api/admin/telegram-map` | Create or update a mapping. The mapped user must already exist (404 otherwise). |

## Knowledge

| Method | Path | Role | Description |
|---|---|---|---|
| `GET` | `/api/knowledge/cultivation` | any authenticated | The full cultivation knowledge base as Markdown — the same source the supervisor's system prompt is built from. Used by the AI (and a reviewer) to resolve an `AP-*` / `EC-*` / `SAT-*` reason code to its full definition. |

## Not yet implemented

The v0.1 API does not yet expose a dedicated recipe-management surface — recipe
revisions are seeded by the migration tool and managed through the planner UI
(Phase 10). PDF audit export is stubbed at HTTP 501. These are tracked work, not
omissions of intent.
