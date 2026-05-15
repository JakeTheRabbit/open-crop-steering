# Compliance overview

This page explains the compliance posture of Open Crop Steering: what standard
it is built against, what "validated" means here, and where the validation
evidence lives.

!!! warning "The public repo is not validated software"
    What you are reading is the **reference implementation**. It is open source,
    MIT-licensed, and built honestly — but it is not validated software. A
    facility does not run the public `main` branch. It runs a **pinned,
    controlled fork** with its own validation evidence. Read this page before
    treating Open Crop Steering as a controller of record.

## The standard

The facility this software was built for is a licensed New Zealand medicinal
cannabis cultivator operating under the
[Misuse of Drugs (Medicinal Cannabis) Regulations 2019](https://www.legislation.govt.nz/regulation/public/2019/0327/latest/whole.html),
administered by Medsafe / the Medicinal Cannabis Agency.

For **cultivation activities**, the operative standard is **GACP** — Good
Agricultural and Collection Practice. Final-product processing is out of scope
and handed off downstream under EU-GMP.

## The verification posture

GACP is a lighter-weight standard than EU-GMP. Open Crop Steering is verified
with a **GACP-tier, risk-based verification package** — not a formal
IQ/OQ/PQ exercise, but a package that covers the equivalent ground:

- A system description and user requirements.
- A risk assessment.
- A requirements-traceability matrix linking each requirement to where it is
  implemented and how it is verified.
- A configuration specification.
- Installation verification (IQ-equivalent).
- Operational verification (OQ-equivalent) — a numbered, signable test protocol.
- An ongoing performance-monitoring plan.
- Backup / restore test evidence.
- Access-control test evidence.
- Audit-chain verification evidence.
- An AI-guardrail test protocol.
- A deviation-handling procedure.
- A change-control procedure.
- An operating SOP, including the manual-fallback procedure.

That is the **14-document validation pack**. It does not live in this public
repo — it lives in the facility's private repo, under `validation/`, where
each document targets a Medsafe inspector and the facility QAP, and every
change to it requires QAP review.

## What "validated" means here

A facility deployment is *validated* when:

- The 14-document validation pack is populated, current, and **signed by the
  QAP**.
- The operational-verification protocol has been executed, with evidence
  recorded per run.
- The installation, backup/restore, access-control, audit-integrity and
  AI-guardrail tests have passed against the **pinned version** the facility
  runs.
- The manual-fallback procedure has been drilled.
- The QAP has signed the rollout-advance approval.

"Validated" is a property of a specific pinned version running in a specific
facility with specific evidence — not a property of the software in the
abstract. The public repo cannot be "validated"; only a controlled deployment
of it can.

## How the software supports the posture

Several design choices exist specifically to make the verification posture
defensible:

- **Immutable recipes.** What a QAP approved is frozen at the database level —
  it cannot drift. See [Recipes and overlays](../concepts/recipes-and-overlays.md).
- **Tamper-evident audit.** Every state change is HMAC-chained and daily-sealed;
  a verifier can prove the record was not altered. See
  [The audit chain](../concepts/audit-chain.md).
- **Bounded AI.** The AI can only produce expiring overlays within hard
  guardrails; it can never touch recipes, admin state, or a Home Assistant
  service directly. See [AI control modes](../concepts/ai-control-modes.md).
- **Graduated rollout with QAP gates.** AI autonomy is earned, advance by
  advance, and a formal deviation stops it. See [Deviations](../operations/deviations.md).
- **A defined deviation taxonomy.** A bounded auto-adjust is not a deviation; a
  defined set of events is. The bar is set deliberately.
- **A tested manual fallback.** If the add-on is unavailable, the facility
  reverts to its prior Home Assistant automation and runs the cycle manually.

## Honest gaps

Two things the plan calls for are **not yet shipped** as of v0.1, and the
validation pack states this plainly rather than implying completeness:

- **Encrypted off-box seal export.** The daily audit seal is exported gzip-only,
  unencrypted. At-rest protection currently relies on Home Assistant backup
  encryption. Envelope encryption is tracked work.
- **Styled PDF audit export.** `GET /api/audit/export?format=pdf` returns HTTP
  501. CSV export is fully implemented; the styled PDF inspector report (with
  the chain-validity stamp rendered) is tracked work.

A validation package that overstates what the software does is worse than
useless to an inspector. These gaps are documented in
`validation/10_audit_integrity_test.md`.

## The two-track distribution

| | Public `open-crop-steering` | Private facility repo |
|---|---|---|
| Licence | MIT, open | Private, not for distribution |
| Contents | The reference implementation | A pinned version + facility config + the validation pack + the audit archive |
| Recipes | None — the 12-week preset is a generic example | The facility's actual approved recipes |
| Validation pack | Not present | The 14 documents, QAP-controlled |
| Change cadence | Normal open-source | Every pinned-version bump is an audited change |

The validation pack and the facility's recipes never appear in this public
repo. This page is the public-facing explanation of a package whose contents
are, correctly, private.
