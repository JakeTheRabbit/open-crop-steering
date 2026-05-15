# Alerts

Open Crop Steering tells you when something needs attention — and is built to do
*only* that. An alerting system that floods you gets muted, and a muted system
misses the one alert that mattered. So alerts are tiered by severity, and each
tier behaves differently.

## The three severity tiers

The `alerts` worker routes every operational event by its severity. There are
three Telegram notify-service slots:

| Tier | Behaviour | Examples |
|---|---|---|
| **Critical** | Sent immediately. **Bypasses Do-Not-Disturb.** | `SAT-IRRIG` (irrigation not delivering), `SAT-DEW` (active condensation), a lights-state drift over two minutes, a formal deviation, an audit-chain failure. |
| **Warning** | **Digested** — collected and sent once a day at a time you choose. | Equipment saturation (AC / dehumidifier / CO₂ / fans), a stale sensor, a degraded LLM, an out-of-tolerance drift in Report mode. |
| **Info** | **Dashboard only.** No push. | Nominal status changes, a rollout-stage advance, a `controlled_adjustment` applied within guardrails. |

The split maps to the event taxonomy in the audit log — see
[The audit chain](../concepts/audit-chain.md) and
[Deviations](deviations.md).

## Why the tiers exist

- **Critical bypasses DND** because an unresponsive irrigation valve at 2 a.m.
  is a crop emergency. It should wake someone.
- **Warnings are digested** because a saturated AC during a heatwave is worth
  knowing about — but as one daily summary line, not forty pushes. Digesting
  keeps the channel credible.
- **Info stays on the dashboard** because a `controlled_adjustment` going
  through cleanly is the system *working*. It is auditable, but it is not news.

## Per-room muting

Each room can be muted independently (`room_runtime.muted`). If one room is
mid-renovation and producing noise, mute that room — the others keep alerting.
A muted room still records every event to the audit log and the event log; it
just does not push.

When a room is `IMPAIRED`, the supervisor also suppresses the LLM tick for it —
a degraded room does not generate a stream of AI chatter on top of the
saturation warning that already told you about it.

## Configuring the notify services

The three slots are set in the add-on options (or `options.json` in the facility
config):

```json
"notify": {
  "critical": "notify.grow_telegram_critical",
  "warning":  "notify.grow_telegram_warning",
  "info":     null
}
```

Point `critical` and `warning` at Home Assistant notify services. `info` is
normally `null` — info events live on the dashboard.

For Telegram, the bot and the chat ids are configured at setup. Telegram is also
the channel for SFW approvals — see [AI control modes](../concepts/ai-control-modes.md).
Approval authority over Telegram is gated by the `telegram_user_map`: a callback
from a chat id not mapped to a known HA user with the right role is refused and
audited.

## What an alert does NOT do

An alert is a notification. It never *applies* anything. A warning that the AC
is saturated does not change a setpoint. A critical that irrigation is
unresponsive blocks further auto-shots (that is the saturation predicate's
doing, recorded in the audit log) — but the alert itself is just the message
that tells you.
