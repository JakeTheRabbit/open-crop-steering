# AI control modes

The AI in Open Crop Steering operates in one of three modes per parameter, and
it earns its way up a graduated rollout ladder under QAP control. This page
explains both.

## The three modes

The modes are labelled plainly in the UI. The names in the audit log are the
formal ones.

| UI label | Audit name | What the AI does |
|---|---|---|
| **Report** | `report_only` | Observes and explains. Logs what it would do. Applies nothing. |
| **SFW** | `supervised_approval` | Proposes a change. A human must approve it (UI or Telegram) before it applies. |
| **YOLO** | `bounded_auto_adjust` | Applies a change automatically — *if* it passes the deterministic guardrail validator. |

"SFW" is "supervised future work" — the AI's proposal sits in a queue until a
human decides. "YOLO" still goes through every guardrail; the name is about who
clicks the button, not about removing the safety net.

### Report mode

The supervisor builds a snapshot, runs the deterministic monitoring, and (if
something is out of tolerance) asks the LLM for an assessment. The LLM's output
is logged to `llm_call_log` and surfaced as an alert per its severity. **No
overlay is created, no command is queued.** Report mode is how the facility
gathers a week-plus of evidence that the AI's judgement is sound before letting
it touch anything.

### SFW mode (supervised approval)

The AI produces a proposal. It becomes a `pending_approval` row with a 90-minute
TTL. A human approves or rejects it — from the UI (`POST
/api/approvals/{id}/approve`) or from a Telegram inline keyboard. Both paths run
the same logic and produce the same audit row.

On approval the proposal is **re-checked** — room state may have shifted since
the AI proposed — and only applied if it still passes. A pending row survives an
add-on restart (it is in Postgres), so tapping Approve still works after a
restart.

### YOLO mode (bounded auto-adjust)

The AI's proposal goes straight into the **guardrail validator**
(`core/guardrails.py`). The validator is deterministic — same input, same
verdict — and decides, in this order:

1. **Class E** → reject. The AI never writes admin/control state.
2. **Saturation + anti-pattern** → reject, cite both ids (e.g. `SAT-AC` +
   `AP-02`), start a cool-down.
3. **Anti-pattern** (uncoupled) → reject, cool-down.
4. **Coupling-rule violation** → reject, cite the `EC-*` id, cool-down.
5. **Outside absolute bounds** → reject (`guardrail_rejection`), cool-down.
6. **In a cool-down or no-touch window** → defer (not a rejection — no cool-down
   change).
7. **Would breach the rolling 24h / 7d cumulative cap** → reject, cool-down.
8. **In the boring-safe action set**, in bounds, within caps, clean → **apply**
   (`controlled_adjustment`).
9. **Not in the action set** but otherwise clean and within the **halved**
   budget → apply with `novel_proposal: true`.

A `controlled_adjustment` is the normal, expected outcome. It is **not** a
deviation — it is the system working. See
[Operations → Anti-patterns](../operations/anti_patterns.md) and
[Coupling rules](../operations/coupling_rules.md) for the rule sets, and
[Saturation indicators](../operations/saturation_indicators.md) for the
saturation predicates.

## Parameter risk classes

Every cultivation parameter has a risk class. The class determines the *highest*
mode the AI can ever reach for it.

| Class | Parameters | AI ceiling |
|---|---|---|
| **A** | Read / report-only params | Always `report_only`. |
| **B** | Environment — temp, RH, CO₂, VPD, PPFD, photoperiod, air velocity | Up to `bounded_auto_adjust`. |
| **C** | Irrigation — shot size, VWC/EC targets, dryback, drain %, frequency | Up to `bounded_auto_adjust`. |
| **D** | Nutrient / chemistry — tank pH/EC, leaf temp | Up to `bounded_auto_adjust`, with **tighter** caps and a **2-hour** cool-down. |
| **E** | Admin / control state — rollout stage, cycle day, no-touch windows, guardrail bounds, role mappings | **Never written by the AI.** The validator refuses Class E outright. |

A parameter the system does not recognise defaults to Class A — failing safe.

## The rollout ladder

A class does not jump to `bounded_auto_adjust` on day one. The facility advances
through a graduated rollout. Each rung loosens what the AI may do:

| Stage | What it enables |
|---|---|
| `report_only` (stage 0) | Report only — AI observes, never proposes to apply. |
| `stage_1` | Report only — extended evidence period. |
| `stage_2` | **Class B** bounded auto-adjust enabled. |
| `stage_3` | Class B auto-adjust; **Class C** supervised approval. |
| `stage_4` | **Class C** bounded auto-adjust enabled. |
| `stage_5` | Class C auto-adjust; **Class D** supervised approval. |
| `stage_6` | **Class D** bounded auto-adjust enabled (tighter caps). |

The effective mode for a parameter is `min(class ceiling, what the current
rollout stage allows)`. One stage *before* a class reaches
`bounded_auto_adjust`, it sits at `supervised_approval` (SFW).

Every room starts at stage 0. In practice the AI is report-only for everything
until a QAP advances the room.

## Advancing — the gates

A QAP advances a room one rung at a time (`POST
/api/rollout/{room}/advance`). The advance is **blocked** unless the gates pass.
The hard, enforced gate:

- **No unresolved formal deviation** for the room. Any open deviation blocks
  every advance until a QAP acknowledges it.

The advance check also reports the advisory gates the facility's rollout SOP
uses:

- At least five days at the current stage.
- Fewer than three SFW rejections in the trailing 7 days.
- No audit-recovery events.
- A QAP-recorded approval — which, for the API, *is* the QAP making the advance
  call; their id becomes the audit `actor_id`.

Every advance writes a `rollout_advanced` audit row. The facility's own rollout
gates and the full 8-week schedule are in the private validation pack
(`validation/14_sop_grow_control.md`).

## What the AI can never do

No matter the mode or the rollout stage:

- It cannot edit a recipe — recipes are immutable
  ([Recipes and overlays](recipes-and-overlays.md)).
- It cannot write Class E — roles, rollout stage, cycle day, no-touch windows,
  guardrail bounds.
- It cannot exceed a parameter's absolute bounds, in one step or by accumulating
  many small steps (the cumulative caps stop that).
- It cannot reach a Home Assistant service directly — every change goes through
  the command queue with readback.
