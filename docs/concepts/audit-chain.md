# The audit chain

Every state-changing action in Open Crop Steering produces a row in the
`audit_event` table. The table is **tamper-evident**: each row is HMAC-signed
and cryptographically chained to the row before it, and a daily seal of the
chain head is exported off-box.

!!! note "It is tamper-evident, not WORM"
    This is deliberately *not* called "WORM" (write-once-read-many) storage.
    The audit table is append-only and tampering is **detectable** — but the
    guarantee is detection, not physical impossibility. An attacker with
    Postgres superuser could in principle bypass the triggers; the chain
    verification and the off-box seal are what catch that. Honest framing
    matters for a compliance document.

## What gets audited

Every audit row carries: when, the event type, the actor and their role, the
room, a human summary, structured `params`, `reason_codes`, foreign-key
references to the related recipe revision / overlay / command / snapshot / LLM
call, the signing `key_id`, the previous row's hash, and this row's HMAC.

The event types (the `audit_event_type` enum):

| Event type | Written when |
|---|---|
| `info_event` | Nominal status change, rollout advance, etc. |
| `controlled_adjustment` | A bounded auto-adjust applied within guardrails. **Not** a deviation. |
| `system_warning` | Sensor stale, equipment saturated, LLM degraded. |
| `guardrail_rejection` | A proposal was rejected. Not by itself a deviation. |
| `formal_deviation` | A defined deviation event (see [Deviations](../operations/deviations.md)). |
| `critical_incident` | A formal deviation needing immediate notification + a rollout block. |
| `recipe_revision_created` / `recipe_revision_approved` | Recipe lifecycle. |
| `runtime_adjustment_added` / `runtime_adjustment_reverted` | Overlay lifecycle. |
| `user_role_changed` | An RBAC change — including Telegram-map changes. |
| `rollout_advanced` | A QAP advanced a room's rollout stage. |
| `deviation_acknowledged` | A QAP acknowledged a formal deviation. |
| `audit_export` | The audit log was exported. The export is itself audited. |
| `hmac_key_rotated` | The audit signing key was rotated. |

## How the chain works

Every `audit_event` INSERT runs through a Postgres `BEFORE INSERT` trigger
(`audit_event_hmac_chain`). The trigger:

1. Takes a transaction-scoped advisory lock so two concurrent inserts cannot
   both read the same "previous head" and fork the chain.
2. Reads the most recent row's `hmac` and stores it as this row's
   `prev_event_hash`. (The genesis row chains from 32 zero bytes.)
3. Builds a fixed canonical byte payload from the row —
   `prev_event_hash` followed by the `key_id`, the timestamp, the actor, the
   event type, the room, the `params` and the `reason_codes`, each separated by
   an ASCII unit separator.
4. Computes `HMAC-SHA256(payload, key)` where `key` is the HMAC key selected by
   the row's `key_id`, and stores it as `hmac`.

Each row therefore signs the row before it. Change any field of any historical
row and its HMAC no longer matches; change its HMAC and the *next* row's
`prev_event_hash` no longer matches. Either way the break is detectable, and
the verifier reports the first bad row id.

UPDATE and DELETE on `audit_event` are blocked outright by two further triggers
(`trg_audit_event_no_update`, `trg_audit_event_no_delete`).

## Key rotation

The signing key has a `key_id`. Multiple keys can be configured
(`HMAC_KEY_1`, `HMAC_KEY_2`, …); `HMAC_KEY_ID_CURRENT` selects which one signs
*new* rows. When a key is rotated:

- A new `key_id` becomes current. New rows sign with it.
- **Old rows are never re-signed.** They keep their original HMAC and their
  original `key_id`.
- The verifier walks the key history — each row is verified with the key its
  own `key_id` names.

A rotation writes an `hmac_key_rotated` audit event.

## The daily seal

Once a day the `seal` worker computes a `daily_seal` row. It captures that
day's chain span — the first and last `audit_event` ids — and the **chain
head** (the last row's HMAC), then signs a *seal HMAC* over
`(date, first_id, last_id, last_event_hmac)` with the current key.

The seal worker then writes a gzipped export —
`/data/audit-seal-exports/<date>.sealed.gz` — bundling that day's audit rows
with the seal record, and is intended to ship it to a configured off-box target
(S3 / B2 / equivalent).

The seal gives an independent, dated anchor: if anyone alters history *after*
the seal was taken, the recomputed chain head no longer matches the sealed
value held off-box.

!!! warning "Encryption of the seal export — current state"
    The plan calls for the off-box seal export to be **encrypted** with a key
    distinct from the HMAC key. As of v0.1 the seal worker writes the export
    **gzip only, unencrypted**. The export-encryption envelope and the off-box
    copy are tracked work, not yet shipped. Until then, at-rest protection of
    `/data` relies on Home Assistant backup encryption. This is stated honestly
    in the validation pack (`validation/10_audit_integrity_test.md`).

## Verifying the chain

A QAP (or higher) can verify the chain at any time:

- `GET /api/audit/verify` — recomputes every row's HMAC server-side using the
  exact canonical payload the trigger uses, and checks every `prev_event_hash`
  link. It returns `ok`, the number of rows checked, and the id of the first
  bad row if any.
- A chain verification failure on export is a **formal deviation**.

## Exporting for an inspector

- `GET /api/audit/export?format=csv` — streams a real CSV of the audit log over
  a date range. The export action itself writes an `audit_export` audit row.
- `GET /api/audit/export?format=pdf` — **currently returns HTTP 501.** A styled
  PDF inspector report (with the chain-validity stamp rendered) is tracked work
  and not yet implemented. CSV covers the machine-readable inspector need in the
  meantime. This gap is stated in the validation pack.

See [Compliance → Overview](../compliance/overview.md) for how the audit chain
fits the GACP verification posture.
