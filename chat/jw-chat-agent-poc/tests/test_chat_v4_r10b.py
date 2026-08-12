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
            "delta_basis": "display_end_minus_display_start",
        },
        {
            "brand": "크레스토",
            "role": "competitor",
            "start": "20.00억원",
            "end": "18.00억원",
            "delta": "-2.00억원",
            "delta_basis": "display_end_minus_display_start",
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


def test_r10b_precomputes_share_direction_from_nested_general_view_rows() -> None:
    from jw_chat_agent_poc.service.v4.synthesizer import _comparison_facts

    # Given: the nested general-view payload shape observed on DEV revision 1274.
    result = SourceResult(
        source="mart",
        query="아일리아 요즘 어때",
        status="ok",
        payload={
            "calls": [
                {
                    "tool_calls": [
                        {
                            "tool": "general_view_dynamic_market",
                            "render_data": {
                                "anchor_brand": "아일리아",
                                "competitor_rows": [
                                    {
                                        "brand": "아일리아",
                                        "period": "2021-Q2",
                                        "sales_krw": 17_635_479_450.0,
                                        "share_pct": 63.8922032341,
                                    },
                                    {
                                        "brand": "아일리아",
                                        "period": "2026-Q1",
                                        "sales_krw": 21_867_326_960.0,
                                        "share_pct": 51.3805234812,
                                    },
                                ],
                            },
                        }
                    ]
                }
            ]
        },
    )

    # When: comparison facts are derived for synthesis.
    facts = _comparison_facts((result,))

    # Then: the structured rows produce the explicit adverse share direction.
    assert facts["period_start"] == "2021-Q2"
    assert facts["period_end"] == "2026-Q1"
    assert facts["brand_deltas"] == [
        {
            "brand": "아일리아",
            "role": "target",
            "start": "176.35억원",
            "end": "218.67억원",
            "delta": "+42.32억원",
            "delta_basis": "display_end_minus_display_start",
        }
    ]
    assert facts["share_direction"]["brand_growth"] == "+24.00%"
    assert facts["share_direction"]["market_growth"] == "+54.19%"
    assert facts["share_direction"]["direction"] == "하락"
    assert "점유율 방향은 하락입니다" in facts["share_direction"]["statement"]


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
                    "outcome": "doc_not_found",
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
        "absence_status": "doc_not_found",
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
        "outcome": "doc_not_found",
        "subject": "마운자로",
        "error_code": "REALTIME_NO_EVIDENCE",
    }
    assert _reimbursement_lookup_metadata(timeout, "마운자로") == {
        "document": "reimbursement",
        "outcome": "coverage_unknown",
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
                                    "published_date": "2024-10-25",
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
        "## 핵심 답\n현재 조회한 HIRA 세부 급여기준에서는 별도 기준을 찾지 못했습니다. "
        "이 결과만으로 비급여 여부를 확정할 수는 없습니다. [출처: HIRA]\n\n"
    )
    assert "마운자로 급여 협상 결렬 뒤 재신청" in answer
    assert "로 보도되고 있습니다" in answer


def test_r10b_synthesis_keeps_official_absence_when_web_context_is_empty() -> None:
    from jw_chat_agent_poc.service.v4.runtime import _tag_absence_context
    from jw_chat_agent_poc.service.v4.synthesizer import V4Synthesizer

    class Client:
        def complete(self, _messages, *, budget_s=None, max_tokens=None) -> str:
            return (
                "## 핵심 답\n허가사항은 확인됐습니다.\n\n"
                "## 근거와 맥락\n내부 데이터마트를 참고했습니다.\n\n"
                "## 종합 인사이트\n추가 확인이 필요합니다.\n\n"
                "## 미확인 요소\n웹 보강 자료는 확인되지 않았습니다.\n\n"
                "## 출처\n- 내부 데이터마트"
            )

    # Given: official absence is typed, while optional web context is empty.
    tagged_empty = _tag_absence_context(
        SourceResult(
            source="web",
            query="마운자로 급여기준 부재 경과",
            status="empty",
            payload={"calls": []},
        ),
        {
            "source": "hira",
            "document": "reimbursement",
            "subject": "마운자로",
            "query": "마운자로 급여기준",
        },
    )
    mart = SourceResult(
        source="mart",
        query="마운자로",
        status="ok",
        payload={"calls": [{"summary_text": "시장 참고 자료입니다."}]},
    )

    # When: synthesis succeeds using other evidence.
    answer = V4Synthesizer(Client()).synthesize(
        _plan("마운자로 급여기준"),
        (mart, tagged_empty),
        (),
    )

    # Then: supplemental web failure cannot erase the official absence fact.
    assert answer.startswith(
        "## 핵심 답\n현재 조회한 HIRA 세부 급여기준에서는 별도 기준을 찾지 못했습니다. "
        "이 결과만으로 비급여 여부를 확정할 수는 없습니다. [출처: HIRA]\n\n"
    )
