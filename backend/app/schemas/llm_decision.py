"""Strict LLM output contract — ``ocs.llm_decision.v1``.

Plan locked decision #12: the supervisor's LLM must return a JSON object
matching this exact schema. Anything else — malformed JSON, a missing
field, an unexpected extra field, an out-of-range ``confidence`` — is
rejected by :func:`app.llm_client.parse_decision` and logged with the
matching :class:`~app.models.llm_call_log.LLMCallOutcome`. The model's
free text is never executable intent; only a row that validates against
:class:`LLMDecision` is.

Why each field exists:

* ``schema_version`` — pins the contract; a bumped version fails old
  parsers loudly instead of silently mis-reading.
* ``snapshot_id`` — the model echoes back the snapshot it reasoned
  about; the parser rejects a mismatch (stale / unknown), closing the
  state-shift race (plan risk #4).
* ``recommended_action_id`` — an ``action_id`` from the deterministic
  ``allowed_action_set``, or ``null`` if the model proposes nothing /
  only free-form changes.
* ``proposed_changes`` — free-form per-parameter proposals; may be
  empty. In report mode (Phase 7) nothing here is applied; Phase 9's
  validator decides in-set / novel / rejected.
* ``confidence`` — the model's own ``0.0-1.0`` confidence; the system
  prompt instructs "propose nothing below 0.5".
* ``reason_codes`` — machine-actionable ids the model cites: ``AP-*``
  (anti-patterns), ``EC-*`` (coupling rules), ``SAT-*`` (saturation
  indicators), or free-form diagnostic tags. **Must** be able to carry
  those ids — they are how a report is tied back to the knowledge base.
* ``human_summary`` — a plain-language explanation for the operator /
  QAP review.
* ``requires_human`` — the model flags that a human must look (low
  confidence, a question the knowledge base cannot answer, a safety
  concern).

Every model here sets ``extra="forbid"`` — an unexpected field is a
schema violation, not something to ignore. Tolerating extra fields would
let a drifting model smuggle un-reviewed intent past the contract.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: The one accepted schema version string for this contract.
LLM_DECISION_SCHEMA_VERSION = "ocs.llm_decision.v1"


class ProposedChange(BaseModel):
    """One free-form per-parameter change the model proposes.

    In report mode this is recorded but never applied. Phase 9's
    validator is what decides whether a change is in the allowed action
    set, a novel-but-bounded proposal, or an out-of-bounds rejection.

    Attributes:
        param_name: The setpoint the change targets.
        direction: ``increase`` / ``decrease`` / ``no_change``.
        delta: Signed change requested (``0.0`` for ``no_change``).
        unit: Optional unit string, for a units-mismatch cross-check.
        rationale: Optional short reason for this specific change.
    """

    model_config = ConfigDict(extra="forbid")

    param_name: str = Field(min_length=1, max_length=64)
    direction: Literal["increase", "decrease", "no_change"]
    delta: float
    unit: str | None = Field(default=None, max_length=32)
    rationale: str | None = Field(default=None, max_length=512)


class LLMDecision(BaseModel):
    """The strict, validated LLM decision (schema ``ocs.llm_decision.v1``).

    Constructing this model *is* the schema check — a payload that does
    not validate raises :class:`pydantic.ValidationError`, which
    :func:`app.llm_client.parse_decision` turns into a
    :class:`~app.models.llm_call_log.LLMCallOutcome.schema_invalid`
    outcome.

    Attributes:
        schema_version: Must equal :data:`LLM_DECISION_SCHEMA_VERSION`.
        snapshot_id: The ``SensorSnapshot.id`` the model reasoned about;
            the parser rejects a mismatch against the snapshot actually
            sent.
        assessment: The model's short verdict on the room's state.
        recommended_action_id: An ``action_id`` from the allowed action
            set, or ``None``.
        proposed_changes: Free-form proposed changes; may be empty.
        confidence: Model self-confidence in ``[0.0, 1.0]``.
        reason_codes: Cited ``AP-`` / ``EC-`` / ``SAT-`` ids and/or
            free-form diagnostic tags.
        human_summary: Plain-language explanation for human review.
        requires_human: ``True`` if the model wants a human to look.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["ocs.llm_decision.v1"]
    snapshot_id: int = Field(ge=1)
    assessment: str = Field(min_length=1, max_length=2048)
    recommended_action_id: str | None = Field(default=None, max_length=128)
    proposed_changes: list[ProposedChange] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list)
    human_summary: str = Field(min_length=1, max_length=4096)
    requires_human: bool = False
