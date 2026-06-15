from __future__ import annotations

from pathlib import Path
import os

import pytest

from jw_chat_agent_poc import ChatAgent
from jw_chat_agent_poc.rag import LocalDocumentRag
from jw_chat_agent_poc.router import BQRouter
from jw_chat_agent_poc.tools.external import ExternalApiClient


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "jw_chat_agent_poc" / "fixtures"


def test_q1_single_metric_market_size_growth():
    result = ChatAgent().answer("리바로 시장 규모랑 성장 추이는?")
    assert "Q1" in {row["bq"] for row in result["decomposition"]}
    assert "cache" in result["sources"]
    assert "시장" in result["answer"]
    assert any(call.get("tool") == "get_market_landscape" for call in result["tool_calls"])


def test_q2_competitive_and_clinical_routes_metrics_and_ct():
    result = ChatAgent().answer("리바로 경쟁 상황이랑 임상 현황?")
    bqs = {row["bq"] for row in result["decomposition"]}
    assert {"Q2", "Q2.5"}.issubset(bqs)
    tools = {call.get("tool") for call in result["tool_calls"]}
    assert "get_brand_metric" in tools
    assert "clinicaltrials_v2_search" in tools
    assert "external_api" in result["sources"]


def test_q2_combo_fda_label_and_patent_splits_combo_ingredients():
    result = ChatAgent().answer("리바로젯 FDA 라벨·특허?")
    assert result["resolution"]["is_combo"] is True
    assert result["resolution"]["molecule_en"] == ("ezetimibe", "pitavastatin")
    tools = [call.get("tool") for call in result["tool_calls"]]
    assert tools.count("openfda_label_search") == 2
    assert tools.count("mfds_patent") == 2
    assert tools.count("mfds_fda_orangebook") == 2


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
    assert result["sources"] == ["none"]
    assert "현재 데이터로 답변 불가" in result["answer"]
    assert result["tool_calls"] == []


def test_mixed_structured_and_document_sources_are_separated():
    result = ChatAgent().answer(
        "업로드한 시장 전망이랑 실제 우리 점유율 비교",
        documents=[FIXTURE_DIR / "datamonitor_mock.txt"],
    )
    assert {"cache", "document"}.issubset(set(result["sources"]))
    assert any(call.get("tool") == "get_brand_metric" for call in result["tool_calls"])
    assert any(call.get("tool") == "document_rag" for call in result["tool_calls"])


def test_structured_upload_guard_rejects_csv(tmp_path):
    csv = tmp_path / "structured.csv"
    csv.write_text("period,value\\n2026-01,1\\n", encoding="utf-8")
    with pytest.raises(ValueError, match="정형 통계 업로드는 거부"):
        LocalDocumentRag().search("시장 전망", [csv])


def test_router_uses_provided_boundary_without_expanding_bq_map():
    route = BQRouter().route("리바로 포트폴리오 사업성은?")
    assert route[0].bq == "Q5"
    assert route[0].sources == ("none",)


def test_external_redaction_masks_service_key():
    url = "https://example.test/api?" + "serviceKey=X&x=1"
    assert ExternalApiClient.redact_url(url) == "https://example.test/api?serviceKey=<redacted>&x=1"


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
