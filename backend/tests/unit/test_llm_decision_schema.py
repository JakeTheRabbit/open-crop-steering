"""Unit tests for the strict LLM decision contract.

Covers :class:`app.schemas.llm_decision.LLMDecision` validation and
:func:`app.llm_client.parse_decision`: a well-formed payload parses to a
:class:`LLMCallOutcome.parsed`; malformed JSON, an extra field, a bad
``confidence``, a missing field, a stale / unknown ``snapshot_id``, a
param outside the allowed set, and an unknown room each reject with the
correct :class:`LLMCallOutcome`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from app.llm_client import parse_decision
from app.models.llm_call_log import LLMCallOutcome
from app.schemas.llm_decision import LLM_DECISION_SCHEMA_VERSION, LLMDecision

pytestmark = pytest.mark.unit


def _valid_payload(snapshot_id: int = 1, **over: Any) -> dict[str, Any]:
    """Build a minimal valid ``ocs.llm_decision.v1`` payload."""
    base: dict[str, Any] = {
        "schema_version": LLM_DECISION_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "assessment": "Room is drifting warm; AC has headroom.",
        "recommended_action_id": None,
        "proposed_changes": [],
        "confidence": 0.8,
        "reason_codes": [],
        "human_summary": "Temperature is a little high but recoverable.",
        "requires_human": False,
    }
    base.update(over)
    return base


class TestLLMDecisionModel:
    def test_minimal_valid_payload_parses(self) -> None:
        decision = LLMDecision.model_validate(_valid_payload())
        assert decision.snapshot_id == 1
        assert decision.confidence == 0.8
        assert decision.proposed_changes == []

    def test_proposed_changes_parse(self) -> None:
        decision = LLMDecision.model_validate(
            _valid_payload(
                proposed_changes=[
                    {
                        "param_name": "temp_day",
                        "direction": "decrease",
                        "delta": -0.2,
                        "unit": "C",
                        "rationale": "small nudge",
                    }
                ]
            )
        )
        assert len(decision.proposed_changes) == 1
        assert decision.proposed_changes[0].param_name == "temp_day"

    def test_reason_codes_carry_ap_ec_sat_ids(self) -> None:
        decision = LLMDecision.model_validate(
            _valid_payload(reason_codes=["SAT-AC", "AP-02", "EC-001"])
        )
        assert decision.reason_codes == ["SAT-AC", "AP-02", "EC-001"]

    def test_extra_top_level_field_rejected(self) -> None:
        with pytest.raises(ValueError, match="Extra inputs"):
            LLMDecision.model_validate(_valid_payload(sneaky="payload"))

    def test_extra_field_in_proposed_change_rejected(self) -> None:
        with pytest.raises(ValueError, match="Extra inputs"):
            LLMDecision.model_validate(
                _valid_payload(
                    proposed_changes=[
                        {
                            "param_name": "temp_day",
                            "direction": "decrease",
                            "delta": -0.2,
                            "evil": True,
                        }
                    ]
                )
            )

    def test_bad_confidence_above_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="less than or equal to 1"):
            LLMDecision.model_validate(_valid_payload(confidence=1.5))

    def test_bad_confidence_below_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="greater than or equal to 0"):
            LLMDecision.model_validate(_valid_payload(confidence=-0.1))

    def test_wrong_schema_version_rejected(self) -> None:
        with pytest.raises(ValueError, match="schema_version"):
            LLMDecision.model_validate(
                _valid_payload(schema_version="ocs.llm_decision.v2")
            )

    def test_missing_required_field_rejected(self) -> None:
        payload = _valid_payload()
        del payload["human_summary"]
        with pytest.raises(ValueError, match="human_summary"):
            LLMDecision.model_validate(payload)


class TestParseDecisionSuccess:
    def test_valid_json_parses_to_parsed_outcome(self) -> None:
        result = parse_decision(
            json.dumps(_valid_payload(snapshot_id=42)),
            expected_snapshot_id=42,
        )
        assert result.outcome is LLMCallOutcome.parsed
        assert result.ok
        assert result.decision is not None
        assert result.decision.snapshot_id == 42
        assert result.errors == []

    def test_recommended_action_in_allowed_set_parses(self) -> None:
        result = parse_decision(
            json.dumps(
                _valid_payload(snapshot_id=1, recommended_action_id="act-temp-up-1")
            ),
            expected_snapshot_id=1,
            allowed_action_ids={"act-temp-up-1", "act-temp-down-2"},
        )
        assert result.outcome is LLMCallOutcome.parsed

    def test_proposed_param_in_allowed_set_parses(self) -> None:
        result = parse_decision(
            json.dumps(
                _valid_payload(
                    snapshot_id=1,
                    proposed_changes=[
                        {
                            "param_name": "temp_day",
                            "direction": "decrease",
                            "delta": -0.2,
                        }
                    ],
                )
            ),
            expected_snapshot_id=1,
            allowed_params={"temp_day", "rh_day"},
        )
        assert result.outcome is LLMCallOutcome.parsed


class TestParseDecisionRejection:
    def test_empty_response_is_schema_invalid(self) -> None:
        result = parse_decision("", expected_snapshot_id=1)
        assert result.outcome is LLMCallOutcome.schema_invalid
        assert not result.ok

    def test_none_response_is_schema_invalid(self) -> None:
        result = parse_decision(None, expected_snapshot_id=1)
        assert result.outcome is LLMCallOutcome.schema_invalid

    def test_malformed_json_is_schema_invalid(self) -> None:
        result = parse_decision(
            '{"schema_version": "ocs.llm_decision.v1", broken',
            expected_snapshot_id=1,
        )
        assert result.outcome is LLMCallOutcome.schema_invalid
        assert any("malformed JSON" in e for e in result.errors)

    def test_json_array_is_schema_invalid(self) -> None:
        result = parse_decision("[1, 2, 3]", expected_snapshot_id=1)
        assert result.outcome is LLMCallOutcome.schema_invalid
        assert any("expected a JSON object" in e for e in result.errors)

    def test_extra_field_is_schema_invalid(self) -> None:
        result = parse_decision(
            json.dumps(_valid_payload(sneaky="x")),
            expected_snapshot_id=1,
        )
        assert result.outcome is LLMCallOutcome.schema_invalid

    def test_bad_confidence_is_schema_invalid(self) -> None:
        result = parse_decision(
            json.dumps(_valid_payload(confidence=9.9)),
            expected_snapshot_id=1,
        )
        assert result.outcome is LLMCallOutcome.schema_invalid
        assert any("confidence" in e for e in result.errors)

    def test_stale_snapshot_id_known_is_snapshot_stale(self) -> None:
        # The model echoed an *older but real* snapshot id.
        result = parse_decision(
            json.dumps(_valid_payload(snapshot_id=7)),
            expected_snapshot_id=9,
            known_snapshot_ids={7, 8, 9},
        )
        assert result.outcome is LLMCallOutcome.snapshot_stale
        assert not result.ok

    def test_unknown_snapshot_id_is_snapshot_unknown(self) -> None:
        # The model echoed an id that does not exist at all.
        result = parse_decision(
            json.dumps(_valid_payload(snapshot_id=999)),
            expected_snapshot_id=9,
            known_snapshot_ids={7, 8, 9},
        )
        assert result.outcome is LLMCallOutcome.snapshot_unknown

    def test_snapshot_mismatch_without_known_ids_is_unknown(self) -> None:
        result = parse_decision(
            json.dumps(_valid_payload(snapshot_id=5)),
            expected_snapshot_id=9,
        )
        assert result.outcome is LLMCallOutcome.snapshot_unknown

    def test_action_id_outside_allowed_set_is_schema_invalid(self) -> None:
        result = parse_decision(
            json.dumps(
                _valid_payload(snapshot_id=1, recommended_action_id="act-evil")
            ),
            expected_snapshot_id=1,
            allowed_action_ids={"act-temp-up-1"},
        )
        assert result.outcome is LLMCallOutcome.schema_invalid
        assert any("allowed action set" in e for e in result.errors)

    def test_proposed_param_outside_allowed_set_is_schema_invalid(self) -> None:
        result = parse_decision(
            json.dumps(
                _valid_payload(
                    snapshot_id=1,
                    proposed_changes=[
                        {
                            "param_name": "unauthorized_param",
                            "direction": "increase",
                            "delta": 1.0,
                        }
                    ],
                )
            ),
            expected_snapshot_id=1,
            allowed_params={"temp_day"},
        )
        assert result.outcome is LLMCallOutcome.schema_invalid
        assert any("disallowed parameter" in e for e in result.errors)

    def test_unknown_room_is_schema_invalid(self) -> None:
        result = parse_decision(
            json.dumps(_valid_payload(snapshot_id=1)),
            expected_snapshot_id=1,
            known_room_ids={"f1", "f2"},
            decision_room_id="ghost_room",
        )
        assert result.outcome is LLMCallOutcome.schema_invalid
        assert any("unknown room" in e for e in result.errors)
