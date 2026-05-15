# Open Crop Steering

!!! warning "Reference / Unvalidated"
    This is a **reference implementation**. It is not validated software you can
    drop into a regulated cultivation operation and forget. The public repo is
    where the engineering lives; the version a facility actually runs is pinned,
    controlled and validated by that facility's QAP. See
    [Compliance](compliance/overview.md) for what "validated" means here.

A Home Assistant add-on (with a standalone Docker fallback) that turns
cultivation recipes into immutable, versioned plans; layers AI-driven runtime
overlays bounded by hard guardrails; and produces tamper-evident audit records
suitable for a regulated medicinal cannabis facility.

**Status:** v0.1 in development. Built for a licensed New Zealand medicinal
cannabis cultivation facility operating under
[GACP](https://www.legislation.govt.nz/regulation/public/2019/0327/latest/whole.html)
(Good Agricultural and Collection Practice).

---

## In normal English (skip if you read code)

It is an app that runs inside Home Assistant. You write down your cultivation
plan once — temperature, humidity, CO₂, lights, irrigation, nutrients for every
day of an 84-day cycle. The app applies the right day's settings every morning
when the lights come on, watches what is actually happening with your plants,
and either tells you when something is drifting, asks for permission to fix it,
or just fixes it — depending on how much you trust it.

### What the plants get

- The recipe you wrote down, applied at the right time every day. Same as if you
  stood there with a clipboard, but without the clipboard.
- Adjustments when something is wrong — temperature drifting, AC working too
  hard, nutrient dosing off. The app catches it before it becomes a yield
  problem.
- Protection from sudden changes — the app physically cannot make wild swings,
  no matter what an AI suggests.

### What the operator gets

- One screen showing every room's current state, the active recipe, and what the
  AI is thinking.
- A planner where you draw out the 12-week recipe like a spreadsheet.
- Telegram messages when something needs your attention — and only then.
- A clear approve / reject flow when the AI wants to change something.

### What the QAP / compliance person gets

- Every change ever made — by you, by the AI, by a scheduled run — logged with
  who did it, when, what changed, and why.
- The log cannot be edited or deleted. It is cryptographically chained, so
  tampering is detectable.
- One-click export for inspectors, with proof the records were not altered.
- A formal **deviation** workflow that blocks the AI from getting more autonomy
  until you have reviewed the issue.
- A 14-document GACP validation pack that lives in the private facility repo.

### The AI safety story in plain terms

The AI is on a leash, not in charge.

- **The AI can never edit your recipe.** Your written plan is locked. The AI can
  only add temporary "nudges" that expire automatically.
- **The AI can never change its own rules.** It cannot loosen its own limits,
  change who has permission to do what, or extend the hours it is allowed to
  operate.
- **The AI earns autonomy over 8 weeks** — it starts at "report only" and only
  progresses if there are no problems and the QAP approves the next step.
- **If the AI tries something silly** — like turning the AC down when the AC is
  already maxed out — the app rejects it before any change happens.
- **Equipment knows its limits.** If your dehumidifier is already running
  flat-out and humidity is not dropping, the app will not lower the humidity
  target — it tells you to check the dehumidifier instead.

### What this is NOT

- Not a replacement for your cultivator. It runs your plan; you write the plan.
- Not an autonomous robot grower.
- Not a substitute for SOPs or QAP oversight. It is the **tool** your SOP uses.
- Not validated software you can drop in and forget.
- Not for sale, not a SaaS, not a cloud product. It runs on your hardware, on
  your network, with your data.

---

## Where to go next

| If you want to… | Read |
|---|---|
| Install it in Home Assistant | [Installation → HA add-on](installation/addon.md) |
| Run it outside HA | [Installation → Standalone Docker](installation/standalone.md) |
| Understand how it is built | [Concepts → Architecture](concepts/architecture.md) |
| Understand recipe immutability | [Concepts → Recipes and overlays](concepts/recipes-and-overlays.md) |
| Understand the audit log | [Concepts → The audit chain](concepts/audit-chain.md) |
| Understand AI autonomy | [Concepts → AI control modes](concepts/ai-control-modes.md) |
| See the cultivation logic | [Concepts → Cultivation knowledge](concepts/cultivation_knowledge.md) |
| See what the AI is blocked from doing | [Operations → Anti-patterns](operations/anti_patterns.md) |
| Understand the compliance posture | [Compliance → Overview](compliance/overview.md) |

## License

MIT. This is a reference implementation. It builds on top of (but does not
replace or fork) [HA-Irrigation-Strategy](https://github.com/JakeTheRabbit/HA-Irrigation-Strategy)
("Crop Steering") for irrigation shot mechanics.
