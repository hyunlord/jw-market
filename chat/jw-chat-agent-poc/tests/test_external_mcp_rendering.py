from __future__ import annotations

from jw_chat_agent_poc.orchestrator.markdown_renderers import generic_external_md
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.orchestrator.provenance_calls import provenance_rows_from_calls
from jw_chat_agent_poc.tool_use.registry import _external_call_envelope
from jw_chat_agent_poc.tool_use.renderer import render_evidence_answer
from jw_chat_agent_poc.tools.external import ExternalCall
from jw_chat_agent_poc.tools.external.client import (
    _mcp_external_call,
    _mcp_payload,
    _mcp_tool_spec,
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


def test_openfda_adverse_event_request_uses_dedicated_live_tool() -> None:
    spec = _mcp_tool_spec(
        "openfda_label_search",
        {
            "search": 'openfda.substance_name:"PITAVASTATIN"',
            "evidence_type": "adverse_event",
        },
    )

    assert spec["mcp_tool"] == "search_drug_adverse_events"
    assert spec["arguments"] == {
        "generic_name": "PITAVASTATIN",
        "limit": 5,
        "sort": "receivedate:desc",
    }


def test_openfda_adverse_event_text_becomes_public_evidence() -> None:
    result = McpToolResult(
        content_text=(
            "total_results: 10054\n"
            "adverse_events[1]:\n"
            '  - safety_report_id: "26558911"\n'
            "    report_date: 2026-03-31\n"
            "    serious: Yes\n"
            "    country: TW\n"
            "    drugs[1]:\n"
            "      - name: LIVALO\n"
            "        characterization: concomitant\n"
            "    reactions[2]{term,outcome}:\n"
            "      Myalgia,Recovered\n"
            "      Dizziness,Unknown\n"
        ),
        raw_result={"content": []},
    )

    call = _mcp_external_call(
        "openfda_label_search",
        "openfda_mcp",
        {"search": 'openfda.substance_name:"PITAVASTATIN"', "evidence_type": "adverse_event"},
        "search_drug_adverse_events",
        result,
        "http://gateway/mcp/184/mcp",
        123.4,
    )
    envelope = _external_call_envelope(call, "pitavastatin", "FAERS 자발보고 내 이상반응")
    answer = render_evidence_answer(envelope.evidence)

    assert call.status == "live"
    assert call.render_data["payload"]["meta"]["results"]["total"] == 10054
    assert call.render_data["payload"]["results"][0]["reaction_terms"] == ["Myalgia", "Dizziness"]
    assert envelope.ok is True
    assert envelope.evidence[0].source_name == "FDA 이상반응 보고 정보"
    assert envelope.evidence[0].metric == "FAERS 자발보고 내 이상반응"
    assert "FAERS 보고 26558911" in str(envelope.evidence[0].source_locator)
    assert "보고 반응: Myalgia, Dizziness" in str(envelope.evidence[0].source_locator)
    assert "Myalgia" in str(envelope.evidence[0].source_locator)
    assert "인과관계를 입증하지 않습니다" in answer
    assert "pitavastatin: 부작용 =" not in answer


def test_openfda_adverse_event_preserves_structured_mcp_results() -> None:
    result = McpToolResult(
        content_text="",
        raw_result={
            "structuredContent": {
                "result": {
                    "meta": {"results": {"total": 1}},
                    "results": [
                        {
                            "safety_report_id": "30000001",
                            "date": "2026-04-01",
                            "reaction_terms": ["Headache"],
                        }
                    ],
                }
            }
        },
    )

    call = _mcp_external_call(
        "openfda_label_search",
        "openfda_mcp",
        {"search": 'openfda.substance_name:"PITAVASTATIN"', "evidence_type": "adverse_event"},
        "search_drug_adverse_events",
        result,
        "http://gateway/mcp/184/mcp",
        10.0,
    )
    envelope = _external_call_envelope(call, "pitavastatin", "FAERS 자발보고 내 이상반응")

    assert call.status == "live"
    assert call.render_data["payload"]["results"][0]["safety_report_id"] == "30000001"
    assert envelope.ok is True
    assert "FAERS 보고 30000001" in str(envelope.evidence[0].source_locator)
    assert "보고 반응: Headache" in str(envelope.evidence[0].source_locator)


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
    assert envelope.evidence[0].source_locator == "[2026 고지혈증 진료지침](https://example.test/guideline)"


def test_patent_evidence_keeps_expiry_status_and_owner() -> None:
    call = ExternalCall(
        tool="mfds_patent",
        source="nedrug_mcp",
        status="live",
        summary_text="특허 1건",
        render_data={
            "items": [
                {
                    "GOODS_NAME": "리바로정",
                    "DOMESTIC_PATENT_NO": "10-1234567",
                    "DOMESTIC_END_DATE": "2028-05-17",
                    "DOMESTIC_PATENT_STATUS": "존속",
                    "PATENTEE": "KOWA",
                }
            ]
        },
    )

    envelope = _external_call_envelope(call, "pitavastatin", "국내 특허")
    locator = str(envelope.evidence[0].source_locator)

    assert "2028-05-17" in locator
    assert "존속" in locator
    assert "KOWA" in locator


def test_requested_source_unavailable_does_not_claim_ubist_provenance() -> None:
    rows = provenance_rows_from_calls(
        [
            {
                "tool": "requested_source_unavailable",
                "source": "cache",
                "render_data": {"requested_source": "KOL", "status": "unsupported"},
            }
        ],
        ["cache"],
    )

    assert rows[0].source == "지원 범위"
    assert all(row.source != "UBIST" for row in rows)


def test_patent_ingredient_alias_requires_a_token_boundary() -> None:
    assert resolve_patent_ingredient_query("pitavastatin 특허") == "Pitavastatin"
    assert resolve_patent_ingredient_query("pitavastatin-calcium 특허") == "Pitavastatin"
    assert resolve_patent_ingredient_query("notpitavastatin 특허") is None
    assert resolve_patent_ingredient_query("피타바스타틴유사 특허") is None
