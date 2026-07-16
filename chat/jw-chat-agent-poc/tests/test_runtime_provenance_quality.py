from __future__ import annotations

from jw_chat_agent_poc.agent_loop.tools import ToolExecution
from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.service.markdown_cleanup import cleanup_markdown_answer
from jw_chat_agent_poc.service.runtime_provenance import _empty_result_calls, _ungrounded_numbers, trace_envelope
from jw_chat_agent_poc.service.web_mi_summary import web_search_mi_section_from_calls


def test_recovered_tool_call_is_not_reported_as_empty_result() -> None:
    result = {
        "tool_calls": [
            {"tool": "mfds_permission_search", "status": "error"},
            {
                "tool": "mfds_permission_search",
                "status": "ok",
                "render_data": {
                    "ok": True,
                    "evidence": [{"subject": "리바로", "metric": "허가 품목"}],
                },
            },
        ]
    }

    assert _empty_result_calls(result) == ()


def test_unrecovered_tool_call_remains_empty_result() -> None:
    result = {
        "tool_calls": [
            {"tool": "mfds_permission_search", "status": "error"},
            {"tool": "web_search", "status": "ok", "render_data": {"ok": True, "evidence": [{}]}},
        ]
    }

    assert _empty_result_calls(result) == (
        {"tool": "mfds_permission_search", "status": "error"},
    )


def test_rendered_fact_number_is_grounded_when_allowed_numbers_is_incomplete() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "| 기간 | 매출 |\n| --- | --- |\n| 2025-04 | 83.184115억원 |",
        "data_md": "",
    }

    assert _ungrounded_numbers("리바로 2025-04 매출은 83.184115억원입니다.", markdown_response) == ()


def test_exact_monthly_sales_is_grounded_by_successful_structured_tool_value() -> None:
    call = {
        "tool": "get_brand_metric",
        "status": "ok",
        "source": "UBIST",
        "render_data": {
            "status": "ok",
            "brand": "리바로",
            "metric": "sales",
            "period": "2025-04",
            "sales_억원": 83.184115,
            "sales_krw": 8_318_411_526.5,
        },
    }
    fact_md = answer_fact_markdown([call], ["UBIST"])
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": fact_md,
        "data_md": fact_md,
    }

    assert "| 매출 | 83.18억원 |" in fact_md
    assert (
        _ungrounded_numbers(
            "2025-04 리바로 매출은 83.184115억원입니다.",
            markdown_response,
            [call],
        )
        == ()
    )


def test_successful_execution_preserves_status_for_runtime_numeric_grounding() -> None:
    call = {
        "tool": "get_brand_metric",
        "source": "UBIST",
        "render_data": {
            "brand": "리바로",
            "metric": "sales",
            "period": "2025-04",
            "sales_억원": 83.184115,
            "sales_krw": 8_318_411_526.5,
        },
    }

    execution = ToolExecution("ok", "리바로 sales query-layer", call, {"period": "2025-04"})

    assert execution.call["status"] == "ok"
    assert (
        _ungrounded_numbers(
            "2025-04 리바로 매출은 83.184115억원입니다.",
            {"allowed_numbers": (), "fact_md": "", "data_md": ""},
            [execution.call],
        )
        == ()
    )


def test_number_absent_from_rendered_facts_remains_ungrounded() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "| 기간 | 매출 |\n| --- | --- |\n| 2025-04 | 83.184115억원 |",
        "data_md": "",
    }

    assert _ungrounded_numbers("리바로 매출은 99.99억원입니다.", markdown_response) == ("99.99억원",)


def test_public_web_search_number_is_grounded_by_rendered_tool_evidence() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "ok",
            "render_data": {
                "items": [
                    {
                        "title": "가이드라인 업데이트",
                        "url": "https://example.test/guideline",
                        "snippet": "고위험군에서 LDL-C를 30% 이상 낮추도록 권고합니다.",
                    }
                ]
            },
        }
    ]

    assert (
        _ungrounded_numbers(
            "웹 검색 근거에서는 LDL-C를 30% 이상 낮추도록 권고합니다.",
            markdown_response,
            tool_calls,
        )
        == ()
    )


def test_live_public_web_search_number_is_grounded_by_rendered_tool_evidence() -> None:
    # Given: the live external adapter returned public web evidence.
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "live",
            "render_data": {
                "items": [
                    {
                        "title": "2023-06-20 가이드라인 업데이트",
                        "url": "https://example.test/guideline",
                        "snippet": "고위험군에서 LDL-C를 30% 이상 낮추도록 권고합니다.",
                    }
                ]
            },
        }
    ]

    # When: the runtime grounding gate checks the answer.
    ungrounded = _ungrounded_numbers(
        "2023-06-20 지침은 LDL-C를 30% 이상 낮추도록 권고합니다.",
        markdown_response,
        tool_calls,
    )

    # Then: values in the public live projection are grounded.
    assert ungrounded == ()


def test_partial_public_evidence_number_is_grounded_when_one_source_succeeds() -> None:
    # Given: one external source returned evidence while another returned no data.
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "source": "web_search",
            "status": "partial",
            "render_data": {
                "calls": [
                    {
                        "tool": "web_search",
                        "status": "live",
                        "render_data": {
                            "items": [
                                {
                                    "title": "가이드라인 업데이트",
                                    "url": "https://example.test/guideline",
                                    "snippet": "LDL-C 목표를 28% 낮춘 결과를 보고했습니다.",
                                }
                            ]
                        },
                    },
                    {
                        "tool": "web_search",
                        "status": "no_data",
                        "render_data": {"items": []},
                    },
                ]
            },
        }
    ]

    # When: the runtime grounding gate checks a value from the successful source.
    ungrounded = _ungrounded_numbers(
        "확인된 공개 근거에서는 LDL-C 목표가 28% 낮아졌습니다.",
        markdown_response,
        tool_calls,
    )

    # Then: partial aggregate evidence remains usable.
    assert ungrounded == ()


def test_non_rendered_tool_internal_number_remains_ungrounded() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "ok",
            "render_data": {
                "items": [
                    {
                        "title": "가이드라인 업데이트",
                        "url": "https://example.test/guideline",
                        "snippet": "고위험군 치료 권고를 정리했습니다.",
                    }
                ],
                "internal_total_count": 999,
            },
        }
    ]

    assert _ungrounded_numbers("검색 내부 건수는 999건입니다.", markdown_response, tool_calls) == ("999건",)


def test_live_tool_internal_number_remains_ungrounded() -> None:
    # Given: a live call includes one public item and an internal-only counter.
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "live",
            "render_data": {
                "items": [
                    {
                        "title": "가이드라인 업데이트",
                        "url": "https://example.test/guideline",
                        "snippet": "고위험군 치료 권고를 정리했습니다.",
                    }
                ],
                "internal_total_count": 999,
            },
        }
    ]

    # When: the answer cites the internal-only counter.
    ungrounded = _ungrounded_numbers("검색 내부 건수는 999건입니다.", markdown_response, tool_calls)

    # Then: live status does not expose fields outside the public projection.
    assert ungrounded == ("999건",)


def test_deterministic_web_appendix_is_excluded_from_claim_grounding() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "live",
            "render_data": {
                "items": [
                    {
                        "title": "과거 허가 기사",
                        "url": "https://www.biospectator.com/news/view/27271",
                        "snippet": "리바로 허가 이력을 정리한 기사입니다.",
                        "published_date": "2016-05-20",
                    }
                ]
            },
        }
    ]
    answer = "\n\n".join(
        (
            "시장 수치는 99.99억원입니다.",
            web_search_mi_section_from_calls(tool_calls),
        )
    )

    assert _ungrounded_numbers(answer, markdown_response, tool_calls) == ("99.99억원",)


def test_cleaned_deterministic_web_appendix_is_excluded_from_claim_grounding() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "live",
            "render_data": {
                "items": [
                    {
                        "title": "리바로 1위브랜드 허가 기사",
                        "url": "https://www.biospectator.com/news/view/27271",
                        "snippet": "리바로 허가 이력을 정리한 기사입니다.",
                        "published_date": "2016-05-20",
                    }
                ]
            },
        }
    ]
    answer = cleanup_markdown_answer(
        "\n\n".join(
            (
                "시장 수치는 99.99억원입니다.",
                web_search_mi_section_from_calls(tool_calls),
            )
        )
    )

    assert _ungrounded_numbers(answer, markdown_response, tool_calls) == ("99.99억원",)


def test_web_appendix_heading_alone_does_not_bypass_claim_grounding() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "live",
            "render_data": {
                "items": [
                    {
                        "title": "리바로 허가 기사",
                        "url": "https://example.test/article",
                        "snippet": "리바로 허가 이력을 정리한 기사입니다.",
                        "published_date": "2016-05-20",
                    }
                ]
            },
        }
    ]
    answer = "### 웹 검색 결과(미검증)\n\n시장 수치는 99.99억원입니다."

    assert _ungrounded_numbers(answer, markdown_response, tool_calls) == ("99.99억원",)


def test_number_repeated_in_narrative_remains_ungrounded_even_when_web_appendix_contains_it() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "live",
            "render_data": {
                "items": [
                    {
                        "title": "과거 허가 기사",
                        "url": "https://www.biospectator.com/news/view/27271",
                        "snippet": "리바로 허가 이력을 정리한 기사입니다.",
                        "published_date": "2016-05-20",
                    }
                ]
            },
        }
    ]
    answer = "\n\n".join(
        (
            "기사 식별자 27271이 핵심 시장 수치입니다.",
            web_search_mi_section_from_calls(tool_calls),
        )
    )

    assert _ungrounded_numbers(answer, markdown_response, tool_calls) == ("27271",)


def test_bare_url_identifiers_are_not_treated_as_numeric_claims() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    answer = (
        "| 기사 | URL |\n"
        "| --- | --- |\n"
        "| 과거 허가 기사 | https://example.test/news/2016/27271-20?item=3300 |"
    )

    assert _ungrounded_numbers(answer, markdown_response) == ()


def test_visible_number_remains_ungrounded_when_same_number_appears_in_bare_url() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    answer = "\n".join(
        (
            "기사 식별자 27271이 핵심 시장 수치입니다.",
            "원문: https://example.test/news/2016/27271-20?item=3300",
        )
    )

    assert _ungrounded_numbers(answer, markdown_response) == ("27271",)


def test_trace_envelope_grounds_numbers_from_public_tool_projection() -> None:
    result = {
        "context_scope": "MARKET",
        "tool_calls": [
            {
                "tool": "web_search",
                "status": "ok",
                "render_data": {
                    "items": [
                        {
                            "title": "가이드라인 업데이트",
                            "url": "https://example.test/guideline",
                            "snippet": "고위험군에서 LDL-C를 30% 이상 낮추도록 권고합니다.",
                        }
                    ]
                },
            }
        ],
        "markdown_response": {
            "allowed_numbers": (),
            "fact_md": "",
            "data_md": "",
        },
    }

    trace = trace_envelope(
        question="/deep 고지혈증 치료 가이드라인",
        result=result,
        answer="웹 검색 근거에서는 LDL-C를 30% 이상 낮추도록 권고합니다.",
        charts=(),
        timing={"stages": []},
        conversation_id="fixture",
    )

    assert trace["ungrounded_numeric_spans"] == ()
