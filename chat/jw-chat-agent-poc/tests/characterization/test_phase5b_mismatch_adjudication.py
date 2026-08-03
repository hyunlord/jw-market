from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from jw_chat_agent_poc.agent_loop.bq_planner import plan_bq_question
from jw_chat_agent_poc.agent_loop.periods import build_period_grounding
from jw_chat_agent_poc.agent_loop.schemas import tool_schemas
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tool_use.routing_v4_rules import classify_question
from jw_chat_agent_poc.tools.query_layer.catalog import default_catalog


FIXTURES = Path(__file__).parent / "fixtures"
ROUTING_INPUTS = FIXTURES / "routing_inputs.v2.json"
ADJUDICATIONS = FIXTURES / "routing_mismatch_adjudication.v1.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _plan_bq(question: str):
    resolver = BrandResolver(mode="fixture")
    grounding = build_period_grounding(question, current_month=lambda: "2026-06")
    schemas = tool_schemas(("리바로",), grounding.schema_periods, default_catalog())
    return plan_bq_question(question, resolver, grounding, schemas)


def test_every_captured_mismatch_has_an_evidence_backed_adjudication() -> None:
    routing = _read_json(ROUTING_INPUTS)
    adjudication = _read_json(ADJUDICATIONS)

    mismatches = routing["mismatches"]
    cases = adjudication["cases"]
    assert len(mismatches) == len(cases) == 24
    assert [(item["question"], item["route_point"]) for item in mismatches] == [
        (item["question"], item["route_point"]) for item in cases
    ]
    assert all(item["verdict"] != "NOT_PERFORMED" for item in cases)
    assert all(item["legacy_fixture_evidence"] for item in cases)
    assert all(item["canonical_contract_evidence"] for item in cases)
    assert all(item["answer_comparison"] for item in cases)


def test_adjudication_summary_and_defect_scope_are_explicit() -> None:
    payload = _read_json(ADJUDICATIONS)
    counts = Counter(item["verdict"] for item in payload["cases"])

    assert counts == {
        "CANONICAL_CORRECT": 23,
        "LEGACY_CORRECT": 1,
    }
    assert payload["summary"] == {
        "LEGACY_CORRECT": 1,
        "CANONICAL_CORRECT": 23,
        "BOTH_DEFENSIBLE": 0,
        "cutover_answer_change_count": 23,
    }
    defects = [item for item in payload["cases"] if item["verdict"] == "LEGACY_CORRECT"]
    assert [item["case_id"] for item in defects] == [17]


def test_fb02_facets_remain_preserved_below_the_canonical_capability() -> None:
    question = "뇌경색 관련 임상시험이랑 허가 현황 알려줘"
    classification = classify_question(question)

    assert classification.requested_capability == "CLINICAL_TRIAL_SEARCH"
    assert classification.requested_facets == ("clinical", "permission")
    assert tuple(item.facet for item in classification.unresolvable_facets) == ("permission",)


def test_case17_canonical_single_capability_loses_the_legacy_market_facet() -> None:
    question = "리바로 질병 환자수랑 최근 매출 한번에"
    classification = classify_question(question)
    plan = _plan_bq(question)

    assert classification.requested_capability == "HIRA_DISEASE_PATIENT_STATS"
    assert classification.unresolved_arguments is True
    assert plan is not None
    tool_names = {call.name for call in plan.decision.tool_calls}
    assert "get_disease_stats" in tool_names
    assert "get_brand_sales" in tool_names
