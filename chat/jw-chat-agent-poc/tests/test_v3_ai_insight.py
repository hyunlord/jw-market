from __future__ import annotations

from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    InsightFact,
    ToolExecutionRecord,
    V3EvidenceBundle,
)
from jw_chat_agent_poc.tool_use.v3_execution_conversion import convert_execution_facts
from jw_chat_agent_poc.tool_use.v3_fusion import validate_fusion_answer
from jw_chat_agent_poc.tool_use.v3_fusion_contracts import (
    GeneratedFusionAnswer,
    GeneratedFusionClaim,
)
from jw_chat_agent_poc.tool_use.v3_fusion_evidence import fusion_fact_payload


def _insight(raw_text: str = "경쟁 구도가 변화하고 있습니다.") -> InsightFact:
    return InsightFact(
        evidence_id="v3-shadow:market.get_deep_analysis:1234567890abcdef",
        tool_name="market.get_deep_analysis",
        arguments={"brand": "리바로"},
        raw_result={"raw_text": raw_text},
        missing_required_fields=(),
        raw_text=raw_text,
        generated_by="deep-analysis-api-llm",
        fetched_at_utc="2026-08-06T00:00:00Z",
        target_market="ml_006",
        target_brand="리바로",
        api_response_location="data.ai_analysis",
    )


def _bundle(fact: InsightFact) -> V3EvidenceBundle:
    return V3EvidenceBundle(
        status="complete",
        facts=(fact,),
        failures=(),
        deferred=(),
        executions=(),
        original_call_count=1,
        executed_call_count=1,
        deduplicated_call_count=0,
    )


def test_deep_analysis_conversion_supplies_distinct_insight_fact() -> None:
    record = ToolExecutionRecord(
        tool_name="market.get_deep_analysis",
        arguments={"brand": "리바로", "market": "ml_006"},
        raw_result={
            "render_data": {
                "brand": "리바로",
                "metric": "deep_analysis",
                "period": "2026-05",
                "unit_label": "mixed",
                "view_type": "market_landscape",
                "market_id": "ml_006",
            },
            "insight": {
                "raw_text": "경쟁 구도가 변화하고 있습니다.",
                "generated_by": "deep-analysis-api-llm",
                "fetched_at_utc": "2026-08-06T00:00:00Z",
                "target_market": "ml_006",
                "target_brand": "리바로",
                "api_response_location": "data.ai_analysis",
            },
        },
        latency_ms=10.0,
    )

    facts, failure, deferred = convert_execution_facts(record, "market")

    assert failure is None
    assert deferred is None
    assert [fact.fact_type for fact in facts] == ["market_metric", "insight"]
    assert isinstance(facts[1], InsightFact)
    assert facts[0].evidence_id != facts[1].evidence_id


def test_insight_payload_never_supplies_allowed_numeric_literals() -> None:
    payload = fusion_fact_payload(_insight("매출 130억원으로 전망됩니다."))

    assert payload["allowed_numeric_literals"] == []
    assert "130" not in payload.get("web_quoted_numeric_literals", [])


def test_insight_gate_rejects_missing_source_and_numeric_promotion() -> None:
    plain = _insight()
    missing_source = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(GeneratedFusionClaim(text=plain.raw_text, evidence_ids=(plain.evidence_id,)),),
            limitations=(),
        ),
        _bundle(plain),
    )
    assert missing_source.audit.rejected_claims[0].reason == "insight_source_missing"

    numeric = _insight("매출 130억원으로 전망됩니다.")
    promoted = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(GeneratedFusionClaim(text=f'시스템 AI 인사이트에 따르면 "{numeric.raw_text}"', evidence_ids=(numeric.evidence_id,)),),
            limitations=(),
        ),
        _bundle(numeric),
    )
    assert promoted.audit.rejected_claims[0].reason == "insight_numeric_promoted"


def test_insight_gate_accepts_visibly_attributed_verbatim_text() -> None:
    fact = _insight()
    answer = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(GeneratedFusionClaim(text=f'시스템 AI 인사이트에 따르면 "{fact.raw_text}"', evidence_ids=(fact.evidence_id,)),),
            limitations=(),
        ),
        _bundle(fact),
    )

    assert answer.answer.claims[0].text.endswith(f'"{fact.raw_text}"')
    assert answer.audit.rejected_claims == ()
