from __future__ import annotations

import json
from pathlib import Path

from jw_chat_agent_poc.contracts.routing import RouteMode
from jw_chat_agent_poc.orchestrator.unified_router import (
    MarketShortcutSignals,
    SecurityVerdict,
    UnifiedRouteInput,
    compare_with_legacy,
    route,
)
from jw_chat_agent_poc.tool_use.routing_v4_rules import classify_question
from scripts.phase5a_routing_input_capture import capture_corpus
from test_service import _market_scope_resolver


QUESTION = "리바로 질병 환자수랑 최근 매출 한번에"
FIXTURES = Path(__file__).parent / "characterization" / "fixtures"


def _case17_route():
    return route(
        UnifiedRouteInput(
            question=QUESTION,
            security_verdict=SecurityVerdict.ALLOW,
            market_shortcut=MarketShortcutSignals(
                has_documents=False,
                use_direct_agent_loop=True,
                market_scope_resolver=_market_scope_resolver(),
            ),
        )
    )


def test_case17_preserves_disease_and_sales_capabilities() -> None:
    decision = _case17_route()

    assert decision.domain == "market"
    assert decision.handler == "agent_loop"
    assert decision.execution_mode is RouteMode.AGENTIC
    assert decision.capability == "HIRA_DISEASE_PATIENT_STATS"
    assert decision.requested_capabilities == (
        "HIRA_DISEASE_PATIENT_STATS",
        "MARKET_BRAND_SALES",
    )
    assert decision.unresolved_capabilities == ()
    assert decision.clarification_message is None

    capability_decision = route(
        UnifiedRouteInput(
            question=QUESTION,
            security_verdict=SecurityVerdict.ALLOW,
        )
    )
    assert (
        capability_decision.requested_capabilities == decision.requested_capabilities
    )
    assert capability_decision.unresolved_capabilities == ()
    assert capability_decision.clarification_message is None


def test_case17_matches_legacy_market_shortcut_without_relaxing_comparison() -> None:
    comparison = compare_with_legacy(
        _case17_route(),
        decided_by="market_shortcut",
        legacy_domain="market",
        legacy_handler="agent_loop",
        legacy_mode=RouteMode.AGENTIC,
    )

    assert comparison.matches is True
    assert comparison.mismatch_fields == ()
    assert [item.field for item in comparison.field_comparisons] == [
        "domain",
        "handler",
        "mode",
    ]


def test_full_recomparison_changes_only_case17(monkeypatch) -> None:
    monkeypatch.setenv("JW_CHAT_ROUTER_CUTOVER_HIRA_REIMBURSEMENT", "0")
    monkeypatch.setenv("JW_CHAT_ROUTER_CUTOVER_HIRA_DISEASE_STATS", "0")
    actual = capture_corpus(
        FIXTURES / "corpus.v1.json",
        FIXTURES / "observed_snapshots.v1.json",
    )
    prior = json.loads((FIXTURES / "routing_inputs.v2.json").read_text(encoding="utf-8"))

    assert actual["comparison_totals"] == {
        "match": 319,
        "mismatch": 23,
        "unavailable": 170,
    }
    old_mismatches = {
        (item["question"], item["route_point"]) for item in prior["mismatches"]
    }
    new_mismatches = {
        (item["question"], item["route_point"]) for item in actual["mismatches"]
    }
    assert new_mismatches == old_mismatches - {(QUESTION, "market_shortcut")}

    point_totals: dict[str, dict[str, int]] = {}
    for case in actual["cases"]:
        for point_name, point in case["route_points"].items():
            if point["capture_status"] != "captured":
                continue
            counts = point_totals.setdefault(point_name, {"match": 0, "mismatch": 0})
            key = "match" if point["comparison"]["matches"] else "mismatch"
            counts[key] += 1
    assert point_totals["app_scope"] == {"match": 111, "mismatch": 0}
    assert point_totals["routing_v4_rules"] == {"match": 128, "mismatch": 0}
    assert point_totals["market_shortcut"] == {"match": 80, "mismatch": 23}


def test_fb02_facets_remain_owned_by_routing_v4() -> None:
    classification = classify_question("뇌경색 관련 임상시험이랑 허가 현황 알려줘")
    decision = route(
        UnifiedRouteInput(
            question="뇌경색 관련 임상시험이랑 허가 현황 알려줘",
            security_verdict=SecurityVerdict.ALLOW,
        )
    )

    assert classification.requested_facets == ("clinical", "permission")
    assert tuple(item.facet for item in classification.unresolvable_facets) == ("permission",)
    assert decision.capability == "CLINICAL_TRIAL_SEARCH"
    assert decision.requested_capabilities == ()
    assert decision.unresolved_capabilities == ()


def test_single_unresolved_hira_capability_is_typed_without_claiming_resolution() -> None:
    decision = route(
        UnifiedRouteInput(
            question="리바로 질병 환자수 알려줘",
            security_verdict=SecurityVerdict.ALLOW,
        )
    )

    assert decision.requested_capabilities == ()
    assert decision.unresolved_capabilities == ("HIRA_DISEASE_PATIENT_STATS",)
    assert decision.clarification_message == "capability arguments unresolved"
