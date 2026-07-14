from __future__ import annotations

from jw_chat_agent_poc.orchestrator.markdown_renderers import generic_external_md
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.tool_use.registry import _external_call_envelope
from jw_chat_agent_poc.tools.external import ExternalCall
from jw_chat_agent_poc.tools.external.client import (
    _mcp_payload,
    resolve_patent_ingredient_query,
)
from jw_chat_agent_poc.tools.external.mcp_client import McpToolResult


def test_generic_external_renderer_projects_item_evidence_without_provider_envelope() -> None:
    # Given: a provider envelope contains one useful public item and transport scalars.
    data = {
        "resultCode": "00",
        "totalCount": 10,
        "items": [
            {
                "ITEM_SEQ": "internal-row-id",
                "ITEM_NAME": "리바로정1밀리그램",
                "ITEM_INGR_NAME": "Pitavastatin Calcium Hydrate",
            }
        ],
    }

    # When: the legacy renderer projects external evidence.
    markdown = generic_external_md("mfds_permission_search", data)

    # Then: useful item evidence is visible while raw envelope and row IDs stay private.
    assert "리바로정1밀리그램" in markdown
    assert "Pitavastatin Calcium Hydrate" in markdown
    assert "resultCode" not in markdown
    assert "totalCount" not in markdown
    assert "ITEM_SEQ" not in markdown
    assert "internal-row-id" not in markdown


def test_mfds_permission_provenance_is_not_labeled_as_patent() -> None:
    # Given: a permission-search call comes from the shared NeDrug MCP resource.
    call = {
        "tool": "mfds_permission_search",
        "source": "nedrug_mcp",
        "status": "live",
        "summary_text": "식약처 허가 품목을 확인했습니다.",
        "render_data": {
            "items": [{"ITEM_NAME": "리바로정1밀리그램"}],
        },
    }

    # When: the public answer and provenance block are assembled.
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[call],
        sources=["nedrug_mcp"],
    )

    # Then: the public source reflects the permission tool rather than the MCP host label.
    assert "식약처 의약품 허가 정보" in response.sources_md
    assert "식약처 의약품 특허 정보" not in response.sources_md


def test_mcp_payload_preserves_structured_object_result() -> None:
    result = McpToolResult(
        content_text="fallback text",
        raw_result={
            "structuredContent": {
                "result": {"results": [{"title": "고지혈증 진료지침"}]},
            }
        },
    )

    assert _mcp_payload(result) == {
        "results": [{"title": "고지혈증 진료지침"}],
    }


def test_mcp_payload_preserves_structured_list_result() -> None:
    result = McpToolResult(
        content_text="fallback text",
        raw_result={
            "structuredContent": {
                "result": [{"title": "Pitavastatin label"}],
            }
        },
    )

    assert _mcp_payload(result) == [{"title": "Pitavastatin label"}]


def test_payload_results_are_promoted_to_external_evidence() -> None:
    call = ExternalCall(
        tool="web_search",
        source="web_search",
        status="live",
        summary_text="검색 결과 1건",
        render_data={
            "payload": {
                "results": [
                    {
                        "title": "2026 고지혈증 진료지침",
                        "url": "https://example.test/guideline",
                    }
                ]
            }
        },
    )

    envelope = _external_call_envelope(call, "고지혈증", "웹 검색")

    assert envelope.ok is True
    assert len(envelope.evidence) == 1
    assert envelope.evidence[0].source_locator == "2026 고지혈증 진료지침"


def test_patent_ingredient_alias_requires_a_token_boundary() -> None:
    assert resolve_patent_ingredient_query("pitavastatin 특허") == "Pitavastatin"
    assert resolve_patent_ingredient_query("pitavastatin-calcium 특허") == "Pitavastatin"
    assert resolve_patent_ingredient_query("notpitavastatin 특허") is None
    assert resolve_patent_ingredient_query("피타바스타틴유사 특허") is None
