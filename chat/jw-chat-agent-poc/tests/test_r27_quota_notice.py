from __future__ import annotations

import pytest
import requests

from jw_chat_agent_poc.service.v4.contracts import SourceResult
from jw_chat_agent_poc.service.v4.adapters import _external_call_http_status
from jw_chat_agent_poc.service.v4.retrieval_events import (
    public_retrieval_notice,
    retrieval_event_from_result,
)
from jw_chat_agent_poc.service.v4.runtime import _retrieval_shortfall_notice
from jw_chat_agent_poc.tools.external.client import (
    HIRA_MCP_SOURCE,
    NEDRUG_MCP_SOURCE,
    TAVILY_MCP_SOURCE,
    ExternalCall,
    _mcp_external_call,
)
from jw_chat_agent_poc.tools.external import client as external_client
from jw_chat_agent_poc.tools.external.mcp_client import McpToolResult
from jw_chat_agent_poc.tools.external.telemetry import failure_class_from_call


PUBLIC_DATA_QUOTA_MESSAGE = "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"


def _public_data_quota_result() -> McpToolResult:
    payload = {
        "response": {
            "header": {"resultCode": "22", "resultMsg": PUBLIC_DATA_QUOTA_MESSAGE},
            "body": {"items": []},
        }
    }
    return McpToolResult(
        content_text="",
        raw_result={"structuredContent": {"result": payload}},
    )


@pytest.mark.parametrize(
    ("source", "tool", "mcp_tool"),
    [
        (NEDRUG_MCP_SOURCE, "mfds_permission_search", "search_drug_products"),
        (HIRA_MCP_SOURCE, "hira_disease_stats", "get_disease_statistics"),
    ],
)
def test_public_data_quota_is_not_misclassified_as_zero_rows(
    source: str,
    tool: str,
    mcp_tool: str,
) -> None:
    # Given a live-shaped MCP wrapper whose HTTP request succeeded but provider code is 22
    result = _public_data_quota_result()

    # When the common MCP boundary normalizes the response
    call = _mcp_external_call(
        tool,
        source,
        {"query": "리바로"},
        mcp_tool,
        result,
        "http://gateway/mcp",
        10.0,
    )

    # Then the provider exhaustion remains distinct from a valid empty result
    assert call.status == "error"
    assert call.render_data["error_type"] == "quota"
    assert call.render_data["items"] == []
    assert call.render_data["message"] == "제공자 조회 한도 초과"
    assert call.status != "no_data"


def test_disabling_quota_detection_recreates_the_zero_row_regression(monkeypatch) -> None:
    """F2: prove the exact old branch returns when quota detection is disabled."""
    monkeypatch.setattr(
        external_client,
        "_public_data_quota_exceeded",
        lambda _payload, _content_text: False,
    )

    call = _mcp_external_call(
        "mfds_permission_search",
        NEDRUG_MCP_SOURCE,
        {"query": "리바로"},
        "search_drug_products",
        _public_data_quota_result(),
        "http://gateway/mcp",
        10.0,
    )

    assert call.status == "no_data"
    assert call.render_data.get("error_type") != "quota"


def test_public_data_result_code_22_is_sufficient_without_message_text() -> None:
    result = McpToolResult(
        content_text="",
        raw_result={
            "structuredContent": {
                "result": {
                    "response": {
                        "header": {"resultCode": "22"},
                        "body": {"items": []},
                    }
                }
            }
        },
    )

    call = _mcp_external_call(
        "hira_disease_stats",
        HIRA_MCP_SOURCE,
        {"disease_code": "D50"},
        "get_disease_statistics",
        result,
        "http://gateway/mcp",
        10.0,
    )

    assert call.status == "error"
    assert call.render_data["error_type"] == "quota"


def test_tavily_quota_is_named_in_call_details() -> None:
    # Given Tavily's observed plan-limit error wrapper
    result = McpToolResult(
        content_text=(
            'Tavily API error: {"error":"This request exceeds your plan\'s '
            'set usage limit."}'
        ),
        raw_result={"isError": True},
    )

    # When the Tavily MCP result is normalized
    call = _mcp_external_call(
        "tavily_mcp_search",
        TAVILY_MCP_SOURCE,
        {"query": "리바로"},
        "tavily_search",
        result,
        "http://gateway/mcp/214",
        10.0,
    )

    # Then inspection details and telemetry can distinguish quota from zero rows
    assert call.status == "error"
    assert call.render_data["error_type"] == "quota"
    assert failure_class_from_call(call) == "quota"


def test_tavily_http_429_is_classified_as_quota() -> None:
    response = requests.Response()
    response.status_code = 429
    error = requests.HTTPError("429 Client Error", response=response)

    assert external_client.failure_class_from_exception(error) == "quota"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("web", "웹 검색은 조회 한도를 초과해 이번 답변에 반영되지 않았습니다."),
        ("nedrug", "식약처 조회는 일일 한도를 초과해 이번 답변에 반영되지 않았습니다."),
        ("hira", "HIRA 조회는 일일 한도를 초과해 이번 답변에 반영되지 않았습니다."),
    ],
)
def test_quota_shortfall_notice_is_source_specific(source: str, expected: str) -> None:
    # Given an executed source query that the provider rejected for quota
    result = SourceResult(
        source=source,
        query="원문 질의",
        status="quota",
        failure_reason="QUOTA_EXCEEDED",
    )

    # When both deterministic notice surfaces render the result
    shortfall = _retrieval_shortfall_notice((result,)) or ""
    public = public_retrieval_notice(retrieval_event_from_result(result))

    # Then users see which provider was unavailable without internal reason codes
    assert expected in shortfall
    assert expected == public
    assert "QUOTA_EXCEEDED" not in shortfall
    assert "provider_quota" not in shortfall


def test_partial_patent_result_surfaces_nedrug_quota_without_losing_facts() -> None:
    # Given a patent lane that retained US facts while the NeDrug sub-call hit quota
    result = SourceResult(
        source="patent",
        query="리바로젯 특허 현황",
        status="ok",
        payload={"calls": [{"status": "live", "items": [{"patent_no": "US-1"}]}]},
        failure_detail={"provider_quotas": [NEDRUG_MCP_SOURCE]},
    )

    # When the shortfall notice is rendered
    notice = _retrieval_shortfall_notice((result,)) or ""

    # Then the preserved fact lane stays successful while the missing provider is disclosed
    assert "식약처 조회는 일일 한도를 초과해 이번 답변에 반영되지 않았습니다." in notice
    assert result.status == "ok"
    assert result.payload["calls"]


def test_exhausted_patent_lane_names_the_nedrug_provider() -> None:
    # Given a patent lane whose NeDrug sub-call is the exhausted provider
    result = SourceResult(
        source="patent",
        query="리바로젯 특허 현황",
        status="quota",
        failure_reason="QUOTA_EXCEEDED",
        failure_detail={"provider_quotas": [NEDRUG_MCP_SOURCE]},
    )

    # When the lane-level notice is rendered
    notice = _retrieval_shortfall_notice((result,)) or ""

    # Then the aggregate lane does not hide which upstream limit was reached
    assert "식약처 조회는 일일 한도를 초과" in notice
    assert "특허 조회는 제공자 한도를 초과" not in notice


def test_http_status_is_not_inferred_from_arbitrary_three_digit_payload_text() -> None:
    # Given a trace containing timestamps and counters but no status field
    payload = {
        "render_data": {
            "observed_at": "2026-08-17T03:26:35+09:00",
            "queue_depth": 327,
        }
    }

    # When the adapter reads transport status
    absent = _external_call_http_status(payload)
    explicit = _external_call_http_status({"render_data": {"http_status": 429}})

    # Then only the explicitly named status is accepted
    assert absent is None
    assert explicit == 429


def test_valid_empty_result_remains_distinct_from_quota() -> None:
    # Given a provider that completed normally with no matching rows
    empty = SourceResult(source="hira", query="D999", status="empty")

    # When its shortfall is rendered
    notice = _retrieval_shortfall_notice((empty,)) or ""

    # Then the established zero-row branch remains and no quota claim is invented
    assert "해당하는 자료를 찾지 못했습니다" in notice
    assert "일일 한도를 초과" not in notice


def test_one_quota_lane_does_not_remove_another_lane_facts() -> None:
    # Given one exhausted lane and one independently grounded lane
    results = (
        SourceResult(
            source="web",
            query="리바로 뉴스",
            status="quota",
            failure_reason="QUOTA_EXCEEDED",
        ),
        SourceResult(
            source="clinicaltrials",
            query="pitavastatin AND ezetimibe",
            status="ok",
            payload={"records": [{"nct_id": "NCT00000001"}]},
        ),
    )

    # When the deterministic shortfall layer explains only the failed lane
    notice = _retrieval_shortfall_notice(results) or ""

    # Then the other lane and its facts remain untouched
    assert "웹 검색은 조회 한도를 초과" in notice
    assert results[1].status == "ok"
    assert results[1].payload["records"][0]["nct_id"] == "NCT00000001"
