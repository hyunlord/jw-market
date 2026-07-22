from __future__ import annotations

import json
from pathlib import Path

from jw_chat_agent_poc.tool_use.routing_v4_release_gate import (
    ShadowActivationObservation,
    evaluate_release_gate,
    evaluate_shadow_activation_gate,
)


CONTRACT_DIR = Path(__file__).parent / "contracts" / "external_tool_routing_v4"


def test_a02_missing_oracle_is_an_inactive_out_of_scope_precondition() -> None:
    manifest = json.loads((CONTRACT_DIR / "expected_route_manifest.json").read_text(encoding="utf-8"))
    conditional_gate = manifest["conditional_gates"]["A-02"]
    case = next(item for item in manifest["cases"] if item["case_id"] == "A-02")

    decision = evaluate_release_gate(manifest)

    assert conditional_gate["active"] is False
    assert conditional_gate["verdict"] == "OUT_OF_SCOPE_PRECONDITION"
    assert conditional_gate["gap_id"] == "GAP-R1-HIRA-CODE-ORACLE"
    assert conditional_gate["external_ticket"] == "PENDING"
    assert case["expected_decision_source"] == "INACTIVE"
    assert case["release_gate"] == "INACTIVE"
    assert case["conditional_gate"] == "A-02"
    assert decision.release_allowed is True
    assert decision.blocking_cases == ()
    assert decision.reason_codes == ()


def test_a02_gap_ledger_preserves_the_oracle_precondition_evidence() -> None:
    gap_manifest = json.loads((CONTRACT_DIR / "gap_manifest.json").read_text(encoding="utf-8"))

    gap = next(item for item in gap_manifest["gaps"] if item["gap_id"] == "GAP-R1-HIRA-CODE-ORACLE")

    assert gap["case_ids"] == ["A-02"]
    assert gap["classification"] == "out_of_scope_precondition"
    assert gap["external_ticket"] == "PENDING"
    assert gap["release_blocking"] is False
    assert gap["evidence"] == [
        "organization-approved H36.0 oracle absent",
        "Phase 0 live HIRA exact H36.0 query returned zero rows",
    ]


def test_a02_expected_change_contract_is_inactive_not_blocked() -> None:
    change_manifest = json.loads(
        (CONTRACT_DIR / "expected_change_manifest.json").read_text(encoding="utf-8")
    )

    assert change_manifest["blocked_cases"] == []
    a02 = next(item for item in change_manifest["inactive_cases"] if item["case_id"] == "A-02")
    assert a02["classification"] == "out_of_scope_precondition"
    assert a02["gap_id"] == "GAP-R1-HIRA-CODE-ORACLE"
    assert a02["external_ticket"] == "PENDING"
    assert a02["allowed_changes"] == []


def test_release_gate_still_fails_closed_for_a_real_blocked_case() -> None:
    decision = evaluate_release_gate(
        {
            "cases": [
                {
                    "case_id": "SYNTHETIC-BLOCKER",
                    "release_gate": "BLOCKED",
                }
            ]
        }
    )

    assert decision.release_allowed is False
    assert decision.blocking_cases == ("SYNTHETIC-BLOCKER",)
    assert decision.reason_codes == ("RELEASE_CASE_BLOCKED",)


def test_shadow_activation_requires_all_five_enforce_conditions() -> None:
    decision = evaluate_shadow_activation_gate(
        (
            ShadowActivationObservation(case_id="D693", eligible_tools_count=1),
            ShadowActivationObservation(case_id="DME", eligible_tools_count=2),
        )
    )

    assert decision.enforce_allowed is True
    assert decision.checked == 2
    assert decision.eligible_cases == 2
    assert decision.blocking_conditions == ()


def test_shadow_activation_fails_closed_for_each_violation_and_empty_population() -> None:
    empty = evaluate_shadow_activation_gate(())
    blocked = evaluate_shadow_activation_gate(
        (
            ShadowActivationObservation(
                case_id="bad",
                eligible_tools_count=0,
                forbidden_tool_calls=1,
                invalid_argument_calls=1,
                wrong_source_owner_calls=1,
                normal_to_typed_unsupported=1,
            ),
        )
    )

    assert empty.enforce_allowed is False
    assert empty.blocking_conditions == ("EMPTY_SHADOW_POPULATION", "NO_ELIGIBLE_TOOL_CASES")
    assert blocked.enforce_allowed is False
    assert blocked.blocking_conditions == (
        "NO_ELIGIBLE_TOOL_CASES",
        "FORBIDDEN_TOOL_CALLS",
        "INVALID_TOOL_ARGUMENTS",
        "WRONG_SOURCE_OWNER",
        "NORMAL_TO_TYPED_UNSUPPORTED",
    )
