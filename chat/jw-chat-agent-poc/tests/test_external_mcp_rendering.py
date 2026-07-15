from __future__ import annotations

from jw_chat_agent_poc.orchestrator.markdown_renderers import generic_external_md
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.orchestrator.provenance_calls import provenance_rows_from_calls
from jw_chat_agent_poc.tool_use.registry import _external_call_envelope
from jw_chat_agent_poc.tool_use.registry import ExternalToolRegistry
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tool_use.renderer import render_evidence_answer
from jw_chat_agent_poc.tools.external import ExternalCall
from jw_chat_agent_poc.tools.external import ExternalApiClient
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


def test_mcp_specs_use_verified_service_defaults_and_direct_urls(monkeypatch) -> None:
    monkeypatch.delenv("CLINICAL_TRIALS_MCP_RESOURCE_ID", raising=False)
    monkeypatch.delenv("OPENFDA_MCP_RESOURCE_ID", raising=False)
    monkeypatch.delenv("HIRA_MCP_RESOURCE_ID", raising=False)
    monkeypatch.delenv("NEDRUG_MCP_RESOURCE_ID", raising=False)
    monkeypatch.setenv("CLINICAL_TRIALS_MCP_URL", "http://code-serving-112:8080/json")
    monkeypatch.setenv("OPENFDA_MCP_URL", "http://code-serving-127:8080/json")
    monkeypatch.setenv("HIRA_MCP_URL", "http://code-serving-190:8080/json")
    monkeypatch.setenv("NEDRUG_MCP_URL", "http://code-serving-196:8080/json")

    clinical = _mcp_tool_spec("clinicaltrials_v2_search", {"query.intr": "pitavastatin"})
    openfda = _mcp_tool_spec("openfda_label_search", {"search": 'openfda.substance_name:"PITAVASTATIN"'})
    hira = _mcp_tool_spec("hira_disease_name_code", {"sickCd": "고지혈증"})
    nedrug = _mcp_tool_spec("mfds_permission_search", {"brand": "리바로"})

    assert (clinical["resource_id"], clinical["mcp_tool"]) == ("112", "search_studies")
    assert (openfda["resource_id"], openfda["mcp_tool"]) == ("127", "search_drug_labels")
    assert (hira["resource_id"], hira["mcp_tool"]) == ("190", "search_disease_code")
    assert (nedrug["resource_id"], nedrug["mcp_tool"]) == ("196", "search_drug_permission_list")


def test_main_ingredient_spec_uses_mart_then_mfds_fallback_method(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")
    calls: list[str] = []

    def fake_main_ingredient(brand: str) -> ExternalCall:
        calls.append(brand)
        return ExternalCall(
            tool="mfds_main_ingredient",
            source="nedrug_mcp",
            status="live",
            summary_text="주성분 확인",
            render_data={"items": [{"MTRAL_NM": "TIRZEPATIDE"}]},
        )

    monkeypatch.setattr(external, "mfds_main_ingredient", fake_main_ingredient)
    registry = ExternalToolRegistry(resolver=BrandResolver(), external=external)
    spec = next(item for item in registry.list_for_query("주성분") if item.name == "get_drug_main_ingredient")

    envelope = spec.execute(spec.input_model.model_validate({"brand": "마운자로"}))

    assert calls == ["마운자로"]
    assert envelope.ok is True
    assert envelope.evidence[0].source_locator == "TIRZEPATIDE"


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
            "        generic_name: PITAVASTATIN CALCIUM\n"
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


def test_openfda_adverse_event_rejects_reports_for_a_different_drug() -> None:
    result = McpToolResult(
        content_text=(
            "total_results: 2\n"
            "adverse_events[2]:\n"
            '  - safety_report_id: "unrelated-report"\n'
            "    report_date: 2026-03-31\n"
            "    drugs[1]:\n"
            "      - name: TYLENOL\n"
            "        generic_name: ACETAMINOPHEN\n"
            "    reactions[1]{term,outcome}:\n"
            "      Headache,Unknown\n"
            '  - safety_report_id: "pitavastatin-report"\n'
            "    report_date: 2026-03-30\n"
            "    drugs[1]:\n"
            "      - name: LIVALO\n"
            "        generic_name: PITAVASTATIN CALCIUM\n"
            "    reactions[1]{term,outcome}:\n"
            "      Myalgia,Recovered\n"
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
        10.0,
    )
    envelope = _external_call_envelope(call, "pitavastatin", "FAERS 자발보고 내 이상반응")

    assert envelope.ok is True
    assert len(envelope.evidence) == 1
    assert "pitavastatin-report" in str(envelope.evidence[0].source_locator)
    assert all("unrelated-report" not in str(fact.source_locator) for fact in envelope.evidence)


def test_openfda_adverse_event_accepts_indexed_generic_name_fields() -> None:
    result = McpToolResult(
        content_text=(
            "total_results: 1\n"
            "adverse_events[1]:\n"
            '  - safety_report_id: "pitavastatin-indexed"\n'
            "    report_date: 2026-03-30\n"
            "    drugs[1]:\n"
            "      - name: LIVALO\n"
            "        generic_name[1]: PITAVASTATIN CALCIUM\n"
            "    reactions[1]{term,outcome}:\n"
            "      Myalgia,Recovered\n"
        ),
        raw_result={"content": []},
    )

    call = _mcp_external_call(
        "openfda_label_search",
        "openfda_mcp",
        {'search': 'openfda.substance_name:"PITAVASTATIN"', "evidence_type": "adverse_event"},
        "search_drug_adverse_events",
        result,
        "http://gateway/mcp/184/mcp",
        10.0,
    )
    envelope = _external_call_envelope(call, "pitavastatin", "FAERS 자발보고 내 이상반응")

    assert envelope.ok is True
    assert len(envelope.evidence) == 1
    assert "pitavastatin-indexed" in str(envelope.evidence[0].source_locator)


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
                            "patient": {
                                "drug": [
                                    {
                                        "medicinalproduct": "LIVALO",
                                        "openfda": {"generic_name": ["PITAVASTATIN CALCIUM"]},
                                    }
                                ]
                            },
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


def test_clinicaltrials_evidence_keeps_nct_identifier_title_and_url() -> None:
    call = ExternalCall(
        tool="clinicaltrials_v2_search",
        source="clinicaltrials_mcp",
        status="live",
        summary_text="임상시험 1건",
        render_data={
            "payload": {
                "studies": [
                    {
                        "NCTId": "NCT01234567",
                        "briefTitle": "Pitavastatin Cardiovascular Outcomes Study",
                        "url": "https://clinicaltrials.gov/study/NCT01234567",
                    }
                ]
            }
        },
    )

    envelope = _external_call_envelope(call, "pitavastatin", "글로벌 임상시험")
    locator = str(envelope.evidence[0].source_locator)

    assert "NCT01234567" in locator
    assert "Pitavastatin Cardiovascular Outcomes Study" in locator
    assert "https://clinicaltrials.gov/study/NCT01234567" in locator


def test_clinicaltrials_text_parser_accepts_canonical_field_names() -> None:
    result = McpToolResult(
        content_text=(
            "studies[1]:\n"
            "  - NCTId: NCT07654321\n"
            "    briefTitle: Pitavastatin Outcomes Study\n"
            "    overallStatus: RECRUITING\n"
            "    clinicaltrials_url: https://clinicaltrials.gov/study/NCT07654321\n"
        ),
        raw_result={"content": []},
    )

    call = _mcp_external_call(
        "clinicaltrials_v2_search",
        "clinicaltrials_mcp",
        {"query.intr": "pitavastatin"},
        "search_clinical_trials",
        result,
        "http://gateway/mcp/169/mcp",
        10.0,
    )
    envelope = _external_call_envelope(call, "pitavastatin", "글로벌 임상시험")
    locator = str(envelope.evidence[0].source_locator)

    assert envelope.ok is True
    assert "NCT07654321" in locator
    assert "Pitavastatin Outcomes Study" in locator
    assert "https://clinicaltrials.gov/study/NCT07654321" in locator


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
