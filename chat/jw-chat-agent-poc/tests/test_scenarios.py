from __future__ import annotations

import json
from pathlib import Path
import os

import pytest
import requests

from jw_chat_agent_poc import ChatAgent
from jw_chat_agent_poc.orchestrator.agent import HIRA_DISEASE_MAPPINGS
from jw_chat_agent_poc.rag import LocalDocumentRag
from jw_chat_agent_poc.router import BQRouter
from jw_chat_agent_poc.tools.external import ExternalApiClient


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "jw_chat_agent_poc" / "fixtures"


class _McpResponse:
    def __init__(self, event: dict) -> None:
        self.text = f"event: message\ndata: {json.dumps(event)}\n\n"

    def raise_for_status(self) -> None:
        return None


def test_q1_single_metric_market_size_growth():
    result = ChatAgent().answer("리바로 시장 규모랑 성장 추이는?")
    assert "Q1" in {row["bq"] for row in result["decomposition"]}
    assert "cache" in result["sources"]
    assert "시장" in result["answer"]
    assert any(call.get("tool") == "get_market_landscape" for call in result["tool_calls"])


def test_hira_disease_question_routes_to_external_disease_stats_without_metrics():
    result = ChatAgent().answer("이상지질혈증 환자 통계")

    assert result["sources"] == ["hira_disease"]
    assert result["decomposition"][0]["bq"] == "Q1"
    assert result["decomposition"][0]["sources"] == ("external_api",)
    tools = {call.get("tool") for call in result["tool_calls"]}
    assert "hira_disease_mapping" in tools
    assert "hira_disease_name_code" in tools
    assert "hira_disease_hospitalization_outpatient_stats" in tools
    assert "hira_disease_gender_age_stats" in tools
    assert "hira_disease_institution_class_stats" in tools
    assert "get_brand_metric" not in tools
    mapping = next(call for call in result["tool_calls"] if call.get("tool") == "hira_disease_mapping")
    assert mapping["render_data"]["sickCd"] == "E78"
    assert "지질단백질대사장애" in result["answer"]


def test_hira_disease_trend_requests_five_distinct_years() -> None:
    result = ChatAgent().answer("고지혈증 환자수 추이")

    calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "hira_disease_hospitalization_outpatient_stats"
    ]
    assert [call["render_data"]["request"]["year"] for call in calls] == ["2020", "2021", "2022", "2023", "2024"]
    assert all(call.get("tool") != "hira_disease_gender_age_stats" for call in result["tool_calls"])


@pytest.mark.parametrize("question", ["이상지질혈증 환자통계", "이상지질혈증 환자분포"])
def test_hira_disease_question_accepts_compact_patient_stat_spacing(question):
    result = ChatAgent().answer(question)

    assert result["sources"] == ["hira_disease"]
    mapping = next(call for call in result["tool_calls"] if call.get("tool") == "hira_disease_mapping")
    assert mapping["render_data"]["sickCd"] == "E78"


def test_brand_related_hira_disease_question_uses_confirmed_kcd_mapping():
    result = ChatAgent().answer("리바로 관련 질병 환자수")

    tools = [call.get("tool") for call in result["tool_calls"]]
    assert tools[:2] == ["hira_disease_mapping", "hira_disease_name_code"]
    assert "hira_disease_area_stats" in tools
    assert result["tool_calls"][0]["render_data"]["sickCd"] == "E78"
    assert result["sources"] == ["hira_disease"]


@pytest.mark.parametrize(
    ("question", "expected_sick_cd"),
    [
        ("라베칸 관련 질병 환자수", "K21"),
        ("가드렛 관련 질병 환자수", "E11"),
        ("시그마트 관련 질병 환자수", "I20"),
        ("트루패스 관련 질병 환자수", "N40"),
        ("뉴트로진 관련 질병 환자수", "D70"),
    ],
)
def test_brand_related_hira_disease_question_uses_new_verified_kcd_mapping(question, expected_sick_cd):
    result = ChatAgent().answer(question)

    mapping = result["tool_calls"][0]
    assert mapping["tool"] == "hira_disease_mapping"
    assert mapping["render_data"]["sickCd"] == expected_sick_cd
    for call in result["tool_calls"][1:]:
        request = call.get("render_data", {}).get("request", {})
        assert request.get("sickCd") == expected_sick_cd
    assert "MFDS 효능효과" in mapping["render_data"]["basis"]
    assert result["sources"] == ["hira_disease"]


def test_hira_confirmed_mapping_coverage_is_nineteen_brands():
    assert len(HIRA_DISEASE_MAPPINGS) == 19


@pytest.mark.parametrize(
    ("question", "expected_sick_cds"),
    [
        ("리바로하이 관련 질병 환자수", ["I10", "E78"]),
        ("리바로하이 이상지질혈증 환자수", ["I10", "E78"]),
        ("리바로브이 관련 질병 환자수", ["E78", "I10"]),
        ("리바로브이 관련 질병", ["E78", "I10"]),
        ("리바로브이 고혈압 환자수", ["E78", "I10"]),
    ],
)
def test_hira_disease_question_supports_multiple_co_primary_kcds(question, expected_sick_cds):
    result = ChatAgent().answer(question)

    mapping_calls = [call for call in result["tool_calls"] if call.get("tool") == "hira_disease_mapping"]
    assert [call["render_data"]["sickCd"] for call in mapping_calls] == expected_sick_cds
    assert all(call["render_data"]["mapping_total"] == 2 for call in mapping_calls)
    request_sick_cds = [
        call.get("render_data", {}).get("request", {}).get("sickCd")
        for call in result["tool_calls"]
        if call.get("tool", "").startswith("hira_disease_") and call.get("tool") != "hira_disease_mapping"
    ]
    assert set(request_sick_cds) == set(expected_sick_cds)
    assert result["sources"] == ["hira_disease"]


@pytest.mark.parametrize(
    "question",
    [
        "제이클 관련 질병 환자수",
        "위너프 관련 질병 환자수",
        "위너프A+ 관련 질병 환자수",
        "엔커버 관련 질병 환자수",
        "모빌리아 관련 질병 환자수",
    ],
)
def test_hira_disease_question_marks_unsuitable_brands_explicitly(question):
    result = ChatAgent().answer(question)

    assert result["sources"] == ["hira_disease"]
    assert result["tool_calls"][0]["tool"] == "hira_disease_mapping_unsuitable"
    assert result["tool_calls"][0]["status"] == "unsupported"
    assert "질병 유병 통계 조회가 부적합" in result["answer"]
    assert all(call.get("tool") != "hira_disease_name_code" for call in result["tool_calls"])


def test_hira_disease_question_with_unconfirmed_brand_mapping_gracefully_stops():
    result = ChatAgent().answer("플라주오피 관련 질병 환자수")

    assert result["sources"] == ["hira_disease"]
    assert result["tool_calls"][0]["tool"] == "hira_disease_mapping_unresolved"
    assert result["tool_calls"][0]["status"] == "unsupported"
    assert "대표 질병 KCD 매핑이 아직 확정되지 않아" in result["answer"]


def test_sales_question_still_uses_metrics_not_hira():
    result = ChatAgent().answer("리바로 매출")

    assert "cache" in result["sources"]
    assert "hira_disease" not in result["sources"]
    assert any(call.get("tool") == "get_brand_metric" for call in result["tool_calls"])


def test_q2_competitive_and_clinical_routes_metrics_and_ct():
    result = ChatAgent().answer("리바로 경쟁 상황이랑 임상 현황?")
    bqs = {row["bq"] for row in result["decomposition"]}
    assert {"Q2", "Q2.5"}.issubset(bqs)
    tools = {call.get("tool") for call in result["tool_calls"]}
    assert "get_brand_metric" in tools
    assert "clinicaltrials_v2_search" in tools
    assert "external_api" in result["sources"]
    assert "pitavastatin 성분 기준 동향" in result["answer"]
    assert "특정 제품에 한정되지 않음" in result["answer"]


def test_combo_clinical_uses_and_query_and_separates_reference_results():
    result = ChatAgent().answer("리바로젯 임상")

    ct_calls = [call for call in result["tool_calls"] if call.get("tool") == "clinicaltrials_v2_search"]
    assert ct_calls[0]["render_data"]["request"]["query.intr"] == "ezetimibe AND pitavastatin"
    assert ct_calls[0]["render_data"]["match_scope"] == "combo_and"
    assert "복합제 조합 임상" in result["answer"]
    assert "성분별 참고" in result["answer"]
    assert "\n\n## 주의\n- 리바로젯 임상은" in result["answer"]
    assert "유의해야 합니다.에" not in result["answer"]


def test_cortellis_requested_source_trap_keeps_clinicaltrials_as_alternate_reference():
    result = ChatAgent().answer("Cortellis 기준 이상지질혈증 파이프라인과 리바로 경쟁 임상 현황을 분석해줘")

    assert result["answer"].startswith("Cortellis 데이터는 현재 운영 데이터에 미보유입니다.")
    assert "Cortellis 기준" not in result["answer"]
    assert "### 대체 참고" in result["answer"]
    assert "ClinicalTrials/MFDS 결과는 Cortellis 데이터가 아니므로 요청 소스 결론으로 승격하지 않습니다." in result["answer"]
    assert "적응증 확장 가능성" not in result["answer"]
    assert "상업 경쟁 압력" not in result["answer"]
    assert result["tool_calls"][0]["tool"] == "requested_source_unavailable"


def test_requested_source_trap_short_circuits_kol_and_nccn_before_web_or_news_tools():
    for question, first_sentence in (
        ("리바로 KOL 자문 기준 처방 의견과 시장 시사점을 알려줘", "KOL 자문 데이터는 현재 운영 데이터에 미보유입니다."),
        ("리바로 NCCN 치료 지침 기준 시장 영향을 알려줘", "NCCN/가이드라인 데이터는 현재 운영 데이터에 미보유입니다."),
    ):
        result = ChatAgent().answer(question)

        assert result["answer"].startswith(first_sentence)
        assert "### 미보유 데이터 처리" in result["answer"]
        assert [call.get("tool") for call in result["tool_calls"]] == ["requested_source_unavailable"]


def test_nutrition_infusion_electrolyte_external_clinical_is_marked_inapplicable():
    result = ChatAgent().answer("플라주오피 임상")

    assert result["tool_calls"][0]["tool"] == "external_api_inapplicable"
    assert result["tool_calls"][0]["render_data"]["reason"] == "nutrition_infusion_electrolyte_false_positive_risk"
    assert "영양/수액/전해질 제제" in result["answer"]
    assert "임상·특허 조회가 부적합" in result["answer"]


def test_q2_combo_fda_label_and_patent_splits_combo_ingredients():
    result = ChatAgent().answer("리바로젯 FDA 라벨·특허?")
    assert result["resolution"]["is_combo"] is True
    assert result["resolution"]["molecule_en"] == ("ezetimibe", "pitavastatin")
    tools = [call.get("tool") for call in result["tool_calls"]]
    assert tools.count("openfda_label_search") == 2
    assert tools.count("mfds_patent") == 2
    assert tools.count("mfds_fda_orangebook") == 2
    assert "국내 식약처/특허는 리바로젯 제품 기준" in result["answer"]
    assert "해외 FDA/OpenFDA/Orange Book은 ezetimibe, pitavastatin 성분 기준" in result["answer"]
    assert "\n\n## 주의\n- 국내 식약처/특허는" in result["answer"]
    assert "Book 자료는" not in result["answer"]
    assert "Orange Book" in result["answer"]


def test_document_rag_upload_search_and_citation():
    result = ChatAgent().answer(
        "이 가이드라인에서 1차 치료제는?",
        documents=[FIXTURE_DIR / "guideline_mock.txt"],
    )
    assert "document" in result["sources"]
    rag_call = next(call for call in result["tool_calls"] if call.get("tool") == "document_rag")
    assert rag_call["chunks"][0]["document"] == "guideline_mock.txt"
    assert "first-line" in rag_call["summary_text"]


def test_no_data_boundary_for_sales_impact():
    result = ChatAgent().answer("리바로 영업활동 Impact는?")
    assert result["sources"] == ["cache"]
    assert "현재 데이터로 답변 불가" in result["answer"]
    assert [call["tool"] for call in result["tool_calls"]] == ["get_brand_metric"]
    assert "84.93" in result["answer"]


def test_mixed_structured_and_document_sources_require_explicit_market_anchor():
    result = ChatAgent().answer(
        "업로드한 시장 전망이랑 실제 우리 점유율 비교",
        documents=[FIXTURE_DIR / "datamonitor_mock.txt"],
    )
    assert result["tool_calls"] == []
    assert "브랜드 또는 시장을 지정" in result["answer"]


def test_structured_upload_guard_rejects_csv(tmp_path):
    csv = tmp_path / "structured.csv"
    csv.write_text("period,value\\n2026-01,1\\n", encoding="utf-8")
    with pytest.raises(ValueError, match="정형 통계 업로드는 거부"):
        LocalDocumentRag().search("시장 전망", [csv])


def test_router_uses_provided_boundary_without_expanding_bq_map():
    route = BQRouter().route("리바로 포트폴리오 사업성은?")
    assert route[0].bq == "Q5"
    assert route[0].sources == ("none",)


def test_router_routes_forecast_without_documents_to_no_data_boundary():
    route = BQRouter().route("리바로의 향후 시장 규모와 매출을 예측해줘")

    assert route[0].bq == "Q1"
    assert route[0].sources == ("none",)
    assert "forecast" in route[0].reason


def test_router_keeps_uploaded_forecast_questions_on_document_route():
    routes = BQRouter().route("업로드한 시장 전망이랑 실제 우리 점유율 비교", has_documents=True)

    sources = {source for route in routes for source in route.sources}
    assert "document" in sources
    assert "metrics" in sources


def test_external_redaction_masks_service_key():
    url = "https://example.test/api?" + "serviceKey=X&x=1"
    assert ExternalApiClient.redact_url(url) == "https://example.test/api?serviceKey=<redacted>&x=1"


def test_live_error_redacts_service_key_in_summary_and_render_data(monkeypatch):
    monkeypatch.setenv("DATA_GO_KR_KEY", "SECRETKEY")

    def fake_get(url, timeout):
        class Response:
            status_code = 503
            text = ""

            def raise_for_status(self):
                raise requests.HTTPError(f"503 Server Error for url: {url}")

        return Response()

    monkeypatch.setattr("jw_chat_agent_poc.tools.external.client.requests.get", fake_get)

    call = ExternalApiClient(mode="live", timeout_s=1).mfds_patent("pitavastatin")

    assert call.status == "error"
    assert "SECRETKEY" not in call.summary_text
    assert "SECRETKEY" not in call.render_data["error"]
    assert "serviceKey=<redacted>" in call.summary_text
    assert "serviceKey=<redacted>" in call.render_data["error"]


def test_live_hira_response_preserves_request_year(monkeypatch):
    monkeypatch.setenv("DATA_GO_KR_KEY", "SECRETKEY")

    def fake_get(url, timeout):
        assert "year=2024" in url

        class Response:
            status_code = 200
            text = (
                "<response>"
                "<header><resultCode>00</resultCode></header>"
                "<body><items><item>"
                "<inpatOpat>외래</inpatOpat>"
                "<sickCd>I10</sickCd>"
                "<sickNm>본태성 고혈압</sickNm>"
                "<ptntCnt>3769201</ptntCnt>"
                "</item></items><totalCount>1</totalCount></body>"
                "</response>"
            )

            def raise_for_status(self):
                return None

        return Response()

    monkeypatch.setattr("jw_chat_agent_poc.tools.external.client.requests.get", fake_get)

    call = ExternalApiClient(mode="live", timeout_s=1).hira_disease_hospitalization_outpatient_stats("I10")

    assert call.status == "live"
    assert call.render_data["request"] == {"sickCd": "I10", "year": "2024"}
    assert call.render_data["items"][0]["ptntCnt"] == "3769201"


def test_tavily_web_search_uses_five_second_timeout_and_caps_results(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "SECRETKEY")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")

    captured: dict[str, object] = {}

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "results": [
                        {
                            "title": f"title-{index}",
                            "url": f"https://example.test/{index}",
                            "content": f"snippet-{index}",
                            "published_date": f"2026-07-{index + 1:02d}",
                        }
                        for index in range(7)
                    ]
                }

        return Response()

    monkeypatch.setattr("jw_chat_agent_poc.tools.external.client.requests.post", fake_post)

    call = ExternalApiClient(mode="live", timeout_s=12).web_search("리바로 pitavastatin 제약", max_results=9)

    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["timeout"] == 5
    assert captured["json"] == {
        "query": "리바로 pitavastatin 제약",
        "max_results": 5,
        "search_depth": "basic",
        "include_answer": False,
        "topic": "general",
    }
    assert call.status == "live"
    assert len(call.render_data["items"]) == 5
    assert call.render_data["items"][-1]["url"] == "https://example.test/4"
    assert call.render_data["items"][-1]["published_date"] == "2026-07-05"


def test_tavily_news_search_requests_provider_dates(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "SECRETKEY")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")
    captured: dict[str, object] = {}

    def fake_post(url, headers, json, timeout):
        captured["json"] = json

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"results": []}

        return Response()

    monkeypatch.setattr("jw_chat_agent_poc.tools.external.client.requests.post", fake_post)

    ExternalApiClient(mode="live", timeout_s=12).web_search(
        "최신 고지혈증 가이드라인",
        topic="news",
    )

    assert captured["json"]["topic"] == "news"


def test_tavily_web_search_timeout_is_graceful(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "SECRETKEY")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")

    def fake_post(url, headers, json, timeout):
        raise requests.Timeout("simulated web timeout")

    monkeypatch.setattr("jw_chat_agent_poc.tools.external.client.requests.post", fake_post)

    call = ExternalApiClient(mode="live", timeout_s=12).web_search("리바로 pitavastatin 제약")

    assert call.status == "error"
    assert call.render_data["items"] == []
    assert call.render_data["external_claim_policy"] == "web_results_unverified"
    assert "simulated web timeout" in call.summary_text


def test_clinicaltrials_live_search_uses_mcp_text_event_stream(monkeypatch):
    def fake_post(url, json, headers, timeout):
        assert url == "http://ct-mcp/json"
        assert "application/json" in headers["Accept"]
        assert "text/event-stream" in headers["Accept"]
        assert json["method"] == "tools/call"
        return _McpResponse(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "totalCount: 1\n"
                                "studies[1]:\n"
                                '  - clinicaltrials_url: "https://clinicaltrials.gov/study/NCT05537948"\n'
                                "    nctId: NCT05537948\n"
                                "    title: Efficacy and Safety of Pitavastatin\n"
                                "    status: ACTIVE_NOT_RECRUITING\n"
                                "    phase[1]: PHASE4\n"
                                "    studyType: INTERVENTIONAL\n"
                                "    sponsor: Example Sponsor\n"
                                "    interventions[1]{type,name}:\n"
                                "      DRUG,Pitavastatin\n"
                                '    url: "https://clinicaltrials.gov/study/NCT05537948"'
                            ),
                        }
                    ]
                },
            }
        )

    monkeypatch.setenv("CLINICAL_TRIALS_MCP_URL", "http://ct-mcp/json")
    monkeypatch.setattr("jw_chat_agent_poc.tools.external.mcp_client.requests.post", fake_post)

    call = ExternalApiClient(mode="live", timeout_s=1).clinicaltrials_v2_search("pitavastatin")

    assert call.status == "live"
    assert call.render_data["external_claim_policy"] == "source_relay_only"
    study = call.render_data["payload"]["studies"][0]
    assert study["protocolSection"]["identificationModule"]["nctId"] == "NCT05537948"
    assert study["protocolSection"]["armsInterventionsModule"]["interventions"][0]["name"] == "Pitavastatin"


def test_clinicaltrials_live_search_fails_closed_on_mcp_error(monkeypatch):
    def fake_post(url, json, headers, timeout):
        raise requests.Timeout("network down")

    monkeypatch.setenv("CLINICAL_TRIALS_MCP_URL", "http://ct-mcp/json")
    monkeypatch.setattr("jw_chat_agent_poc.tools.external.mcp_client.requests.post", fake_post)

    call = ExternalApiClient(mode="live", timeout_s=1).clinicaltrials_v2_search("pitavastatin")

    assert call.status == "error"
    assert call.render_data["payload"]["studies"] == []
    assert call.render_data["external_claim_policy"] == "fail_closed_error"
    assert "NCT" not in call.summary_text


@pytest.mark.skipif(not os.environ.get("DATA_GO_KR_KEY"), reason="DATA_GO_KR_KEY is required for live external API tests")
def test_live_external_endpoints_parse_real_responses():
    client = ExternalApiClient(mode="live", timeout_s=15)

    permission = client.mfds_permission_search("리바로")
    assert permission.status == "live"
    assert permission.render_data["totalCount"] == "21"
    assert permission.render_data["items"][0]["ITEM_SEQ"] == "200500287"
    assert "serviceKey=<redacted>" in permission.safe_url

    detail = client.mfds_permission_detail("200500287")
    assert detail.status == "live"
    assert detail.render_data["totalCount"] == "1"
    assert detail.render_data["items"][0]["ITEM_NAME"].startswith("리바로정1밀리그램")

    trials = client.clinicaltrials_v2_search("pitavastatin")
    assert trials.status == "live"
    study = trials.render_data["payload"]["studies"][0]
    assert study["protocolSection"]["identificationModule"]["nctId"].startswith("NCT")

    label = client.openfda_label_search("PITAVASTATIN")
    assert label.status == "live"
    assert label.render_data["payload"]["meta"]["results"]["total"] >= 1

    domestic_patent = client.mfds_patent("pitavastatin")
    assert domestic_patent.status == "live"
    assert domestic_patent.render_data["items"][0]["ITEM_NAME"].startswith("리바로")

    orangebook = client.mfds_fda_orangebook("Pitavastatin")
    assert orangebook.status == "live"
    assert orangebook.render_data["items"][0]["INGR_NAME"] == "Pitavastatin Calcium"
