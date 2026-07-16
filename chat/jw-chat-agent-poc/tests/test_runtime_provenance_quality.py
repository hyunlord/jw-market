from __future__ import annotations

from jw_chat_agent_poc.service.runtime_provenance import _empty_result_calls, _ungrounded_numbers


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
