# Deviations

In a regulated facility, a **deviation** is a formal thing — a defined event
that gets recorded, notified, reviewed and signed off. Open Crop Steering
deliberately keeps the bar for "formal deviation" high. Most things that happen
are *not* deviations.

## The event taxonomy

Every operational event has a type. Only one type is a formal deviation:

| Event type | Is it a deviation? |
|---|---|
| `info_event` | No — nominal. |
| `controlled_adjustment` | **No** — a bounded auto-adjust that applied within guardrails. This is the system working. |
| `system_warning` | No — a sensor stale, equipment saturated, LLM degraded. Worth knowing; not a deviation. |
| `guardrail_rejection` | **No, by itself** — a proposal was rejected. A rejection is the guardrail *succeeding*. Only aggregates to a deviation if a pattern is detected. |
| `formal_deviation` | **Yes.** |
| `critical_incident` | Yes — a formal deviation needing immediate notification + a rollout block. |

The distinction matters. If every rejected AI proposal were logged as a
deviation, the deviation log would be noise and a real deviation would hide in
it. A `controlled_adjustment` and a `guardrail_rejection` are both **normal,
healthy** outcomes.

## What counts as a formal deviation

These — and only these — become a `formal_deviation` event:

1. **An applied value outside approved guardrails.** This should only ever
   happen via a bug.
2. **Stale or invalid sensor data used in an automated control decision** — the
   executor failed to skip when it should have.
3. **A missed irrigation or control event** exceeding the defined threshold
   (a delay over 30 minutes).
4. **An unauthorised role / config / recipe-revision change** — an RBAC bypass
   was detected.
5. **An audit-chain verification failure on export.**
6. **A backup or restore failure** — the cold backup or a restore test fails.
7. **Equipment saturation with crop or process impact**, sustained beyond the
   defined threshold.
8. **Loss of required records** — any data-integrity failure.
9. **Three or more guardrail rejections citing the same `AP-*` id for one room
   within an hour** — a pattern indicating AI drift or runaway.

The full criteria are also written into the facility's validation pack
(`validation/12_deviation_procedure.md`).

## The repeated-rejection pattern

Item 9 is worth calling out. A single rejected proposal is fine. But if the AI
proposes the *same* known-bad action — same anti-pattern — three times for the
same room inside an hour, that is no longer routine. It suggests the model has
drifted or is misreading its context, and it is escalated to a formal deviation
by `detect_rejection_pattern` (`core/guardrails.py`).

The escalation is idempotent: if an unacknowledged deviation for that room +
anti-pattern already exists, no duplicate is written — the one open deviation
already covers the run.

## What happens when a deviation fires

1. A `formal_deviation` event is written to `event_log`, and a matching
   `formal_deviation` row is written to the tamper-evident `audit_event` chain.
2. The QAP is notified — a critical Telegram message (it bypasses
   Do-Not-Disturb) and a dashboard banner.
3. **Rollout-stage advancement for that room is blocked.** While any
   unresolved formal deviation is open for a room, a QAP cannot advance its
   rollout stage. The AI does not earn more autonomy until the issue is
   reviewed. This is the hard, enforced rollout gate (REQ-010).

## Acknowledging a deviation

A QAP (or higher) acknowledges a deviation:

- `GET /api/deviations` — lists the open (unacknowledged) deviations.
- `POST /api/deviations/{event_id}/ack` — acknowledges one, with an optional
  note (e.g. the corrective action taken).

Acknowledgement:

- stamps `acknowledged_by` / `acknowledged_at` on the event;
- writes a `deviation_acknowledged` audit row — the QAP's action is itself part
  of the chain;
- **clears the rollout block** — once no unresolved deviation remains for the
  room, a QAP can advance its rollout stage again.

## Deviations and the rollout

Deviations are the brake on the [graduated rollout](../concepts/ai-control-modes.md).
The rollout only moves forward; a deviation stops it until a human has looked.
That is the intended safety dynamic: the AI's autonomy is contingent on a clean
operational record, and a deviation is what records that the record is not
clean.
