from __future__ import annotations

import json

from jw_chat_agent_poc.service.v4.contracts import (
    PlannerOutput,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.tools.external.hira_reimbursement import (
    CacheLookupStatus,
    CacheStatus,
    ReimbursementLookupResult,
)


def _plan(question: str, *, answer_sources: tuple[str, ...] = ("mart",)) -> PlannerOutput:
    queries = ToolQueries(
        **{
            source: (question,)
            for source in (
                "mart",
                "nedrug",
                "hira",
                "openfda",
                "clinicaltrials",
                "web",
                "patent",
            )
        }
    )
    return PlannerOutput(
        resolved_question=question,
        expanded_intents=(question,),
        answer_sources=answer_sources,
        tool_queries=queries,
        linking_plan="single wave",
    )


def _comparison_result() -> SourceResult:
    return SourceResult(
        source="mart",
        query="리바로 요즘 어때",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "market_size_series": [
                            {"period": "2025-09", "value_억원": 100.0},
                            {"period": "2026-06", "value_억원": 130.0},
                        ]
                    }
                },
                {
                    "entity_bundle": {
                        "anchor": "리바로",
                        "period_start": "2025-09",
                        "period_end": "2026-06",
                        "same_period_and_denominator": True,
                        "members": [
                            {
                                "brand": "리바로",
                                "role": "target",
                                "share_delta_pctp": -0.8,
                                "render_data": {
                                    "brand_value_series_10pt": [
                                        {"period": "2025-09", "value_억원": 10.0},
                                        {"period": "2026-06", "value_억원": 12.0},
                                    ]
                                },
                            },
                            {
                                "brand": "크레스토",
                                "role": "competitor",
                                "share_delta_pctp": 1.2,
                                "render_data": {
                                    "brand_value_series_10pt": [
                                        {"period": "2025-09", "value_억원": 20.0},
                                        {"period": "2026-06", "value_억원": 18.0},
                                    ]
                                },
                            },
                        ],
                    }
                },
            ]
        },
    )


def test_r10b_precomputes_comparison_facts_for_the_synthesis_prompt() -> None:
    from jw_chat_agent_poc.service.v4.synthesizer import (
        _comparison_facts,
        _synthesis_messages,
    )

    result = _comparison_result()
    facts = _comparison_facts((result,))

    assert facts["period_start"] == "2025-09"
    assert facts["period_end"] == "2026-06"
    assert facts["brand_deltas"] == [
        {
            "brand": "리바로",
            "role": "target",
            "start": "10.00억원",
            "end": "12.00억원",
            "delta": "+2.00억원",
        },
        {
            "brand": "크레스토",
            "role": "competitor",
            "start": "20.00억원",
            "end": "18.00억원",
            "delta": "-2.00억원",
        },
    ]
    assert facts["symmetric_pairs"] == [
        {
            "increase_brand": "리바로",
            "increase_delta": "+2.00억원",
            "decrease_brand": "크레스토",
            "decrease_delta": "-2.00억원",
        }
    ]
    assert facts["share_direction"]["direction"] == "하락"
    assert facts["share_direction"]["brand_growth"] == "+20.00%"
    assert facts["share_direction"]["market_growth"] == "+30.00%"
    assert "점유율 방향은 하락입니다" in facts["share_direction"]["statement"]
    assert facts["competitor_share_changes"] == [
        {"brand": "크레스토", "change": "+1.20%p"}
    ]

    prompt = json.loads(
        _synthesis_messages(_plan("리바로 요즘 어때"), (result,), ())[-1]["content"]
    )
    assert prompt["COMPARISON_FACTS"] == facts
    assert prompt["entity_bundle_contract"]["use_precomputed_comparison_facts"] is True
    assert prompt["entity_bundle_contract"]["explicit_share_direction_sentence"] is True


def test_r10b_absence_bridge_requires_typed_document_absence() -> None:
    from jw_chat_agent_poc.service.v4.runtime import _absence_context_request

    plan = _plan("마운자로 급여기준", answer_sources=("hira",))
    generic_empty = SourceResult(
        source="hira",
        query="마운자로 급여기준",
        status="empty",
        payload={"calls": []},
    )
    assert _absence_context_request(plan, (generic_empty,)) is None

    confirmed = generic_empty.model_copy(
        update={
            "payload": {
                "calls": [],
                "document_lookup": {
                    "document": "reimbursement",
                    "outcome": "confirmed_absent",
                    "subject": "마운자로",
                    "error_code": "REALTIME_NO_EVIDENCE",
                },
            }
        }
    )
    assert _absence_context_request(plan, (confirmed,)) == {
        "source": "hira",
        "document": "reimbursement",
        "subject": "마운자로",
        "query": "마운자로 급여기준",
    }


def test_r10b_reimbursement_metadata_distinguishes_absence_from_unavailable() -> None:
    from jw_chat_agent_poc.service.v4.adapters import _reimbursement_lookup_metadata

    confirmed_absent = ReimbursementLookupResult(
        ok=False,
        cache_status=CacheStatus.NOT_FOUND,
        retrieval="typed_unavailable",
        data=None,
        error_code="REALTIME_NO_EVIDENCE",
        cache_lookup_status=CacheLookupStatus.BRAND_UNMATCHED,
    )
    timeout = ReimbursementLookupResult(
        ok=False,
        cache_status=CacheStatus.NOT_FOUND,
        retrieval="typed_unavailable",
        data=None,
        error_code="TOOL_TIMEOUT",
        cache_lookup_status=CacheLookupStatus.BRAND_UNMATCHED,
    )

    assert _reimbursement_lookup_metadata(confirmed_absent, "마운자로") == {
        "document": "reimbursement",
        "outcome": "confirmed_absent",
        "subject": "마운자로",
        "error_code": "REALTIME_NO_EVIDENCE",
    }
    assert _reimbursement_lookup_metadata(timeout, "마운자로") == {
        "document": "reimbursement",
        "outcome": "unavailable",
        "subject": "마운자로",
        "error_code": "TOOL_TIMEOUT",
    }


def test_r10b_absence_surface_is_first_paragraph_and_reads_nested_web_items() -> None:
    from jw_chat_agent_poc.service.v4.runtime import _tag_absence_context
    from jw_chat_agent_poc.service.v4.synthesizer import _append_absence_context_surface

    request = {
        "source": "hira",
        "document": "reimbursement",
        "subject": "마운자로",
        "query": "마운자로 급여기준",
    }
    result = _tag_absence_context(
        SourceResult(
            source="web",
            query="마운자로 급여기준 부재 경과",
            status="ok",
            payload={
                "calls": [
                    {
                        "render_data": {
                            "items": [
                                {
                                    "url": "https://www.yna.co.kr/view/example",
                                    "title": "마운자로 급여 협상 결렬 뒤 재신청",
                                }
                            ]
                        }
                    }
                ]
            },
        ),
        request,
    )

    answer = _append_absence_context_surface(
        "## 핵심 답\n허가사항은 확인됐습니다.\n\n## 근거와 맥락\n추가 근거입니다.",
        (result,),
    )

    assert answer.startswith(
        "## 핵심 답\n마운자로는 현재 급여기준이 없습니다(비급여). [출처: HIRA]\n\n"
    )
    assert "마운자로 급여 협상 결렬 뒤 재신청" in answer
    assert "로 보도되고 있습니다" in answer
