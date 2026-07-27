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
from jw_chat_agent_poc.tools.external import ExternalApiClient, ExternalCall
from jw_chat_agent_poc.tools.external.mcp_client import McpClientError


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "jw_chat_agent_poc" / "fixtures"


class _McpResponse:
    def __init__(self, event: dict) -> None:
        self.text = f"event: message\ndata: {json.dumps(event)}\n\n"

    def raise_for_status(self) -> None:
        return None


def _clinicaltrials_tools_list_response() -> _McpResponse:
    return _McpResponse(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {
                        "name": "search_studies",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "pageSize": {
                                    "type": "number",
                                    "minimum": 1,
                                    "maximum": 20,
                                }
                            },
                        },
                    }
                ]
            },
        }
    )


class _DiseaseSearchExternal(ExternalApiClient):
    def __init__(self) -> None:
        super().__init__(mode="fixture")
        self.name_code_inputs: list[str] = []

    def hira_disease_name_code(self, sick_cd: str) -> ExternalCall:
        self.name_code_inputs.append(sick_cd)
        if sick_cd in {"고지혈증", "이상지질혈증"}:
            return ExternalCall(
                tool="hira_disease_name_code",
                source="hira_disease",
                status="fixture",
                summary_text=f"HIRA search_disease_code에서 {sick_cd} 후보 1건을 확인했습니다.",
                render_data={
                    "totalCount": "1",
                    "items": [
                        {
                            "sickCd": "E78",
                            "sickNm": "지질단백질대사장애 및 기타 지질증",
                        }
                    ],
                    "request": {"searchText": sick_cd, "diseaseType": "SICK_NM"},
                },
            )
        return super().hira_disease_name_code(sick_cd)


def test_q1_single_metric_market_size_growth():
    result = ChatAgent().answer("리바로 시장 규모랑 성장 추이는?")
    assert "Q1" in {row["bq"] for row in result["decomposition"]}
    assert "cache" in result["sources"]
    assert "시장" in result["answer"]
    assert any(call.get("tool") == "get_market_landscape" for call in result["tool_calls"])


def test_hira_disease_question_routes_to_external_disease_stats_without_metrics():
    result = ChatAgent(external=_DiseaseSearchExternal()).answer("이상지질혈증 환자 통계")

    assert result["sources"] == ["hira_disease"]
    assert result["decomposition"][0]["bq"] == "Q1"
    assert result["decomposition"][0]["sources"] == ("external_api",)
    tools = {call.get("tool") for call in result["tool_calls"]}
    assert "hira_disease_name_code" in tools
    assert "hira_disease_hospitalization_outpatient_stats" in tools
    assert "hira_disease_gender_age_stats" in tools
    assert "hira_disease_institution_class_stats" in tools
    assert "get_brand_metric" not in tools
    search = next(call for call in result["tool_calls"] if call.get("tool") == "hira_disease_name_code")
    assert search["render_data"]["resolved_sickCd"] == "E78"
    assert "지질단백질대사장애" in result["answer"]


def test_direct_disease_name_searches_hira_code_before_stats() -> None:
    external = _DiseaseSearchExternal()
    result = ChatAgent(external=external).answer("고지혈증 환자수")

    assert external.name_code_inputs == ["고지혈증"]
    assert result["sources"] == ["hira_disease"]
    tools = [call.get("tool") for call in result["tool_calls"]]
    assert tools[:2] == ["hira_disease_name_code", "hira_disease_hospitalization_outpatient_stats"]
    assert all(
        call.get("render_data", {}).get("request", {}).get("sickCd") == "E78"
        for call in result["tool_calls"]
        if call.get("tool") == "hira_disease_hospitalization_outpatient_stats"
    )


def test_fixture_hira_disease_name_code_does_not_map_disease_names_to_codes() -> None:
    external = ExternalApiClient(mode="fixture")

    name_call = external.hira_disease_name_code("고지혈증")
    code_call = external.hira_disease_name_code("E78")

    assert name_call.status == "no_data"
    assert name_call.render_data["items"] == []
    assert name_call.render_data["request"]["diseaseType"] == "SICK_NM"
    assert code_call.status == "fixture"
    assert code_call.render_data["items"][0]["sickCd"] == "E78"
    assert code_call.render_data["request"]["diseaseType"] == "SICK_CD"


def test_direct_disease_name_ambiguity_stops_before_stats() -> None:
    class _AmbiguousDiseaseSearchExternal(ExternalApiClient):
        def hira_disease_name_code(self, sick_cd: str) -> ExternalCall:
            return ExternalCall(
                tool="hira_disease_name_code",
                source="hira_disease",
                status="fixture",
                summary_text="HIRA search_disease_code에서 당뇨병 후보 여러 건을 확인했습니다.",
                render_data={
                    "totalCount": "2",
                    "items": [
                        {"sickCd": "E10", "sickNm": "1형 당뇨병"},
                        {"sickCd": "E11", "sickNm": "2형 당뇨병"},
                    ],
                    "request": {"searchText": sick_cd, "diseaseType": "SICK_NM"},
                },
            )

    result = ChatAgent(external=_AmbiguousDiseaseSearchExternal()).answer("당뇨병 환자수")

    assert result["sources"] == ["hira_disease"]
    assert result["tool_calls"][0]["tool"] == "hira_disease_code_ambiguous"
    assert result["tool_calls"][0]["status"] == "ambiguous"
    assert result["tool_calls"][0]["render_data"]["candidates"] == [
        {"sickCd": "E10", "sickNm": "1형 당뇨병"},
        {"sickCd": "E11", "sickNm": "2형 당뇨병"},
    ]
    assert all("stats" not in str(call.get("tool")) for call in result["tool_calls"])
    assert "E10" in result["answer"]
    assert "1형 당뇨병" in result["answer"]
    assert "E11" in result["answer"]
    assert "2형 당뇨병" in result["answer"]
    assert "어느 것으로 조회할까요" in result["answer"]
    assert all("ptntCnt" not in str(call.get("render_data")) for call in result["tool_calls"])


def test_diabetic_retinopathy_name_never_broadens_to_type2_diabetes() -> None:
    class _RetinopathySearchExternal(ExternalApiClient):
        def hira_disease_name_code(self, sick_cd: str) -> ExternalCall:
            return ExternalCall(
                tool="hira_disease_name_code",
                source="hira_disease",
                status="fixture",
                summary_text="HIRA search_disease_code에서 망막병증 후보 여러 건을 확인했습니다.",
                render_data={
                    "totalCount": "3",
                    "items": [
                        {"sickCd": "E10.3", "sickNm": "1형 당뇨병·망막병증 동반"},
                        {"sickCd": "E11.3", "sickNm": "2형 당뇨병·망막병증 동반"},
                        {"sickCd": "E13.3", "sickNm": "기타 명시된 당뇨병·망막병증 동반"},
                    ],
                    "request": {"searchText": sick_cd, "diseaseType": "SICK_NM"},
                },
            )

    result = ChatAgent(external=_RetinopathySearchExternal()).answer(
        "당뇨병성 망막병증의 환자수 통계 알려줘"
    )

    assert result["tool_calls"][0]["tool"] == "hira_disease_code_ambiguous"
    assert "E10.3" in result["answer"]
    assert "E11.3" in result["answer"]
    assert "E13.3" in result["answer"]
    assert "가드메트" not in result["answer"]
    assert all(call.get("tool") != "hira_disease_mapping" for call in result["tool_calls"])
    assert all("stats" not in str(call.get("tool")) for call in result["tool_calls"])


def test_large_disease_candidate_set_is_bounded_without_selecting_one() -> None:
    class _LargeDiseaseSearchExternal(ExternalApiClient):
        def hira_disease_name_code(self, sick_cd: str) -> ExternalCall:
            return ExternalCall(
                tool="hira_disease_name_code",
                source="hira_disease",
                status="fixture",
                summary_text="HIRA 상병코드 후보를 확인했습니다.",
                render_data={
                    "totalCount": "6",
                    "items": [
                        {"sickCd": f"A0{index}", "sickNm": f"후보 질병 {index}"}
                        for index in range(1, 7)
                    ],
                    "request": {"searchText": sick_cd, "diseaseType": "SICK_NM"},
                },
            )

    result = ChatAgent(external=_LargeDiseaseSearchExternal()).answer("당뇨병 환자수")
    call = result["tool_calls"][0]

    assert call["tool"] == "hira_disease_code_ambiguous"
    assert call["render_data"]["candidate_total"] == 6
    assert call["render_data"]["candidate_limit"] == 5
    assert len(call["render_data"]["candidates"]) == 5
    assert "후보 6건 중 앞의 5건만 표시" in result["answer"]
    assert "A06" not in result["answer"]
    assert all("stats" not in str(item.get("tool")) for item in result["tool_calls"])


def test_direct_short_disease_code_absence_does_not_widen_to_e11_stats() -> None:
    class _AbsentDiseaseSearchExternal(ExternalApiClient):
        def hira_disease_name_code(self, sick_cd: str) -> ExternalCall:
            return ExternalCall(
                tool="hira_disease_name_code",
                source="hira_disease",
                status="no_data",
                summary_text="HIRA search_disease_code 조회 결과 없음",
                render_data={
                    "totalCount": "0",
                    "items": [],
                    "request": {"searchText": sick_cd, "diseaseType": "SICK_CD"},
                },
            )

    result = ChatAgent(external=_AbsentDiseaseSearchExternal()).answer("상병코드 E11 2024년 환자수")

    assert result["sources"] == ["hira_disease"]
    assert result["tool_calls"][0]["tool"] == "hira_disease_code_absent"
    assert result["tool_calls"][0]["status"] == "no_data"
    assert all(
        call.get("tool") != "hira_disease_hospitalization_outpatient_stats"
        for call in result["tool_calls"]
    )
    assert "E11" in result["answer"]


def test_hira_disease_trend_requests_five_distinct_years() -> None:
    result = ChatAgent(external=_DiseaseSearchExternal()).answer("고지혈증 환자수 추이")

    calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "hira_disease_hospitalization_outpatient_stats"
    ]
    assert [call["render_data"]["request"]["year"] for call in calls] == ["2020", "2021", "2022", "2023", "2024"]
    assert all(call.get("tool") != "hira_disease_gender_age_stats" for call in result["tool_calls"])


@pytest.mark.parametrize("question", ["이상지질혈증 환자통계", "이상지질혈증 환자분포"])
def test_hira_disease_question_accepts_compact_patient_stat_spacing(question):
    result = ChatAgent(external=_DiseaseSearchExternal()).answer(question)

    assert result["sources"] == ["hira_disease"]
    search = next(call for call in result["tool_calls"] if call.get("tool") == "hira_disease_name_code")
    assert search["render_data"]["resolved_sickCd"] == "E78"


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


def test_sales_impact_uses_csd_activity_without_claiming_unobserved_detail():
    result = ChatAgent().answer("리바로 영업활동 Impact는?")
    assert result["sources"] == ["cache"]
    assert "현재 데이터로 답변 불가" not in result["answer"]
    assert "CSD 월별 aggregate 콜수/활동량" in result["answer"]
    assert "impact level·HCP/의사별·기관별 세부는 이 데이터에 포함되지 않습니다" in result["answer"]
    assert [call["tool"] for call in result["tool_calls"]] == ["csd_activity_trend", "get_brand_metric"]
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
    monkeypatch.setenv("NEDRUG_MCP_URL", "http://mcp-nedrug/mcp?serviceKey=SECRETKEY")

    def fail_mcp(_self, _name, _arguments):
        raise McpClientError("503 Server Error for serviceKey=SECRETKEY")

    monkeypatch.setattr("jw_chat_agent_poc.tools.external.client.McpJsonClient.call_tool", fail_mcp)

    call = ExternalApiClient(mode="live", timeout_s=1).mfds_patent("pitavastatin")

    assert call.status == "error"
    assert "SECRETKEY" not in call.summary_text
    assert "SECRETKEY" not in call.render_data["error"]
    assert "serviceKey=<redacted>" in call.summary_text
    assert "serviceKey=<redacted>" in call.render_data["error"]
    assert call.safe_url == "http://mcp-nedrug/mcp?serviceKey=<redacted>"


def test_live_hira_response_preserves_request_year(monkeypatch):
    def fake_post(url, json, headers, timeout):
        assert json["params"]["arguments"]["year"] == "2024"
        return _McpResponse(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [],
                    "structuredContent": {
                        "result": [
                            {
                                "inpatOpat": "외래",
                                "sickCd": "I10",
                                "sickNm": "본태성 고혈압",
                                "ptntCnt": "3769201",
                            }
                        ]
                    },
                },
            }
        )

    monkeypatch.setenv("HIRA_MCP_URL", "http://hira-mcp/json")
    monkeypatch.setattr("jw_chat_agent_poc.tools.external.mcp_client.requests.post", fake_post)

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
        if json["method"] == "tools/list":
            return _clinicaltrials_tools_list_response()
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


def test_clinicaltrials_live_search_uses_structured_content_when_text_is_empty(monkeypatch):
    def fake_post(url, json, headers, timeout):
        if json["method"] == "tools/list":
            return _clinicaltrials_tools_list_response()
        return _McpResponse(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [],
                    "structuredContent": {
                        "result": {
                            "studies": [
                                {
                                    "NCTId": "NCT05537948",
                                    "briefTitle": "Efficacy and Safety of Pitavastatin",
                                    "overallStatus": "ACTIVE_NOT_RECRUITING",
                                    "url": "https://clinicaltrials.gov/study/NCT05537948",
                                }
                            ],
                            "totalCount": 1,
                        }
                    },
                },
            }
        )

    monkeypatch.setenv("CLINICAL_TRIALS_MCP_URL", "http://ct-mcp/json")
    monkeypatch.setattr("jw_chat_agent_poc.tools.external.mcp_client.requests.post", fake_post)

    call = ExternalApiClient(mode="live", timeout_s=1).clinicaltrials_v2_search("pitavastatin")

    assert call.status == "live"
    assert call.render_data["payload"]["totalCount"] == 1
    assert call.render_data["nct_ids"] == ["NCT05537948"]
    assert call.render_data["briefTitle"] == "Efficacy and Safety of Pitavastatin"


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


def test_clinicaltrials_detail_preserves_supported_fields_and_partial_eligibility(monkeypatch):
    def fake_post(url, json, headers, timeout):
        assert json["params"]["name"] == "get_study_details"
        assert json["params"]["arguments"] == {"nctId": "NCT05151731"}
        return _McpResponse(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "clinicaltrials_url: https://clinicaltrials.gov/study/NCT05151731\n"
                                "identification:\n"
                                "  nctId: NCT05151731\n"
                                "  briefTitle: DME Study\n"
                                "status:\n"
                                "  overallStatus: COMPLETED\n"
                                "  startDate: 2022-01-01\n"
                                "  primaryCompletionDate: 2024-04-01\n"
                                "design:\n"
                                "  phases[1]: PHASE3\n"
                                "  enrollmentCount: 300\n"
                                "outcomes:\n"
                                "  primary[1]{measure,timeFrame}:\n"
                                "    Visual acuity,Week 48\n"
                                "eligibility:\n"
                                "  eligibilityCriteria: Adults with DME att...\n"
                                "interventions[1]{type,name}:\n"
                                "  DRUG,Aflibercept\n"
                            ),
                        }
                    ]
                },
            }
        )

    monkeypatch.setenv("CLINICAL_TRIALS_MCP_URL", "http://ct-mcp/json")
    monkeypatch.setattr("jw_chat_agent_poc.tools.external.mcp_client.requests.post", fake_post)

    call = ExternalApiClient(mode="live", timeout_s=1).clinicaltrials_study_details("NCT05151731")

    assert call.status == "live"
    detail = call.render_data["detail"]
    assert detail["nct_id"] == "NCT05151731"
    assert detail["phase"] == "PHASE3"
    assert detail["enrollment"] == 300
    assert detail["interventions"] == ["Aflibercept"]
    assert detail["outcomes"] == ["Visual acuity"]
    assert call.render_data["field_capabilities"]["eligibility"] == "PARTIAL"
    assert call.render_data["eligibility_disclosure"] == "선정·제외 기준은 원문 앞 200자까지만 제공됩니다."
    assert call.safe_url == "https://clinicaltrials.gov/study/NCT05151731"


def test_clinicaltrials_detail_failure_keeps_exact_tool_identity(monkeypatch):
    def fake_post(url, json, headers, timeout):
        raise requests.Timeout("network down")

    monkeypatch.setenv("CLINICAL_TRIALS_MCP_URL", "http://ct-mcp/json")
    monkeypatch.setattr("jw_chat_agent_poc.tools.external.mcp_client.requests.post", fake_post)

    call = ExternalApiClient(mode="live", timeout_s=1).clinicaltrials_study_details("NCT05151731")

    assert call.status == "error"
    assert call.tool == "clinicaltrials_study_details"
    assert call.render_data["external_claim_policy"] == "fail_closed_error"


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
