from __future__ import annotations

from jw_chat_agent_poc.service.runtime_provenance import _empty_result_calls, _ungrounded_numbers, trace_envelope


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
