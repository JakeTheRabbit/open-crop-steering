# Recipes and overlays

The core data-model decision in Open Crop Steering: the recipe you write is
**immutable**, and every runtime correction is a separate, **expiring overlay**.
This is what makes AI control safe to allow at all.

## The recipe is the source of truth

A **recipe revision** is a day-by-day cultivation plan for one room — a value
(and an optional tolerance band) for each parameter, for each day of the cycle
(84 days for the cannabis 12-week preset).

Recipe revisions live in two tables:

- `recipe_revision` — the header: room, version, name, cycle length, status,
  who created it, who approved it.
- `recipe_revision_param` — the rows: one `(day_index, param_name, value,
  tolerance, unit)` per parameter per day.

### Revisions, not edits

You never *edit* a recipe. You create a **new revision**. The status lifecycle:

```
draft  →  pending_approval  →  approved  →  superseded
```

- A **cultivator** drafts a revision.
- A **QAP** approves it. Approval makes it the active recipe for the room.
- When a newer revision is approved, the previous one becomes `superseded` —
  but it is never deleted. The full revision history stays.

### Immutability is enforced by the database

Once a revision is `approved`, a Postgres trigger
(`recipe_revision_immutable_when_approved`) blocks any change to its
identity fields, and a second trigger
(`recipe_revision_param_block_when_approved`) blocks every UPDATE and DELETE on
its parameter rows. The only status transition allowed out of `approved` is to
`superseded`.

This is not application-layer politeness. An approved recipe is frozen at the
database level. **The AI cannot write to these tables at all** — and even a
human cannot alter an approved revision; they must supersede it with a new one.

## Overlays: how runtime corrections work

The recipe says "day 28, temp_day = 28.5 °C". Reality on day 28 might call for
a small, temporary nudge — the room is running warm, the AC has headroom, a
−0.5 °C correction would help. That correction is a **runtime adjustment**, or
overlay.

A `runtime_adjustment` row carries:

| Field | Meaning |
|---|---|
| `room_id`, `day_index`, `param_name` | What it adjusts. |
| `delta` | The signed correction (e.g. `-0.5`). |
| `source` | `ai_auto`, `ai_sfw`, `operator`, or `cultivator`. |
| `mode` | `report_only`, `supervised_approval`, `bounded_auto_adjust`. |
| `expires_at` | When it stops applying. **Always set.** Default: lights-off today. |
| `active` / `reverted_at` | An overlay can be reverted early. |
| `novel_proposal` | `true` if the AI's proposal was outside the boring-safe action set. |
| `reason_codes` | Why — including any `AP-*` / `EC-*` / `SAT-*` ids. |

Two properties matter:

1. **Overlays expire.** An overlay is a *temporary* correction, not a permanent
   recipe change. By default it lapses at lights-off the same day. The recipe is
   untouched; tomorrow starts from the recipe again.
2. **The AI writes overlays, never recipes.** When the AI is allowed to act, the
   only thing it can produce is a bounded, expiring overlay. It cannot reach the
   recipe tables.

## The effective target

What actually gets sent to Home Assistant is the **effective target**:

```
effective target  =  approved recipe value  +  Σ active, unexpired overlays
```

This is computed by the `effective_target` **materialized view**. The view
joins the highest-version approved revision for each room against its active,
unexpired overlays and sums the deltas. It is the single thing the executor
reads when it decides what value to write.

```
recipe_revision   (room F1, version 12, approved)
  day 28 temp_day = 28.5 °C
        +
runtime_adjustment (room F1, day 28, temp_day,
                    delta −0.5, source ai_auto, expires at lights-off)
        =
effective_target  (room F1, day 28, temp_day = 28.0 °C)
        ↓
command_queue → HA → readback → audit
```

## Why this design

- **The plan is auditable and stable.** What the recipe says is what a QAP
  signed. It does not drift.
- **Corrections are bounded and reversible by construction.** An overlay has a
  delta, a class-based cap, and an expiry. It cannot become a permanent change
  by accident.
- **AI autonomy is safe to grant incrementally.** Because the AI can only
  produce expiring overlays — never recipe edits, never admin state — handing it
  more autonomy never risks the canonical plan. See
  [AI control modes](ai-control-modes.md).
