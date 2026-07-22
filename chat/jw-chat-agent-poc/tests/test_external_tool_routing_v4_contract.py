from __future__ import annotations

import json
from pathlib import Path

import pytest

from jw_chat_agent_poc.tool_use.routing_v4 import (
    CapabilityMatrix,
    CapabilityStatus,
    DomainDecisionSource,
    ExecutedCall,
    ExecutedCallSignature,
    ProposedCall,
    ProposedRoutingSignature,
    RouteOutcome,
    RoutingDecision,
    RoutingMode,
    ToolSelectionSource,
    compare_proposed_routes,
    default_capability_matrix,
    evaluate_existing_axis_gate,
    evaluate_resolver_precondition,
    parse_routing_mode,
    routing_truth_table,
    selection_source_for_eligible_tools,
    verify_claim_evidence,
)
CONTRACT_DIR = Path(__file__).parent / "contracts" / "external_tool_routing_v4"


def _decision(
    *,
    capability_status: CapabilityStatus = CapabilityStatus.SUPPORTED,
    selection_source: ToolSelectionSource = ToolSelectionSource.DETERMINISTIC_SINGLETON,
    route_outcome: RouteOutcome = RouteOutcome.CALL,
) -> RoutingDecision:
    return RoutingDecision(
        source_domain="hira",
        domain_decision_source=DomainDecisionSource.PREFIX_RULE,
        capability_status=capability_status,
        tool_selection_source=selection_source,
        route_outcome=route_outcome,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        (None, RoutingMode.OFF),
        ("", RoutingMode.OFF),
        ("typo", RoutingMode.OFF),
        ("off", RoutingMode.OFF),
        ("shadow", RoutingMode.SHADOW),
        ("ENFORCE", RoutingMode.ENFORCE),
    ),
)
def test_routing_mode_defaults_invalid_or_missing_values_to_off(
    raw: str | None,
    expected: RoutingMode,
) -> None:
    assert parse_routing_mode(raw) is expected


@pytest.mark.parametrize("force_contract_calls", (False, True))
def test_off_truth_table_preserves_legacy_force_behavior(force_contract_calls: bool) -> None:
    behavior = routing_truth_table(RoutingMode.OFF, force_contract_calls=force_contract_calls)

    assert behavior.response_path == "legacy"
    assert behavior.legacy_force_contract_calls is force_contract_calls
    assert behavior.new_router_enabled is False
    assert behavior.new_provider_enabled is False
    assert behavior.new_router_affects_response is False


@pytest.mark.parametrize("mode", (RoutingMode.SHADOW, RoutingMode.ENFORCE))
@pytest.mark.parametrize("force_contract_calls", (False, True))
def test_new_router_truth_table_never_disables_provider_via_legacy_force_flag(
    mode: RoutingMode,
    force_contract_calls: bool,
) -> None:
    behavior = routing_truth_table(mode, force_contract_calls=force_contract_calls)

    assert behavior.legacy_force_contract_calls is force_contract_calls
    assert behavior.new_router_enabled is True
    assert behavior.new_provider_enabled is True
    assert behavior.new_router_affects_response is (mode is RoutingMode.ENFORCE)
    assert behavior.new_router_executes_tools is (mode is RoutingMode.ENFORCE)


def test_capability_manifest_uses_exactly_the_four_v4_states() -> None:
    matrix = CapabilityMatrix.from_json(CONTRACT_DIR / "capability_matrix.json")

    assert set(CapabilityStatus) == {
        CapabilityStatus.SUPPORTED,
        CapabilityStatus.FIELD_NOT_EXPOSED,
        CapabilityStatus.NOT_IMPLEMENTED,
        CapabilityStatus.UNRESOLVED,
    }
    assert matrix.status_for(
        "hira", "HIRA_DISEASE_PATIENT_STATS", input_key="sick_cd"
    ) is CapabilityStatus.SUPPORTED
    assert matrix.status_for(
        "hira", "HIRA_DISEASE_PATIENT_STATS", input_key="disease_name"
    ) is CapabilityStatus.SUPPORTED
    assert matrix.status_for(
        "hira", "HIRA_LABEL_EFFICACY", input_key="product_name"
    ) is CapabilityStatus.FIELD_NOT_EXPOSED
    assert matrix.status_for(
        "regulatory", "REIMBURSEMENT_CRITERIA", input_key="product_name"
    ) is CapabilityStatus.NOT_IMPLEMENTED
    assert matrix.status_for(
        "unresolved", "UNCLASSIFIED_EXTERNAL_REQUEST", input_key="unknown"
    ) is CapabilityStatus.UNRESOLVED


def test_nct_detail_capability_is_supported_by_verified_detail_tool() -> None:
    matrix = default_capability_matrix()

    resolution = matrix.resolve(
        "clinical_trials",
        "CLINICAL_TRIAL_NCT_DETAIL_FIELDS",
        input_key="nct_id",
    )

    assert resolution.status is CapabilityStatus.SUPPORTED
    assert resolution.eligible_tools == ("clinicaltrials_study_details",)


def test_capability_matrix_keeps_identifier_contracts_separate() -> None:
    matrix = CapabilityMatrix.from_json(CONTRACT_DIR / "capability_matrix.json")

    code = matrix.resolve("hira", "HIRA_DISEASE_PATIENT_STATS", input_key="sick_cd")
    disease_name = matrix.resolve(
        "hira", "HIRA_DISEASE_PATIENT_STATS", input_key="disease_name"
    )
    nct_detail = matrix.resolve(
        "clinical_trials", "CLINICAL_TRIAL_NCT_DETAIL_FIELDS", input_key="nct_id"
    )
    unknown = matrix.resolve(
        "clinical_trials", "CLINICAL_TRIAL_SEARCH", input_key="unsupported_identifier"
    )

    assert code.input_key == "sick_cd"
    assert code.eligible_tools == disease_name.eligible_tools
    assert nct_detail.status is CapabilityStatus.SUPPORTED
    assert nct_detail.eligible_tools == ("clinicaltrials_study_details",)
    assert unknown.status is CapabilityStatus.UNRESOLVED
    assert unknown.typed_reason_code == "AMBIGUOUS_INPUT"


def test_mfds_composition_is_supported_while_easy_drug_fields_remain_unexposed() -> None:
    matrix = default_capability_matrix()

    assert matrix.resolve(
        "regulatory", "MFDS_COMPOSITION", input_key="product_name"
    ).eligible_tools == (
        "mfds_composition",
    )
    assert matrix.status_for(
        "regulatory", "MFDS_EASY_DRUG_FIELDS", input_key="product_name"
    ) is CapabilityStatus.FIELD_NOT_EXPOSED


def test_runtime_default_capability_matrix_matches_the_frozen_manifest() -> None:
    payload = json.loads((CONTRACT_DIR / "capability_matrix.json").read_text(encoding="utf-8"))
    runtime = default_capability_matrix()

    for expected in payload["entries"]:
        actual = runtime.resolve(
            expected["source_domain"],
            expected["requested_capability"],
            input_key=expected["input_key"],
        )
        assert actual.status.value == expected["capability_status"]
        assert actual.eligible_tools == tuple(expected["eligible_tools"])


def test_unresolved_capability_is_not_collapsed_to_not_implemented() -> None:
    matrix = CapabilityMatrix.from_json(CONTRACT_DIR / "capability_matrix.json")

    resolution = matrix.resolve("unresolved", "UNCLASSIFIED_EXTERNAL_REQUEST")

    assert resolution.status is CapabilityStatus.UNRESOLVED
    assert resolution.eligible_tools == ()
    assert resolution.typed_reason_code == "AMBIGUOUS_INPUT"


def test_expected_route_manifest_uses_routing_decision_enums_in_their_own_fields() -> None:
    payload = json.loads((CONTRACT_DIR / "expected_route_manifest.json").read_text(encoding="utf-8"))
    decisions = tuple(
        case["routing_decision"]
        for case in payload["cases"]
        if case.get("routing_decision") is not None
    )

    assert all(item["domain_decision_source"] in {value.value for value in DomainDecisionSource} for item in decisions)
    assert all(item["capability_status"] in {value.value for value in CapabilityStatus} for item in decisions)
    assert all(item["tool_selection_source"] in {value.value for value in ToolSelectionSource} for item in decisions)
    assert all(item["route_outcome"] in {value.value for value in RouteOutcome} for item in decisions)


def test_prs_has_only_proposed_route_and_no_runtime_result_fields() -> None:
    proposal = ProposedRoutingSignature(
        routing_mode=RoutingMode.SHADOW,
        routing_decision=_decision(),
        proposed_calls=(
            ProposedCall(
                tool_name="hira_disease_hospitalization_outpatient_stats",
                normalized_args={"sick_cd": "D69.3", "year": "2024"},
            ),
        ),
    )

    dumped = proposal.model_dump(mode="json")

    assert "executed_calls" not in dumped
    assert "fallback_reason" not in dumped
    assert "reason_code" not in dumped
    assert "runtime_status" not in dumped
    assert "result_status" not in str(dumped)


def test_ccs_adds_ordered_execution_results_and_projects_back_to_prs() -> None:
    decision = _decision()
    proposal_call = ProposedCall(
        tool_name="hira_disease_hospitalization_outpatient_stats",
        normalized_args={"sick_cd": "D69.3", "year": "2024"},
    )
    ccs = ExecutedCallSignature(
        routing_mode=RoutingMode.ENFORCE,
        routing_decision=decision,
        proposed_calls=(proposal_call,),
        executed_calls=(
            ExecutedCall(
                call_ordinal=1,
                parent_ordinal=None,
                tool_name=proposal_call.tool_name,
                normalized_args=proposal_call.normalized_args,
                result_status="ok",
            ),
        ),
        fallback_reason=None,
        reason_code=None,
        runtime_status="ok",
    )

    assert ccs.executed_calls[0].call_ordinal == 1
    assert "parent_call_id" not in ccs.model_dump(mode="json")
    assert compare_proposed_routes(
        ccs.as_proposed(routing_mode=RoutingMode.SHADOW),
        ProposedRoutingSignature(
            routing_mode=RoutingMode.SHADOW,
            routing_decision=decision,
            proposed_calls=(proposal_call,),
        ),
    )


def test_shadow_enforce_comparison_ignores_runtime_only_ccs_fields() -> None:
    proposal = ProposedRoutingSignature(
        routing_mode=RoutingMode.SHADOW,
        routing_decision=_decision(),
        proposed_calls=(ProposedCall(tool_name="mfds_permission_search", normalized_args={"brand": "아일리아"}),),
    )
    enforce = ExecutedCallSignature(
        routing_mode=RoutingMode.ENFORCE,
        routing_decision=proposal.routing_decision,
        proposed_calls=proposal.proposed_calls,
        executed_calls=(
            ExecutedCall(
                call_ordinal=1,
                parent_ordinal=None,
                tool_name="mfds_permission_search",
                normalized_args={"brand": "아일리아"},
                result_status="timeout",
            ),
        ),
        fallback_reason="upstream timeout",
        reason_code="UPSTREAM_UNAVAILABLE",
        runtime_status="typed_stop",
    )

    assert compare_proposed_routes(proposal, enforce.as_proposed(routing_mode=RoutingMode.SHADOW))


@pytest.mark.parametrize(
    ("available_axes", "reason_code", "table_allowed", "missing_axes"),
    (
        (("department",), "PARTIAL_RESULT", True, ("channel",)),
        ((), "FIELD_NOT_EXPOSED", False, ("department", "channel")),
    ),
)
def test_a11_gate_uses_only_axes_already_present_in_tool_results(
    available_axes: tuple[str, ...],
    reason_code: str,
    table_allowed: bool,
    missing_axes: tuple[str, ...],
) -> None:
    gate = evaluate_existing_axis_gate(
        requested_axes=("department", "channel"),
        available_axes=available_axes,
    )

    assert gate.provided_axes == available_axes
    assert gate.missing_axes == missing_axes
    assert gate.reason_code == reason_code
    assert gate.table_allowed is table_allowed
    assert gate.may_assemble_new_axes is False


def test_a12_precondition_false_deactivates_release_gate_and_forbids_r5_edit() -> None:
    gate = evaluate_resolver_precondition(exact_unique=False)

    assert gate.active is False
    assert gate.release_gate_active is False
    assert gate.r5_edit_allowed is False
    assert gate.classification == "out_of_scope_precondition"
    assert gate.expected_gap is False


def test_a12_precondition_true_activates_release_gate_and_allows_r5_edit() -> None:
    gate = evaluate_resolver_precondition(exact_unique=True)

    assert gate.active is True
    assert gate.release_gate_active is True
    assert gate.r5_edit_allowed is True
    assert gate.classification == "active"
    assert gate.expected_gap is False


def test_a13_single_eligible_tool_is_deterministic_not_llm_evidence() -> None:
    assert selection_source_for_eligible_tools(1) is ToolSelectionSource.DETERMINISTIC_SINGLETON
    assert selection_source_for_eligible_tools(2) is ToolSelectionSource.LLM
    assert selection_source_for_eligible_tools(0) is ToolSelectionSource.NONE


def test_evidence_binding_positive_and_lineage_mutation_proofs_are_distinct() -> None:
    assert verify_claim_evidence(
        expected_evidence_ids=("HIRA_FIXTURE_01",),
        bound_evidence_ids=("HIRA_FIXTURE_01",),
    )
    assert not verify_claim_evidence(
        expected_evidence_ids=("HIRA_FIXTURE_01",),
        bound_evidence_ids=("MODEL_MEMORY",),
    )
