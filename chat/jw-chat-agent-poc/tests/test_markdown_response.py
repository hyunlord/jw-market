from __future__ import annotations

import inspect
from decimal import Decimal

import requests

from jw_chat_agent_poc import ChatAgent
from jw_chat_agent_poc.agent_loop.planner import GenosToolPlanner
from jw_chat_agent_poc.orchestrator import answer_facts as answer_facts_module
from jw_chat_agent_poc.orchestrator.markdown_formatting import allowed_numbers
from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.orchestrator.markdown_renderers import _safe_table, drug_info_md
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.router.llm_bq_router import GenosBQDecomposer
from jw_chat_agent_poc.service.markdown_cleanup import cleanup_markdown_answer
from jw_chat_agent_poc.service.answer_safety import (
    GENERATION_ATTEMPTS,
    append_competitor_patent_coverage_block,
    dedupe_brand_metric_sentence,
    deterministic_source_block,
    dedupe_repeated_hira_patient_counts,
    ensure_competitive_movement_analysis,
    ensure_judgment_insight,
    ensure_natural_fact_lead,
    ensure_causal_structure,
    ensure_hira_patient_summary,
    ensure_hira_sales_link_analysis,
    ensure_issue_question_quant_analysis,
    fallback_fact_answer,
    replace_internal_fact_dump,
    ensure_single_brand_trend_analysis,
    ensure_top_brand_trend_table,
    enforce_relational_numeric_claims,
    mandatory_fact_lines,
    missing_mandatory_lines,
    normalize_source_line_position,
    single_brand_trend_fact_markdown,
    strip_generated_source_sections,
    presentable_mandatory_lines,
    remove_raw_fact_residue,
    replace_empty_news_shells,
    safe_news_summary_lines,
    strict_allowed_numbers,
    fact_token_allowed,
)
from jw_chat_agent_poc.service.genos_client import (
    GenosClient,
    _ensure_code_rendered_trend_table,
    _ensure_direct_metric_fact_answer,
    _ensure_trend_key_period_table,
    _ensure_trend_prose_fail_closed,
    _fact_lookup_markdown,
    _insert_before_first_table,
    _needs_trend_fact_prose,
    _question_wants_trend_output,
    _remove_endpoint_only_trend_sentence,
    _sanitize_preserving_analysis,
    _web_search_unverified_section,
)
from jw_chat_agent_poc.service.claim_guardrails import apply_claim_guardrails
from jw_chat_agent_poc.tool_use.contracts import EvidenceFact
from jw_chat_agent_poc.tool_use.renderer import render_evidence_answer


def _relational_series_call(brand: str = "리바로") -> dict[str, object]:
    return {
        "tool": "get_brand_metric",
        "render_data": {
            "brand": brand,
            "brand_value_series_10pt": [
                {"period": "2026-01", "value_억원": 83.03, "ms_pct": 3.81, "rank": 7},
                {"period": "2026-02", "value_억원": 75.08, "ms_pct": 3.79, "rank": 7},
                {"period": "2026-03", "value_억원": 87.11, "ms_pct": 3.81, "rank": 6},
                {"period": "2026-04", "value_억원": 84.93, "ms_pct": 3.75, "rank": 6},
                {"period": "2026-05", "value_억원": 80.39, "ms_pct": 3.76, "rank": 6},
            ],
            "market_size_series": [
                {"period": "2026-01", "value_억원": 2_000.0},
                {"period": "2026-05", "value_억원": 2_200.0},
            ],
        },
    }


def test_relational_numeric_gate_corrects_recent_sales_direction_from_raw_series() -> None:
    answer = "리바로 매출은 최근 2개월 연속 상승했습니다."

    revised = enforce_relational_numeric_claims(
        "리바로 최근 월 매출",
        answer,
        [_relational_series_call()],
    )

    assert "최근 2개월 연속 하락했습니다" in revised
    assert "최근 2개월 연속 상승" not in revised


def test_relational_numeric_gate_does_not_invent_share_streak_across_a_reversal() -> None:
    answer = "리바로 점유율은 최근 2개월 연속 상승했습니다."

    revised = enforce_relational_numeric_claims(
        "리바로 점유율",
        answer,
        [_relational_series_call()],
    )

    assert "직전 월 대비 상승했습니다" in revised
    assert "2개월 연속" not in revised


def test_relational_numeric_gate_corrects_peak_period_from_raw_series() -> None:
    call = _relational_series_call("리바로젯")
    render_data = call["render_data"]
    assert isinstance(render_data, dict)
    render_data["brand_value_series_10pt"] = [
        {"period": "2026-01", "value_억원": 108.00, "ms_pct": 5.02},
        {"period": "2026-02", "value_억원": 104.00, "ms_pct": 4.98},
        {"period": "2026-03", "value_억원": 112.00, "ms_pct": 5.10},
        {"period": "2026-04", "value_억원": 120.09, "ms_pct": 5.31},
        {"period": "2026-05", "value_억원": 109.46, "ms_pct": 5.12},
    ]
    answer = "리바로젯은 2026-01 정점 후 하락이 확인됩니다."

    revised = enforce_relational_numeric_claims(
        "리바로젯 최근 성장 배경",
        answer,
        [call],
    )

    assert "2026-04 정점 후 하락" in revised
    assert "2026-01 정점" not in revised


def test_relational_numeric_gate_preserves_supported_relations() -> None:
    answer = "리바로 매출은 최근 2개월 연속 하락했습니다."

    assert enforce_relational_numeric_claims(
        "리바로 최근 월 매출",
        answer,
        [_relational_series_call()],
    ) == answer


def test_relational_numeric_gate_corrects_rank_endpoints_from_raw_series() -> None:
    answer = "순위는 8위에서 9위로 변했습니다."

    revised = enforce_relational_numeric_claims(
        "리바로 성장하고 있어?",
        answer,
        [_relational_series_call()],
    )

    assert "순위는 7위에서 6위로 변했습니다" in revised
    assert "8위에서 9위" not in revised


def test_relational_numeric_gate_corrects_brand_market_growth_relation() -> None:
    answer = "브랜드 성장률이 시장 성장률보다 높아 시장보다 빠르게 성장했습니다."

    revised = enforce_relational_numeric_claims(
        "리바로 성장하고 있어?",
        answer,
        [_relational_series_call()],
    )

    assert "브랜드 성장률이 시장 성장률보다 낮아" in revised
    assert "시장보다 빠르게 성장" not in revised


def test_relational_numeric_gate_replays_captured_relation_failures() -> None:
    answer = (
        "리바로 점유율은 최근 2개월 연속 상승했습니다.\n"
        "리바로 매출은 최근 2개월 연속 상승했습니다."
    )

    revised = enforce_relational_numeric_claims(
        "리바로 점유율과 최근 월 매출",
        answer,
        [_relational_series_call()],
    )

    assert "리바로 점유율은 직전 월 대비 상승했습니다" in revised
    assert "리바로 매출은 최근 2개월 연속 하락했습니다" in revised


def test_relational_numeric_gate_fails_closed_for_ambiguous_brand_series() -> None:
    answer = "최근 2개월 연속 상승했습니다."

    assert enforce_relational_numeric_claims(
        "시장 점유율 변화 설명해줘",
        answer,
        [_relational_series_call("리바로"), _relational_series_call("리바로젯")],
    ) == answer


def test_relational_numeric_gate_does_not_rewrite_competitor_relations() -> None:
    competitor = _relational_series_call("로수젯")
    render_data = competitor["render_data"]
    assert isinstance(render_data, dict)
    render_data["brand_value_series_10pt"] = [
        {"period": "2026-03", "value_억원": 190.0},
        {"period": "2026-04", "value_억원": 192.0},
        {"period": "2026-05", "value_억원": 195.0},
    ]
    answer = (
        "리바로 매출은 최근 2개월 연속 상승했습니다.\n\n"
        "로수젯 매출은 최근 2개월 연속 상승했습니다."
    )

    revised = enforce_relational_numeric_claims(
        "리바로 경쟁구도 분석",
        answer,
        [_relational_series_call(), competitor],
    )

    assert "리바로 매출은 최근 2개월 연속 하락했습니다" in revised
    assert "로수젯 매출은 최근 2개월 연속 상승했습니다" in revised


def test_metric_answer_is_markdown_with_deterministic_table() -> None:
    result = ChatAgent().answer("리바로 매출/시장")

    answer = result["answer"]
    markdown_response = result["markdown_response"]

    assert not answer.startswith("## 답변")
    assert "**요약:**" not in answer
    assert "근거를 종합했습니다" not in answer
    assert "## 데이터" in answer
    assert "| 지표 | 값 |" in answer
    assert "| 매출 | 84.93억원 |" in answer
    assert answer.count("| 매출 | 84.93억원 |") == 1
    assert "| 시장점유율 | 3.33% |" in answer
    assert answer.count("| 시장점유율 | 3.33% |") == 1
    assert "| 시장규모 | 2,256.46억원 |" in answer
    assert answer.count("| 시장규모 | 2,256.46억원 |") == 1
    assert "## 출처" in answer
    assert "UBIST" in answer
    assert "내부 cache" not in answer
    assert "## 근거" in answer
    assert "fact_a" not in answer
    assert "| 출처 | 제공 내용 | 주요 항목 |" in answer
    assert markdown_response["verification"]["status"] == "pass"
    assert any(fact["label"] == "매출" for fact in markdown_response["evidence"])
    assert "<script" not in answer.lower()


def test_metric_interpretation_uses_formatted_units_not_raw_krw() -> None:
    result = ChatAgent().answer("리바로 매출/시장")

    answer = result["answer"]

    assert "2,256.46억원" in answer
    assert "225646489397원" not in answer


def test_hhi_answer_uses_existing_fixture_hhi_value() -> None:
    result = ChatAgent().answer("리바로 HHI는?")

    answer = result["answer"]

    assert "| HHI | 225.78 |" in answer
    assert "### 시장 시계열" in answer


def test_metric_data_dedupes_market_context_across_metric_tables() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_market_landscape",
                "source": "cache",
                "render_data": {"market_size_억원": 2256.77, "market_cagr_5y_pct": 10.73},
            },
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "sales",
                    "period": "2026-04",
                    "sales_억원": 84.93,
                    "market_size_억원": 2256.77,
                    "market_cagr_5y_pct": 10.73,
                    "brand_cagr_5y_pct": 4.98,
                },
            },
        ],
        sources=["cache"],
    )

    assert response.markdown.count("| 시장규모 | 2,256.77억원 |") == 1
    assert "| 매출 | 84.93억원 |" in response.markdown
    assert "CAGR" not in response.markdown
    assert "10.73%" not in response.fact_md
    assert "4.98%" not in response.fact_md
    assert "10.73%" not in response.evidence_md
    assert "4.98%" not in response.allowed_numbers


def test_metric_cagr_scalars_are_blocked_without_reproducible_operands() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로젯",
        calls=[
            {
                "tool": "get_market_landscape",
                "source": "cache",
                "render_data": {
                    "period": "2026-04",
                    "market_size_억원": 2256.77,
                    "market_cagr_5y_pct": 31.22,
                    "brand_cagr_5y_pct": 18.11,
                    "excess_growth_pct": -13.11,
                },
                "summary_text": "리바로젯 시장은 2026-04 기준 2,256.77억원입니다.",
            }
        ],
        sources=["cache"],
    )

    assert "| 시장규모 | 2,256.77억원 |" in response.markdown
    assert "CAGR" not in response.markdown
    assert "Excess growth" not in response.markdown
    assert "31.22%" not in response.markdown
    assert "18.11%" not in response.fact_md
    assert "-13.11%" not in response.evidence_md
    assert "31.22%" not in response.allowed_numbers


def test_hira_answer_is_markdown_with_disease_tables() -> None:
    result = ChatAgent().answer("이상지질혈증 환자 통계")

    answer = result["answer"]

    assert "## 데이터" in answer
    assert "| 구분 | 질병코드 | 질병명 |" in answer
    assert "E78" in answer
    assert "지질단백질대사장애" in answer
    assert "HIRA 질병정보서비스" in answer


def test_get_disease_stats_call_renders_hira_patient_facts_in_body() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_disease_stats",
                "render_data": {
                    "request": {"year": "2024"},
                    "items": [
                        {
                            "inpatOpat": "외래",
                            "sickCd": "I10",
                            "sickNm": "본태성 고혈압",
                            "ptntCnt": "3769201",
                        },
                        {
                            "inpatOpat": "입원",
                            "sickCd": "I10",
                            "sickNm": "본태성 고혈압",
                            "ptntCnt": "18136",
                        },
                    ]
                },
            }
        ],
        sources=["hira_disease"],
    )

    assert "HIRA 환자수" in fact_md
    assert "본태성 고혈압(I10) 2024년 외래: 3769201명" in fact_md
    assert "본태성 고혈압(I10) 2024년 입원: 18136명" in fact_md
    assert "### HIRA 질병통계 fact" in fact_md
    assert "| 외래 | I10 | 본태성 고혈압 | 2024 | 3769201 |" in fact_md
    assert "| 입원 | I10 | 본태성 고혈압 | 2024 | 18136 |" in fact_md


def test_hira_patient_counts_without_year_are_blocked() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_disease_stats",
                "render_data": {
                    "items": [
                        {
                            "inpatOpat": "외래",
                            "sickCd": "I10",
                            "sickNm": "본태성 고혈압",
                            "ptntCnt": "3769201",
                        }
                    ]
                },
            }
        ],
        sources=["hira_disease"],
    )

    assert "기준기간 미확인으로 환자수 표시 보류" in fact_md
    assert "3769201명" not in fact_md


def test_get_disease_stats_facade_reads_nested_hira_patient_counts() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_disease_stats",
                "source": "hira_disease",
                "render_data": {
                    "status": "ok",
                    "facade_tool": "get_disease_stats",
                    "calls": [
                        {
                            "tool": "hira_disease_mapping",
                            "source": "hira_disease",
                            "render_data": {
                                "sickCd": "I10",
                                "disease_name": "본태성 고혈압",
                            },
                        },
                        {
                            "tool": "hira_disease_hospitalization_outpatient_stats",
                            "source": "hira_disease",
                            "render_data": {
                                "request": {"year": "2024"},
                                "mapping_sickCd": "I10",
                                "mapping_disease_name": "본태성 고혈압",
                                "items": [
                                    {
                                        "inpatOpat": "외래",
                                        "sickCd": "I10",
                                        "sickNm": "본태성 고혈압",
                                        "ptntCnt": "3769201",
                                    }
                                ],
                            },
                        },
                    ],
                },
            }
        ],
        sources=["hira_disease"],
    )

    assert "HIRA 환자수" in fact_md
    assert "본태성 고혈압(I10) 2024년 외래: 3769201명" in fact_md
    assert "| 외래 | I10 | 본태성 고혈압 | 2024 | 3769201 |" in fact_md


def test_get_procedure_stats_facade_reads_nested_hira_procedure_counts() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_procedure_stats",
                "source": "hira_procedure",
                "render_data": {
                    "facade_tool": "get_procedure_stats",
                    "calls": [
                        {
                            "tool": "hira_procedure_gender_ipat_opat_stats",
                            "source": "hira_procedure",
                            "render_data": {
                                "request": {"st5Cd": "MM302", "year": "2024"},
                                "items": [
                                    {
                                        "inpatOpat": "외래",
                                        "st5Cd": "MM302",
                                        "st5Nm": "기관절개술",
                                        "ptntCnt": "1234",
                                        "specCnt": "1300",
                                        "useQty": "1400",
                                    }
                                ],
                            },
                        }
                    ],
                },
            }
        ],
        sources=["hira_procedure"],
    )

    assert "### HIRA 진료행위통계" in response.markdown
    assert "| 외래 | MM302 | 기관절개술 | 2024 | 1234 | 1300 | 1400 |" in response.markdown
    assert "### HIRA 진료행위통계 fact" in response.fact_md
    assert "| 외래 | MM302 | 기관절개술 | 2024 | 1234 |" in response.fact_md
    assert "HIRA 진료행위정보서비스" in response.markdown


def test_web_search_facade_renders_nested_results_as_unverified_external_section() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "web_search",
                "source": "web_search",
                "render_data": {
                    "facade_tool": "web_search",
                    "query": "리바로 경쟁제품 디테일링",
                    "calls": [
                        {
                            "tool": "web_search",
                            "source": "web_search",
                            "render_data": {
                                "provider": "fixture",
                                "request": {"query": "리바로 경쟁제품 디테일링"},
                                "items": [
                                    {
                                        "title": "리바로 경쟁제품 디테일링 동향",
                                        "url": "https://example.com/livalo-detailing",
                                        "snippet": "웹 검색 snippet",
                                    }
                                ],
                            },
                        }
                    ],
                },
            }
        ],
        sources=["web_search"],
    )

    assert "### 웹 검색 결과(미검증)" in response.markdown
    assert "https://example.com/livalo-detailing" in response.markdown
    assert "### 웹 검색 결과 fact(미검증)" not in response.fact_md
    assert "https://example.com/livalo-detailing" not in response.fact_md
    assert "웹 검색 snippet" not in response.fact_md
    assert "| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |" in response.markdown
    assert "| 웹 검색 결과(미검증) | — | — | — | — | 전체 | — |" in response.markdown


def test_genos_web_only_answer_skips_final_llm_and_appends_unverified_section(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "web_search",
                "source": "web_search",
                "render_data": {
                    "provider": "fixture",
                    "query": "리바로 경쟁제품 동향",
                    "items": [
                        {
                            "title": "리바로 경쟁제품 디테일링 동향",
                            "url": "https://example.com/livalo-detailing",
                            "snippet": "경쟁제품 디테일링 웹 스니펫",
                        }
                    ],
                },
            }
        ],
        sources=["web_search"],
    )
    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        raise AssertionError("web-only answers should not call the final LLM")

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            "리바로 경쟁제품 최근 동향 검색해줘",
            {"markdown_response": response.to_dict(), "tool_calls": [
                {
                    "tool": "web_search",
                    "source": "web_search",
                    "render_data": {
                        "provider": "fixture",
                        "query": "리바로 경쟁제품 동향",
                        "items": [
                            {
                                "title": "리바로 경쟁제품 디테일링 동향",
                                "url": "https://example.com/livalo-detailing",
                                "snippet": "경쟁제품 디테일링 웹 스니펫",
                            }
                        ],
                    },
                }
            ]},
        )
    )

    body, web_section = answer.split("### 웹 검색 결과(미검증)", maxsplit=1)
    assert "경쟁제품 디테일링 웹 스니펫" not in body
    assert "https://example.com/livalo-detailing" not in body
    assert "웹 검색 결과는 하단 웹 검색 결과(미검증) 섹션을 참조하세요." in body
    assert "경쟁제품 디테일링 웹 스니펫" in web_section
    assert "https://example.com/livalo-detailing" in web_section


def test_web_search_section_summarizes_dedupes_and_splits_old_results() -> None:
    section = _web_search_unverified_section(
        [
            {
                "tool": "web_search",
                "source": "web_search",
                "render_data": {
                    "items": [
                        {
                            "title": "리바로젯, 이상지질혈증 2제 복합제 매출 1위 달성",
                            "url": "https://www.monews.co.kr/news/articleView.html?idxno=411866&utm_source=copy",
                            "snippet": "2026-06-17 JW중외제약 리바로젯이 이상지질혈증 2제 복합제 시장에서 매출 1위를 기록했다.",
                            "published_date": "2026-06-17",
                        },
                        {
                            "title": "리바로젯 이상지질혈증 2제 복합제 매출 1위 달성",
                            "url": "https://www.monews.co.kr/news/articleView.html?idxno=411866",
                            "snippet": "2026-06-17 리바로젯 2제 복합제 시장 매출 1위 기사.",
                            "published_date": "2026-06-17",
                        },
                        {
                            "title": "피타바스타틴 시장 경쟁 심화",
                            "url": "https://pharma.example.test/pitavastatin-market",
                            "snippet": "2026-05-30 피타바스타틴 시장에서 복합제 경쟁이 확대되고 있다.",
                            "published_date": "2026-05-30",
                        },
                        {
                            "title": "리바로 시장 장문 보도자료",
                            "url": "https://pharma.example.test/livalo-long",
                            "snippet": "2026-06-18 리바로 시장 동향 요약. " + ("장문 원문 " * 60) + "원문덤프꼬리",
                            "published_date": "2026-06-18",
                        },
                        {
                            "title": "FDA Approves Livalo",
                            "url": "https://www.fda.gov/drugs/livalo-2009",
                            "snippet": "최종편집 2026-06-27. 리바로, 고지혈증 치료제로 미 FDA 승인. 승인 2009.08.05.",
                            "published_date": "2026-06-27",
                        },
                        {
                            "title": "RAG 검색 시스템 구축 사례",
                            "url": "https://it.example.test/rag",
                            "snippet": "2026-07-01 Livalo라는 내부 프로젝트명으로 RAG 시스템을 구축했다.",
                            "published_date": "2026-07-01",
                        },
                    ],
                },
            }
        ]
    )

    assert "### 웹 검색 결과(미검증)" in section
    assert "#### 주요 MI 요약" in section
    assert "리바로젯, 이상지질혈증 2제 복합제 매출 1위 달성" in section
    assert section.count("https://www.monews.co.kr/news/articleView.html?idxno=411866") == 1
    assert "매체 병합: 2건" in section
    assert "패밀리" in section
    assert "시장" in section
    assert "→ 내부 지표 확인 가능" in section
    assert "#### 과거 자료" in section
    assert "2009-08-05" in section
    assert section.index("2026-06-17") < section.index("2009-08-05")
    assert "RAG 검색 시스템 구축 사례" not in section
    assert "FDA Approves Livalo" in section
    assert "원문덤프꼬리" not in section


def test_genos_default_base_url_coerces_existing_env_to_gemini_three_flash(monkeypatch) -> None:
    monkeypatch.setenv("GENOS_BASE_URL", "https://jwai-dev.jwhealthcare.com/api/gateway/rep/serving/163")
    monkeypatch.delenv("GENOS_SERVING_ID", raising=False)

    client = GenosClient(token="dummy-token")
    router = GenosBQDecomposer(token="dummy-token")
    planner = GenosToolPlanner(token="dummy-token")

    assert client.base_url == "https://jwai-dev.jwhealthcare.com/api/gateway/rep/serving/514"
    assert router.base_url == "https://jwai-dev.jwhealthcare.com/api/gateway/rep/serving/517"
    assert planner.base_url == "https://jwai-dev.jwhealthcare.com/api/gateway/rep/serving/508"


def test_genos_scoped_model_envs_allow_mixed_planner_and_final(monkeypatch) -> None:
    monkeypatch.setenv("GENOS_BASE_URL", "https://jwai-dev.jwhealthcare.com/api/gateway/rep/serving/514")
    monkeypatch.setenv("GENOS_PLANNER_SERVING_ID", "163")
    monkeypatch.setenv("GENOS_FINAL_SERVING_ID", "514")
    monkeypatch.setenv("GENOS_PLANNER_BEARER_TOKEN", "planner-token")
    monkeypatch.setenv("GENOS_FINAL_BEARER_TOKEN", "final-token")

    client = GenosClient()
    planner = GenosToolPlanner()

    assert client.base_url == "https://jwai-dev.jwhealthcare.com/api/gateway/rep/serving/514"
    assert client.token == "final-token"
    assert planner.base_url == "https://jwai-dev.jwhealthcare.com/api/gateway/rep/serving/163"
    assert planner.token == "planner-token"


def test_genos_timeouts_are_read_from_runtime_env(monkeypatch) -> None:
    monkeypatch.setenv("GENOS_FINAL_TIMEOUT_S", "181")
    monkeypatch.setenv("GENOS_AGENT_TIMEOUT_S", "182")

    client = GenosClient(token="dummy-token")
    planner = GenosToolPlanner(token="dummy-token")

    assert client.timeout_s == 181
    assert planner.timeout_s == 182


def test_genos_generation_attempts_are_read_from_runtime_env(monkeypatch) -> None:
    monkeypatch.setenv("GENOS_GENERATION_ATTEMPTS", "2")
    calls = [
        {
            "tool": "agent_calculation",
            "source": "cache",
            "render_data": {
                "brand": "리바로",
                "metric": "market_share_delta",
                "period": "2026-03→2026-04",
                "from_ms_pct": 3.46,
                "to_ms_pct": 3.33,
                "ms_delta_pct": -0.13,
            },
        }
    ]
    response = MarkdownResponseBuilder().build(brand="리바로", calls=calls, sources=["cache"])
    attempts = 0

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        nonlocal attempts
        attempts += 1
        raise requests.Timeout("Flash generation timed out")
        yield ""

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            "리바로 3달전 대비 점유율 변화",
            {"markdown_response": response.to_dict()},
        )
    )

    assert attempts == 2
    assert "점유율 변화" in answer


def test_safe_news_summary_lines_cite_article_content() -> None:
    fact_md = """### 인사이트 근거 fact - 뉴스/이슈
| 날짜 | 제목 | 출처 | URL | 요약 | 매칭 발췌 |
| --- | --- | --- | --- | --- | --- |
| 2026-03-12 | 리바로젯 복합제 경쟁 기사 | 데일리팜 | https://news.example/livalozet-atozet | 리바로젯과 아토젯의 복합제 경쟁 구도가 함께 언급됐다. 추가 문장입니다. | 본문에서 아토젯과 리바로젯이 함께 언급됩니다. |
"""

    lines = safe_news_summary_lines(fact_md)

    assert lines == (
        "- 뉴스: 데일리팜(2026-03-12) [「리바로젯 복합제 경쟁 기사」](https://news.example/livalozet-atozet) — 리바로젯과 아토젯의 복합제 경쟁 구도가 함께 언급됐다.",
    )
    assert "관련 기사에서" not in lines[0]
    assert "언급이 확인됐습니다" not in lines[0]


def test_single_brand_focus_facts_suppress_market_level_segments() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로하이",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로하이",
                    "metric": "sales",
                    "period": "2026-04",
                    "answer_scope": "single_brand_focus",
                    "sales_억원": 31.0,
                    "level": "Brand",
                    "level_segments": [
                        {"rank": 1, "name": "트윈스타", "ms_recent_pct": 4.3, "value": 8_508_000_000},
                        {"rank": 2, "name": "아모잘탄", "ms_recent_pct": 3.84, "value": 7_594_000_000},
                    ],
                },
            }
        ],
        sources=["cache"],
    )

    assert "리바로하이 지표 fact" in response.fact_md
    assert "| 매출 | 31.00억원 |" in response.fact_md
    assert "브랜드 핵심 지표" in response.fact_md
    assert "리바로하이 2026-04 매출 31.00억원" in response.fact_md
    assert "트윈스타" not in response.fact_md
    assert "Brand별 점유율 fact" not in response.fact_md


def test_single_brand_focus_recent_sales_is_mandatory() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로하이",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로하이",
                    "metric": "sales",
                    "period": "2026-04",
                    "answer_scope": "single_brand_focus",
                    "sales_억원": 31.0,
                    "ms_recent_pct": 0.03,
                },
            }
        ],
        sources=["cache"],
    )
    lines = mandatory_fact_lines(response.fact_md)

    assert "- 브랜드 핵심 지표: 리바로하이 2026-04 매출 31.00억원 시장점유율 0.03%" in lines
    assert missing_mandatory_lines("리바로하이 환자수만 확인했습니다.", lines)
    assert not missing_mandatory_lines("리바로하이 최근 매출은 31.00억원이고 시장점유율은 0.03%입니다.", lines)


def test_single_brand_focus_requires_share_and_rank_when_present() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 브랜드 핵심 지표 | 리바로 2026-04 매출 84.93억원 시장점유율 3.76% 순위 6/516 |
"""
    lines = mandatory_fact_lines(fact_md)

    assert missing_mandatory_lines("리바로 매출은 84.93억원입니다.", lines)
    assert missing_mandatory_lines("리바로 매출은 84.93억원이고 시장점유율은 3.76%입니다.", lines)
    assert not missing_mandatory_lines(
        "리바로 2026-04 매출은 84.93억원, 시장점유율은 3.76%, 순위는 6/516입니다.",
        lines,
    )


def test_direct_brand_metric_answer_appends_missing_numeric_fact(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "sales",
                    "period": "2026-04",
                    "sales_억원": 84.93,
                    "ms_recent_pct": 3.76,
                    "rank": 6,
                    "total_brands_in_market": 516,
                },
            }
        ],
        sources=["cache"],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield "리바로는 상위권 매출 규모를 유지하고 있습니다.\n\n출처: UBIST"

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(GenosClient(token="dummy-token").stream_answer("리바로 매출", {"markdown_response": response.to_dict()}))

    assert "84.93억원" in answer
    assert "3.76%" in answer
    assert "6/516" in answer
    assert "출처: UBIST" not in answer
    assert "## 출처" in answer
    assert "| UBIST | 2026-04 | — | — | 516 | 전체 | 억원 |" in answer
    assert answer.rfind("## 출처") > answer.rfind("84.93억원")


def test_deterministic_source_block_lists_news_articles_without_internal_names() -> None:
    fact_md = """## 확정 fact set
### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 매출 변화 | 리바로 2026-03→2026-04: 87.11억원 → 84.93억원, 변화 -2.18억원(-2.50%) |

### 인사이트 근거 fact - 뉴스/이슈
| 날짜 | 제목 | 출처 | URL | 요약 | 매칭 발췌 |
| --- | --- | --- | --- | --- | --- |
| 2026-04-01 | 아토젯 시장 이슈 | 약업신문 | https://news.example/atozet | 아토젯 처방 경쟁 맥락이 기사 요약에 포함됐다. | 아토젯 관련 본문 발췌 |

### 출처 유형 fact
| 출처 |
| --- |
| UBIST |
| 뉴스/이슈 |
"""

    block = deterministic_source_block(fact_md)

    assert block.startswith("## 출처")
    assert "| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |" in block
    assert "| UBIST | — | — | — | — | 전체 | — |" in block
    assert "뉴스/이슈 · 약업신문 「아토젯 시장 이슈」 https://news.example/atozet" in block
    assert "| 2026-04-01 | — | — | — | 전체 | — |" in block
    assert "내부 심층분석" not in block
    assert "deep_analysis_events" not in block
    assert "cache" not in block


def test_source_block_renders_hira_call_metadata_from_nested_calls() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_disease_stats",
                "source": "hira_disease",
                "render_data": {
                    "facade_tool": "get_disease_stats",
                    "calls": [
                        {
                            "tool": "hira_disease_hospitalization_outpatient_stats",
                            "source": "hira_disease",
                            "render_data": {
                                "request": {"sickCd": "I10", "year": "2024"},
                                "mapping_sickCd": "I10",
                                "mapping_disease_name": "본태성(원발성) 고혈압",
                                "items": [
                                    {
                                        "inpatOpat": "입원",
                                        "sickCd": "I10",
                                        "sickNm": "본태성(원발성) 고혈압",
                                        "ptntCnt": 16171,
                                    },
                                    {
                                        "inpatOpat": "외래",
                                        "sickCd": "I10",
                                        "sickNm": "본태성(원발성) 고혈압",
                                        "ptntCnt": 3769201,
                                    },
                                ],
                            },
                        }
                    ],
                },
            }
        ],
        ["hira_disease"],
    )

    block = deterministic_source_block(fact_md)

    assert "| HIRA 질병정보서비스 · I10 본태성(원발성) 고혈압 | 2024 | — | — | — | 전체 | 명 |" in block
    assert "KCD 기반 환자 통계" not in block


def test_source_block_uses_answered_hira_options_not_all_nested_tools() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_disease_stats",
                "source": "hira_disease",
                "render_data": {
                    "facade_tool": "get_disease_stats",
                    "calls": [
                        {
                            "tool": "hira_disease_name_code",
                            "source": "hira_disease",
                            "render_data": {
                                "request": {"disease_name": "본태성 고혈압"},
                                "mapping_disease_name": "본태성 고혈압",
                            },
                        },
                        {
                            "tool": "hira_disease_hospitalization_outpatient_stats",
                            "source": "hira_disease",
                            "render_data": {
                                "request": {"sickCd": "I10", "year": "2024"},
                                "mapping_sickCd": "I10",
                                "mapping_disease_name": "본태성(원발성) 고혈압",
                                "items": [
                                    {
                                        "inpatOpat": "입원",
                                        "sickCd": "I10",
                                        "sickNm": "본태성(원발성) 고혈압",
                                        "ptntCnt": 16171,
                                    },
                                    {
                                        "inpatOpat": "외래",
                                        "sickCd": "I10",
                                        "sickNm": "본태성(원발성) 고혈압",
                                        "ptntCnt": 3769201,
                                    },
                                ],
                            },
                        },
                        {
                            "tool": "hira_disease_gender_age_stats",
                            "source": "hira_disease",
                            "render_data": {
                                "request": {"sickCd": "I10", "year": "2024"},
                                "mapping_sickCd": "I10",
                                "mapping_disease_name": "본태성(원발성) 고혈압",
                                "items": [{"age": "0_9세", "sickCd": "I10", "sickNm": "본태성(원발성) 고혈압", "ptntCnt": 129}],
                            },
                        },
                        {
                            "tool": "hira_disease_institution_class_stats",
                            "source": "hira_disease",
                            "render_data": {
                                "request": {"sickCd": "I10", "year": "2024"},
                                "mapping_sickCd": "I10",
                                "mapping_disease_name": "본태성(원발성) 고혈압",
                            },
                        },
                        {
                            "tool": "hira_disease_area_stats",
                            "source": "hira_disease",
                            "render_data": {
                                "request": {"sickCd": "I10", "year": "2024"},
                                "mapping_sickCd": "I10",
                                "mapping_disease_name": "본태성(원발성) 고혈압",
                            },
                        },
                    ],
                },
            }
        ],
        ["hira_disease"],
    )

    block = deterministic_source_block(fact_md)

    assert block.count("I10 본태성(원발성) 고혈압") == 1
    assert "): 본태성 고혈압" not in block
    assert "| 2024 | — | — | — | 전체 | 명 |" in block
    assert "질병명칭/코드" not in block
    assert "성별/연령" not in block
    assert "요양기관종별" not in block
    assert "지역별" not in block
    assert "조회 기준" not in block


def test_sales_trend_table_with_latest_window_prevents_mandatory_tail() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 매출 추이 | 리바로 매출 시계열 2025-07 84.76억원 → 2026-04 84.93억원, MS 3.92% → 3.76% |
"""
    answer = """리바로 매출은 2025-12 90.86억원에서 2026-02 75.08억원까지 내려간 뒤 2026-04 84.93억원으로 회복했습니다.

| 기간 | 리바로 매출 | 리바로 MS |
| --- | --- | --- |
| 2025-11 | 80.35억원 | 3.92% |
| 2025-12 | 90.86억원 | 3.93% |
| 2026-04 | 84.93억원 | 3.76% |
"""
    lines = mandatory_fact_lines(fact_md)

    assert missing_mandatory_lines(answer, lines) == ()


def test_source_block_preserves_news_search_condition_when_present() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "deep_analysis_related_news",
                "source": "deep_analysis_events",
                "render_data": {
                    "facade_tool": "search_news",
                    "filter_entries": [("text_contains", "아토젯")],
                    "items": [
                        {
                            "date": "2026-04-01",
                            "title": "아토젯 시장 이슈",
                            "source": "약업신문",
                            "url": "https://news.example/atozet",
                            "summary": "아토젯 처방 경쟁 맥락이 기사 요약에 포함됐다.",
                        }
                    ],
                },
            }
        ],
        ["deep_analysis_events"],
    )

    block = deterministic_source_block(fact_md)

    assert "뉴스/이슈 · 약업신문 「아토젯 시장 이슈」 https://news.example/atozet" in block
    assert "| 2026-04-01 | — | — | — | 전체 | — |" in block
    assert "events corpus" not in block


def test_source_block_renders_data_period_from_call_series() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "source_label": "UBIST",
                    "brand": "리바로",
                    "metric": "sales",
                    "brand_value_series_10pt": [
                        {"period": "2025-07", "value_억원": 78.0},
                        {"period": "2026-04", "value_억원": 84.93},
                    ],
                    "query_spec": {
                        "view": "market_landscape",
                        "market": "C10A1",
                        "filters": {"brand": "리바로"},
                    },
                    "market_name": "이상지질혈증",
                    "total_brands_in_market": 470,
                },
            }
        ],
        ["UBIST"],
    )

    block = deterministic_source_block(fact_md)

    assert (
        "| UBIST | 2025-07~2026-04 | 전략뷰 (market_landscape) | 이상지질혈증 | 470 | 전체 | 억원 |"
    ) in block


def test_source_block_renders_trend_data_detail_from_render_metadata() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "source_label": "UBIST",
                    "brand": "페린젝트",
                    "metric": "series",
                    "market_id": "B03A",
                    "view": "market_landscape",
                    "market_name": "철분제",
                    "total_brands_in_market": 516,
                    "brand_value_series_10pt": [
                        {"period": "2023-Q3", "value_억원": 41.53, "ms_pct": 29.34},
                        {"period": "2025-Q4", "value_억원": 35.16, "ms_pct": 25.36},
                    ],
                    "market_size_series": [
                        {"period": "2023-Q3", "value_억원": 141.55},
                        {"period": "2025-Q4", "value_억원": 138.67},
                    ],
                },
            }
        ],
        ["UBIST"],
    )

    block = deterministic_source_block(fact_md)

    assert (
        "| UBIST | 2023-Q3~2025-Q4 | 전략뷰 (market_landscape) | 철분제 | 516 | 전체 | 억원 |"
    ) in block


def test_source_block_uses_confirmed_view_mapping_for_strategy_cache_market() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "source_label": "UBIST",
                    "brand": "리바로",
                    "metric": "sales",
                    "period": "2026-04",
                    "market_id": "strategy_006",
                    "market_name": "리바로 리바로젯",
                    "total_brands_in_market": 516,
                    "sales_억원": 84.93,
                },
            }
        ],
        ["cache"],
    )

    block = deterministic_source_block(fact_md)

    assert "| UBIST | 2026-04 | 전략뷰 (market_landscape) | 리바로 리바로젯 | 516 | 전체 | 억원 |" in block
    assert "strategy_006" not in block


def test_source_block_uses_confirmed_view_mapping_for_market_landscape_query_market() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_market_scope",
                "source": "UBIST",
                "render_data": {
                    "source_label": "UBIST",
                    "period": "2026-04",
                    "market_id": "ml_006",
                    "market_name": "리바로 리바로젯",
                    "total_brands_in_market": 470,
                },
            }
        ],
        ["UBIST"],
    )

    block = deterministic_source_block(fact_md)

    assert "| UBIST | 2026-04 | 전략뷰 (market_landscape) | 리바로 리바로젯 | 470 | 전체 | — |" in block


def test_source_block_notes_confirmed_strategy_and_query_layer_denominator_difference() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_market_scope",
                "source": "UBIST",
                "render_data": {
                    "source_label": "UBIST",
                    "period": "2026-04",
                    "market_id": "strategy_006",
                    "market_name": "리바로/리바로젯",
                    "total_brands_in_market": 516,
                },
            },
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "source_label": "UBIST",
                    "period": "2026-04",
                    "query_spec": {
                        "market": "ml_006",
                        "market_name": "리바로/리바로젯",
                        "view": "market_landscape",
                        "total_brands_in_market": 470,
                        "rank": 6,
                    },
                },
            },
        ],
        ["UBIST"],
    )

    block = deterministic_source_block(fact_md)

    assert "| UBIST | 2026-04 | 전략뷰 (market_landscape) | 리바로/리바로젯 | 470, 516 | 전체 | — |" in block
    assert "ml_006" not in block


def test_source_block_notes_confirmed_counterpart_denominator_for_strategy_only_path() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "source_label": "UBIST",
                    "brand": "리바로",
                    "metric": "sales",
                    "period": "2026-04",
                    "market_id": "strategy_006",
                    "market_name": "리바로/리바로젯",
                    "total_brands_in_market": 516,
                    "rank": 6,
                    "sales_억원": 84.93,
                },
            }
        ],
        ["cache"],
    )

    block = deterministic_source_block(fact_md)

    assert "| UBIST | 2026-04 | 전략뷰 (market_landscape) | 리바로/리바로젯 | 516 | 전체 | 억원 |" in block
    assert "ml_006" not in block


def test_source_block_notes_confirmed_counterpart_denominator_for_query_only_path() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "source_label": "UBIST",
                    "brand": "리바로",
                    "metric": "sales",
                    "period": "2026-04",
                    "market_id": "ml_006",
                    "market_name": "리바로/리바로젯",
                    "view": "market_landscape",
                    "total_brands_in_market": 470,
                    "rank": "6/470",
                    "sales_억원": 84.93,
                    "ms_recent_pct": 3.76,
                },
            }
        ],
        ["UBIST"],
    )

    block = deterministic_source_block(fact_md)

    assert "순위 6/470/470" not in fact_md
    assert "| UBIST | 2026-04 | 전략뷰 (market_landscape) | 리바로/리바로젯 | 470 | 전체 | 억원 |" in block
    assert "strategy_006" not in block
    assert "6/470/516" not in block


def test_source_block_notes_split_market_class2_basis() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "IQVIA NSA",
                "render_data": {
                    "source_label": "IQVIA NSA",
                    "brand": "악템라",
                    "metric": "sales",
                    "period": "2025-Q4",
                    "market_id": "ml_011",
                    "market_name": "악템라",
                    "view": "market_landscape",
                    "total_brands_in_market": 26,
                    "rank": "3/26",
                    "market_structure": {
                        "type": "class_split",
                        "display_axis": "class_2",
                        "display_axis_label": "Class 2",
                        "display_denominator": 12,
                        "axes": [
                            {"key": "class_1", "label": "Class 1", "exposure": "catalog_only"},
                            {"key": "class_2", "label": "Class 2", "exposure": "display"},
                        ],
                    },
                },
            }
        ],
        ["IQVIA NSA"],
    )

    block = deterministic_source_block(fact_md)

    assert "| IQVIA NSA | 2025-Q4 | 전략뷰 (market_landscape) | 악템라 | 12 | 전체 | 억원 |" in block
    assert "Class 1" not in block


def test_source_block_uses_confirmed_view_mapping_for_competitive_dynamics_market() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_market_scope",
                "source": "UBIST",
                "render_data": {
                    "source_label": "UBIST",
                    "period": "2026-04",
                    "market_id": "cd_006",
                    "market_name": "리바로 리바로젯",
                    "total_brands_in_market": 104,
                },
            }
        ],
        ["UBIST"],
    )

    block = deterministic_source_block(fact_md)

    assert "| UBIST | 2026-04 | 전략뷰 (competitive_dynamics) | 리바로 리바로젯 | 104 | 전체 | — |" in block
    assert "cd_006" not in block


def test_source_block_omits_view_name_for_unconfirmed_market() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "source_label": "UBIST",
                    "period": "2026-04",
                    "market_id": "unknown_999",
                    "market_name": "미확정 시장",
                    "total_brands_in_market": 17,
                },
            }
        ],
        ["UBIST"],
    )

    block = deterministic_source_block(fact_md)

    assert "| UBIST | 2026-04 | — | — | 17 | 전체 | — |" in block
    assert "확정 시장" not in block
    assert "market_landscape" not in block


def test_completion_series_call_does_not_render_per_call_brand_series_table() -> None:
    response = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "brand": "리바로",
                    "metric": "sales",
                    "period": "2026-04",
                    "completion_reason": "comparison_trend_requires_series",
                    "sales_억원": 84.93,
                    "brand_value_series_10pt": [
                        {"period": "2026-03", "value_억원": 87.11, "ms_pct": 3.81},
                        {"period": "2026-04", "value_억원": 84.93, "ms_pct": 3.76},
                    ],
                },
            }
        ],
        ["UBIST"],
    )

    assert "| 매출 추이 | 리바로 매출 시계열 2026-03 87.11억원 → 2026-04 84.93억원" in response
    assert "### 리바로 매출 시계열 fact" not in response
    assert "### 리바로 지표 fact" not in response


def test_primary_series_call_still_renders_brand_series_table() -> None:
    response = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "brand": "리바로",
                    "metric": "sales",
                    "period": "2026-04",
                    "answer_scope": "single_brand_trend",
                    "sales_억원": 84.93,
                    "brand_value_series_10pt": [
                        {"period": "2026-03", "value_억원": 87.11, "ms_pct": 3.81},
                        {"period": "2026-04", "value_억원": 84.93, "ms_pct": 3.76},
                    ],
                },
            }
        ],
        ["UBIST"],
    )

    assert "### 리바로 매출 시계열 fact" in response
    assert "| 2026-04 | 84.93억원 | 3.76% |" in response


def test_generated_source_lines_are_stripped_before_deterministic_render() -> None:
    raw = "결론입니다.\n\n## 출처\n- 데이터: cache\n- 뉴스: 내부 심층분석\n\n## 다음\n계속"

    stripped = strip_generated_source_sections(raw)

    assert "## 출처" not in stripped
    assert "cache" not in stripped
    assert "내부 심층분석" not in stripped
    assert "## 다음" in stripped


def test_generated_inline_source_bullets_are_stripped_before_deterministic_render() -> None:
    raw = (
        "리바로는 온라인 오정보 이슈와 함께 매출 추이를 봐야 합니다.\n"
        "* 출처(2026-06-11) [「유튜브 오정보가 키운 스타틴 기피」](https://example.test) — 온라인 오정보 이슈\n"
        "- 출처(2026-06-10) [「스타틴 먹지 마라는 말의 대가」](https://example.test/2) — 치료 중단 위험\n"
    )

    stripped = strip_generated_source_sections(raw)

    assert "온라인 오정보 이슈와 함께 매출 추이를 봐야 합니다" in stripped
    assert "출처(2026-06-11)" not in stripped
    assert "출처(2026-06-10)" not in stripped


def test_causal_structure_is_appended_from_verified_facts() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 시장/브랜드 변화율 대조 | 리바로 2026-01→2026-02 브랜드 변화율 -9.58% 시장 변화율 -9.12% 변화율 차이 -0.46%p 근거 기반 인과 분석: 시장 동반 하락이 브랜드 매출 하락의 주요 배경으로 해석됨 |
"""

    answer = ensure_causal_structure("리바로 2월 매출 하락이 시장 영향인지 브랜드 고유인지", "리바로는 하락했습니다.", fact_md)

    assert "## 인과 분석" not in answer
    assert "시장/브랜드 변화율 대조" in answer
    assert "브랜드 변화율을 시장 변화율과 나란히" in answer
    assert "고유 약세 신호" in answer
    assert "-9.58%" in answer
    assert "-9.12%" in answer


def test_causal_structure_uses_single_brand_metric_facts() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 브랜드 핵심 지표 | 리바로 2026-04 매출 84.93억원 시장점유율 3.76% 순위 6/516 |
"""

    answer = ensure_causal_structure("리바로 매출", "리바로 매출은 84.93억원입니다.", fact_md)

    assert "## 인과 분석" not in answer
    assert "브랜드 핵심 지표" not in answer
    assert "리바로는 2026-04 기준 매출 84.93억원" in answer
    assert "시장 내 침투 수준" in answer
    assert "현재 위치를 기준선" not in answer


def test_dedupe_brand_metric_sentence_handles_e2_duplicate_metric_surface() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 브랜드 핵심 지표 | 리바로 2026-04 매출 84.93억원 시장점유율 3.76% 순위 6/470 |
"""
    duplicate = (
        "리바로는 2026-04 기준 매출 84.93억원, 시장점유율 3.76%, 순위 6/470위입니다.\n\n"
        "인과 해석상 시장 내 위치를 기준선으로 봅니다.\n\n"
        "리바로는 2026-04 기준 매출 84.93억원, 시장점유율 3.76%, 순위 6/470위입니다."
    )

    answer = dedupe_brand_metric_sentence(duplicate, fact_md)

    assert answer.count("리바로는 2026-04 기준 매출 84.93억원") == 1
    assert "인과 해석상 시장 내 위치" in answer


def test_causal_structure_uses_hira_patient_facts() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| HIRA 환자수 | 본태성(원발성) 고혈압(I10) 외래: 3769201명 |
"""

    answer = ensure_causal_structure("리바로하이 질병 환자수랑 최근 매출 한번에", "외래 환자수는 3769201명입니다.", fact_md)

    assert "## 인과 분석" not in answer
    assert "HIRA 환자수:" not in answer
    assert "HIRA 기준 본태성(원발성) 고혈압(I10) 외래 환자수는 3769201명입니다." in answer
    assert "환자수 규모와 브랜드 매출·점유율은 질환 통계와 처방 성과를 나란히 보는 보조 근거" in answer


def test_hira_mandatory_fact_presence_accepts_comma_formatted_patient_counts() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| HIRA 환자수 | 본태성(원발성) 고혈압(I10) 입원: 16171명 |
| HIRA 환자수 | 본태성(원발성) 고혈압(I10) 외래: 3769201명 |
"""
    lines = mandatory_fact_lines(fact_md)
    answer = "본태성 고혈압 환자는 입원 16,171명, 외래 3,769,201명으로 확인됩니다."

    assert missing_mandatory_lines(answer, lines) == ()


def test_repeated_hira_patient_counts_are_not_rendered_multiple_times() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| HIRA 환자수 | 본태성(원발성) 고혈압(I10) 입원: 16171명 |
| HIRA 환자수 | 본태성(원발성) 고혈압(I10) 외래: 3769201명 |
| HIRA 환자수 | 본태성(원발성) 고혈압(I10) 0_9세: 129명 |
"""
    lines = mandatory_fact_lines(fact_md)
    answer = (
        "리바로하이는 매출 0.67억원, 시장점유율 0.03%입니다. 본태성 고혈압은 입원 16,171명, 외래 3,769,201명입니다.\n"
        "- 진료 형태별: 입원 환자(16,171명) 대비 외래 3,769,201명이 큽니다.\n"
        "- HIRA 환자수: 본태성 고혈압 입원 16171명"
    )

    revised = dedupe_repeated_hira_patient_counts(answer, lines)

    assert revised.count("16,171") == 1
    assert revised.count("3,769,201") == 1
    assert "16171명" not in revised
    assert "0.67억원" in revised
    assert "0.03%" in revised


def test_sanitize_preserves_fact_period_rank_and_delta_tokens_without_blanks() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 브랜드 핵심 지표 | 리바로 2026-04 매출 84.93억원 시장점유율 3.76% 순위 6/516 |
| 시장/브랜드 변화율 대조 | 리바로 2026-01→2026-02 브랜드 변화율 -9.58% 시장 변화율 -9.12% 변화율 차이 -0.46%p |
| 인사이트 계산 | 리바로젯 2025-07→2026-04 상승폭 0.53%p 리피토 2025-07→2026-04 하락폭 -0.56%p 근거 기반 인과 분석: 두 브랜드 점유율 반대 방향 변화, 직접 처방 이동 미확인 |
"""
    strict = strict_allowed_numbers(fact_md, ())
    answer = (
        "리바로는 2026-04 기준 전체 브랜드 중 6/516에 위치합니다. "
        "2026-01과 2026-02에는 시장 변화율 -9.12%와 브랜드 변화율 -9.58%가 동행했고, "
        "리바로젯 상승폭 0.53%p와 리피토 하락폭 -0.56%p는 반대 방향 변화입니다."
    )

    sanitized = _sanitize_preserving_analysis(answer, strict)

    assert "2026-04" in sanitized
    assert "6/516" in sanitized
    assert "2026-01" in sanitized
    assert "2026-02" in sanitized
    assert "-0.46%p" not in sanitized or "-0.46%p" in strict
    assert "0.53%p" in sanitized
    assert "-0.56%p" in sanitized
    assert "93.62%" not in strict
    assert "93.62%" not in sanitized
    assert "()" not in sanitized
    assert "과 에" not in sanitized
    assert "중 에" not in sanitized


def test_sanitize_drops_unsafe_numeric_line_instead_of_leaving_blank_holes() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 브랜드 핵심 지표 | 리바로 2026-04 매출 84.93억원 시장점유율 3.76% 순위 6/516 |
"""
    strict = strict_allowed_numbers(fact_md, ())
    answer = "리바로는 2026-04 기준 84.93억원입니다.\n기사에는 fact 밖 숫자 511억원도 있습니다."

    sanitized = _sanitize_preserving_analysis(answer, strict)

    assert "84.93억원" in sanitized
    assert "511억원" not in sanitized
    assert "숫자 도 있습니다" not in sanitized


def test_hira_sales_link_analysis_connects_patient_pool_to_brand_metric() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 브랜드 핵심 지표 | 리바로하이 2026-04 매출 0.67억원 시장점유율 0.03% 순위 411/1015 |
| HIRA 환자수 | 본태성(원발성) 고혈압(I10) 입원: 16171명 |
| HIRA 환자수 | 본태성(원발성) 고혈압(I10) 외래: 3769201명 |
"""
    answer = "리바로하이는 2026-04 기준 매출 0.67억원, 시장점유율 0.03%, 순위 411/1015입니다."

    revised = ensure_hira_sales_link_analysis("리바로하이 질병 환자수랑 최근 매출 한번에", answer, fact_md)

    assert "질환 환자수와 브랜드 처방 환자는 직접 연결되지 않으므로" in revised
    assert "외래 3769201명" in revised
    assert "입원 16171명" in revised
    assert revised.count("리바로하이는 2026-04 기준 매출 0.67억원") == 1
    assert "환자당 처방액이나 침투율로 환산하지 않고" in revised
    assert "- 브랜드 핵심 지표:" not in revised
    assert "현재 위치를 판단하는 기본 근거" not in revised
    assert "해당 환자수" not in revised


def test_hira_sales_link_removes_standalone_mandatory_completion_lines() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 브랜드 핵심 지표 | 리바로하이 2026-04 매출 0.67억원 시장점유율 0.03% 순위 411/1015 |
| HIRA 환자수 | 본태성(원발성) 고혈압(I10) 외래: 3769201명 |
"""
    answer = (
        "HIRA 기준 본태성(원발성) 고혈압(I10) 외래 환자수는 3769201명입니다.\n"
        "리바로하이는 2026-04 기준 매출 0.67억원, 시장점유율 0.03%, 순위 411/1015입니다.\n\n"
        "## 처리 시간\n- 총 소요: 1초"
    )

    revised = ensure_hira_sales_link_analysis("리바로하이 질병 환자수랑 최근 매출 한번에", answer, fact_md)

    assert revised.count("리바로하이는 2026-04 기준 매출 0.67억원") == 1
    assert "HIRA 기준 본태성(원발성) 고혈압(I10) 외래 환자수는" not in revised
    assert "HIRA에서 본태성(원발성) 고혈압(I10) 외래 3769201명" in revised
    assert "환자당 처방액이나 침투율로 환산하지 않고" in revised
    assert revised.find("환자당 처방액이나 침투율로 환산하지 않고") < revised.find("## 처리 시간")


def test_hira_sales_link_dedupes_repeated_brand_metric_sentence() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 브랜드 핵심 지표 | 리바로하이 2026-04 매출 0.67억원 시장점유율 0.03% 순위 411/1046 |
| HIRA 환자수 | 본태성 고혈압(I10) 외래: 3769201명 |
"""
    answer = (
        "리바로하이는 2026-04 기준 매출 0.67억원, 시장점유율 0.03%, 순위 411/1046입니다.\n\n"
        "리바로하이는 2026-04 기준 매출 0.67억원, 시장점유율 0.03%, 순위 411/1046입니다. "
        "브랜드 매출·점유율·순위는 시장 내 침투 수준과 경쟁 방어 과제를 보여줍니다. "
        "환자 기반 수요 풀 대비 실제 처방 성과의 침투 수준을 읽을 수 있습니다."
    )

    revised = ensure_hira_sales_link_analysis("리바로하이 환자수+매출", answer, fact_md)

    assert revised.count("리바로하이는 2026-04 기준 매출 0.67억원") == 1
    assert "질환 환자수와 브랜드 처방 환자는 직접 연결되지 않으므로" in revised


def test_hira_sales_link_renders_sales_trend_inside_sales_section() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 매출 추이 | 리바로하이 매출 시계열 2025-07 0.00억원 → 2026-04 0.67억원, MS 0.00% → 0.03% |
| HIRA 환자수 | 지질단백질대사장애 및 기타 지질증(E78) 외래: 1305727명 |
| 브랜드 핵심 지표 | 리바로하이 2026-04 매출 0.67억원 시장점유율 0.03% 순위 411/1015 |

### 리바로하이 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-07 | 0.00억원 | 0.00% |
| 2026-04 | 0.67억원 | 0.03% |
"""
    answer = """### 1. 대상 질환별 환자수 현황
외래 환자수 1,305,727명 규모입니다.
### 2. 리바로하이 매출 및 시장 점유율 시계열 분석
리바로하이는 시장 규모의 변동성과 관계없이 독자적인 성장 곡선을 그리고 있습니다.
### 3. 시장 이슈 및 인과적 해석
최근 스타틴 치료 강조가 이어지고 있습니다.

- 매출 추이: 리바로하이 매출 시계열 2025-07 0.00억원 → 2026-04 0.67억원, MS 0.00% → 0.03%
"""

    revised = ensure_hira_sales_link_analysis("리바로하이 질병 환자수랑 최근 매출 한번에", answer, fact_md)

    sales_section = revised.split("### 2. 리바로하이 매출", maxsplit=1)[1].split("### 3.", maxsplit=1)[0]
    assert "2025-07 0.00억원에서 2026-04 0.67억원" in sales_section
    assert "| 기간 | 매출 | MS |" in sales_section
    assert "| 2026-04 | 0.67억원 | 0.03% |" in sales_section
    assert "- 매출 추이:" not in revised
    assert "HIRA에서 지질단백질대사장애 및 기타 지질증(E78) 외래 1305727명" in revised
    assert "환자수 축과 별도로" not in revised


def test_sales_trend_table_satisfies_mandatory_fact_without_raw_tail() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 매출 추이 | 리바로하이 매출 시계열 2025-07 0.00억원 → 2026-04 0.67억원, MS 0.00% → 0.03% |
"""
    answer = """리바로하이는 2025-07 0.00억원에서 2026-04 0.67억원으로 매출이 형성됐고, MS는 0.00%에서 0.03%로 올랐습니다.

| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-07 | 0.00억원 | 0.00% |
| 2026-04 | 0.67억원 | 0.03% |
"""
    lines = mandatory_fact_lines(fact_md)

    assert missing_mandatory_lines(answer, lines) == ()


def test_raw_fact_residue_cleanup_removes_sales_trend_tail_and_meta() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 매출 추이 | 리바로하이 매출 시계열 2025-07 0.00억원 → 2026-04 0.67억원, MS 0.00% → 0.03% |
"""
    answer = """리바로하이는 2025-07 0.00억원에서 2026-04 0.67억원으로 매출이 형성됐고, MS는 0.00%에서 0.03%로 올랐습니다. 이 매출 축은 환자수 축과 별도로 먼저 확인해야 하는 처방 성과의 시계열 기준입니다.

| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-07 | 0.00억원 | 0.00% |
| 2026-04 | 0.67억원 | 0.03% |

- 매출 추이: 리바로하이 매출 시계열 2025-07 0.00억원 → 2026-04 0.67억원, MS 0.00% → 0.03%
* 2025-07: 0.00억원
* 2026-04: 0.67억원
"""

    cleaned = remove_raw_fact_residue(answer, fact_md)

    assert "- 매출 추이:" not in cleaned
    assert "* 2025-07:" not in cleaned
    assert "* 2026-04:" not in cleaned
    assert "환자수 축과 별도로" not in cleaned
    assert "| 2026-04 | 0.67억원 | 0.03% |" in cleaned
    assert "매출이 형성됐고" in cleaned


def test_raw_fact_residue_cleanup_removes_level_agnostic_top_label_tail() -> None:
    answer = """리바로는 의원에서 매출 41.93억원으로 볼륨이 가장 크고, 상급종합병원에서는 시장점유율 4.49%로 상대 경쟁력이 가장 높습니다.

- channel 상위: 1위 의원 시장점유율 3.37% 매출 41.93억원
- channel 상위: 2위 종합병원 시장점유율 4.22% 매출 20.57억원
- channel 상위: 3위 상급종합병원 시장점유율 4.49% 매출 17.64억원
"""

    cleaned = remove_raw_fact_residue(answer, "")

    assert "channel 상위:" not in cleaned
    assert "의원에서 매출 41.93억원" in cleaned
    assert "상급종합병원에서는 시장점유율 4.49%" in cleaned
    assert "| 채널 | 시장점유율 | 매출 |" in cleaned
    assert "| 의원 | 3.37% | 41.93억원 |" in cleaned
    assert "| 종합병원 | 4.22% | 20.57억원 |" in cleaned


def test_hira_patient_summary_is_added_for_patient_questions_when_missing() -> None:
    fact_md = """### HIRA 질병통계 fact
| 구분 | 질병코드 | 질병명 | 환자수 |
| --- | --- | --- | --- |
| 입원 | I10 | 본태성(원발성) 고혈압 | 16171 |
| 외래 | I10 | 본태성(원발성) 고혈압 | 3769201 |
"""
    answer = "리바로하이 2026-04 매출은 0.67억원이고 시장점유율은 0.03%입니다.\n\n## 처리 시간\n- 총 소요: 1초"

    revised = ensure_hira_patient_summary("리바로하이 질병 환자수랑 최근 매출 한번에", answer, fact_md)

    assert "HIRA 환자수: 본태성(원발성) 고혈압(I10) 입원: 16171명" in revised
    assert "HIRA 환자수: 본태성(원발성) 고혈압(I10) 외래: 3769201명" in revised
    assert revised.find("HIRA 환자수") < revised.find("## 처리 시간")
    assert "0.67억원" in revised
    assert "0.03%" in revised


def test_hira_patient_summary_adds_natural_lead_before_existing_table() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 내용 |
| --- | --- |
| HIRA 환자수 | 고지혈증(E78) 2024년 외래 남: 1,305,727명 |
| HIRA 환자수 | 고지혈증(E78) 2024년 외래 여: 1,910,492명 |
"""
    answer = """| 질병코드 | 연도 | 구분 | 성별 | 환자수(명) | 출처 |
| --- | --- | --- | --- | --- | --- |
| E78 | 2024 | 외래 | 남 | 1,305,727 | 건강보험심사평가원 |
| E78 | 2024 | 외래 | 여 | 1,910,492 | 건강보험심사평가원 |
"""

    revised = ensure_hira_patient_summary("고지혈증 환자수", answer, fact_md)

    assert revised.startswith(
        "HIRA 기준 고지혈증(E78) 2024년 외래 남 환자수는 1,305,727명입니다."
    )
    assert "외래 여 환자수는 1,910,492명입니다." in revised
    assert revised.find("HIRA 기준") < revised.find("| 질병코드 |")
    assert "| E78 | 2024 | 외래 | 남 | 1,305,727 | 건강보험심사평가원 |" in revised


def test_hira_patient_summary_reads_tool_use_renderer_evidence() -> None:
    facts = (
        EvidenceFact(
            fact_id="hira:1",
            subject="E78",
            metric="질병 입원/외래 통계",
            value=Decimal("1305727"),
            unit=None,
            period="2024",
            source_name="건강보험심사평가원 통계",
            source_locator="지질단백질대사장애 및 기타 지질증 · 외래 · 남",
            raw_ref="hira:1",
        ),
        EvidenceFact(
            fact_id="hira:2",
            subject="E78",
            metric="질병 입원/외래 통계",
            value=Decimal("1910492"),
            unit=None,
            period="2024",
            source_name="건강보험심사평가원 통계",
            source_locator="지질단백질대사장애 및 기타 지질증 · 외래 · 여",
            raw_ref="hira:2",
        ),
    )
    fact_md = render_evidence_answer(facts)
    answer = """| 질병코드 | 연도 | 구분 | 성별 | 환자수(명) | 출처 |
| --- | --- | --- | --- | --- | --- |
| E78 | 2024 | 외래 | 남 | 1,305,727 | 건강보험심사평가원 |
| E78 | 2024 | 외래 | 여 | 1,910,492 | 건강보험심사평가원 |
"""

    revised = ensure_hira_patient_summary("고지혈증 환자수", answer, fact_md)

    assert revised.startswith(
        "HIRA 기준 지질단백질대사장애 및 기타 지질증(E78) 2024년 외래 남 환자수는 1,305,727명입니다."
    )
    assert "외래 여 환자수는 1,910,492명입니다." in revised
    assert revised.find("HIRA 기준") < revised.find("| 질병코드 |")


def test_genos_final_answer_keeps_hira_natural_lead_before_table(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="고지혈증",
        calls=[
            {
                "tool": "hira_disease_hospitalization_outpatient_stats",
                "source": "hira_disease",
                "render_data": {
                    "request": {"year": "2024"},
                    "items": [
                        {
                            "inpatOpat": "외래 남",
                            "sickCd": "E78",
                            "sickNm": "지질단백질대사장애 및 기타 지질증",
                            "ptntCnt": 1_305_727,
                        },
                        {
                            "inpatOpat": "외래 여",
                            "sickCd": "E78",
                            "sickNm": "지질단백질대사장애 및 기타 지질증",
                            "ptntCnt": 1_910_492,
                        },
                    ],
                },
            }
        ],
        sources=["hira_disease"],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield (
            "**질병 입원/외래 통계 (2024)**\n"
            "| 질병코드 | 질병명 | 구분 | 환자수 | 출처 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| E78 | 지질단백질대사장애 및 기타 지질증 | 외래 남 | 1,305,727 | 건강보험심사평가원 |\n"
            "| E78 | 지질단백질대사장애 및 기타 지질증 | 외래 여 | 1,910,492 | 건강보험심사평가원 |"
        )

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            "고지혈증 환자수",
            {"markdown_response": response.to_dict()},
        )
    )

    first_table = answer.index("| 질병코드 |")
    lead = answer[:first_table]
    assert "1305727명" in lead
    assert "1910492명" in lead
    assert "| E78 | 지질단백질대사장애 및 기타 지질증 | 외래 남 | 1,305,727 |" in answer


def test_hira_patient_unavailable_notice_is_added_when_live_call_returns_no_counts() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 내용 |
| --- | --- |
| HIRA 조회 상태 | I10 본태성 고혈압: 환자수 수치 미반환 |
| HIRA 조회 상태 | E78 지질단백질대사장애 및 기타 지질증: 환자수 수치 미반환 |
"""
    answer = "리바로하이 2026-04 매출은 0.67억원입니다.\n\n## 처리 시간\n- 총 소요: 1초"

    revised = ensure_hira_patient_summary("리바로하이 질병 환자수랑 최근 매출 한번에", answer, fact_md)
    revised = ensure_hira_patient_summary("리바로하이 질병 환자수랑 최근 매출 한번에", revised, fact_md)

    assert "HIRA 조회 상태: I10 본태성 고혈압: 환자수 수치 미반환" in revised
    assert "HIRA 조회 상태: E78 지질단백질대사장애 및 기타 지질증: 환자수 수치 미반환" in revised
    assert revised.count("환자수 수치 미반환") == 2
    assert revised.find("HIRA 조회 상태") < revised.find("## 처리 시간")


def test_single_brand_trend_hook_does_not_inject_fixed_prose() -> None:
    fact_md = """### 리바로 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-11 | 80.35억원 | 3.92% |
| 2025-12 | 90.86억원 | 3.93% |
| 2026-01 | 83.03억원 | 3.81% |
| 2026-02 | 75.08억원 | 3.80% |
| 2026-03 | 87.11억원 | 3.81% |
| 2026-04 | 84.93억원 | 3.76% |

### 리바로 시장규모 시계열 fact
| 기간 | 시장규모 | YoY |
| --- | --- | --- |
| 2025-11 | 2,049.27억원 | 13.73% |
| 2025-12 | 2,310.96억원 | 10.25% |
| 2026-01 | 2,177.00억원 | 8.10% |
| 2026-02 | 1,978.43억원 | 7.62% |
| 2026-03 | 2,288.39억원 | 9.25% |
| 2026-04 | 2,256.77억원 | 7.52% |
"""
    answer = """**시장 성장과의 상관관계 및 시사점**

| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-12 | 90.86억원 | 3.93% |
| 2026-04 | 84.93억원 | 3.76% |
"""

    revised = ensure_single_brand_trend_analysis("리바로 최근 매출 추이 어때", answer, fact_md)

    assert revised == cleanup_markdown_answer(answer)
    assert "내려간 뒤" not in revised
    assert "시장 반등" not in revised
    assert "()" not in revised


def test_single_brand_trend_fact_uses_full_render_data_when_fact_table_is_truncated() -> None:
    fact_md = """### 페린젝트 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2024-Q3 | 24.32억원 | 17.33% |
| 2024-Q4 | 26.78억원 | 18.34% |
| 2025-Q1 | 25.91억원 | 20.56% |
| 2025-Q2 | 27.73억원 | 26.67% |
| 2025-Q3 | 31.84억원 | 24.75% |
| 2025-Q4 | 35.16억원 | 25.36% |

### 페린젝트 시장규모 시계열 fact
| 기간 | 시장규모 | YoY |
| --- | --- | --- |
| 2024-Q3 | 140.38억원 | - |
| 2024-Q4 | 146.04억원 | - |
| 2025-Q1 | 126.03억원 | - |
| 2025-Q2 | 103.97억원 | - |
| 2025-Q3 | 128.65억원 | - |
| 2025-Q4 | 138.63억원 | - |
"""
    calls = [
        {
            "tool": "get_brand_metric",
            "render_data": {
                "brand": "페린젝트",
                "answer_scope": "single_brand_trend",
                "brand_value_series_10pt": [
                    {"period": "2023-Q3", "value_억원": 41.53, "ms_pct": 29.34},
                    {"period": "2023-Q4", "value_억원": 42.30, "ms_pct": 29.95},
                    {"period": "2024-Q1", "value_억원": 36.69, "ms_pct": 26.89},
                    {"period": "2024-Q2", "value_억원": 19.08, "ms_pct": 15.70},
                    {"period": "2024-Q3", "value_억원": 24.32, "ms_pct": 17.33},
                    {"period": "2024-Q4", "value_억원": 26.78, "ms_pct": 18.34},
                    {"period": "2025-Q1", "value_억원": 25.91, "ms_pct": 20.56},
                    {"period": "2025-Q2", "value_억원": 27.73, "ms_pct": 26.67},
                    {"period": "2025-Q3", "value_억원": 31.84, "ms_pct": 24.75},
                    {"period": "2025-Q4", "value_억원": 35.16, "ms_pct": 25.36},
                ],
                "market_size_series": [
                    {"period": "2023-Q3", "value_억원": 141.55},
                    {"period": "2023-Q4", "value_억원": 141.25},
                    {"period": "2024-Q1", "value_억원": 136.47},
                    {"period": "2024-Q2", "value_억원": 121.52},
                    {"period": "2024-Q3", "value_억원": 140.38},
                    {"period": "2024-Q4", "value_억원": 146.04},
                    {"period": "2025-Q1", "value_억원": 126.03},
                    {"period": "2025-Q2", "value_억원": 103.97},
                    {"period": "2025-Q3", "value_억원": 128.65},
                    {"period": "2025-Q4", "value_억원": 138.63},
                ],
            },
        }
    ]
    trend_fact = single_brand_trend_fact_markdown(fact_md, calls)

    assert "shape | recovery" in trend_fact
    assert "peak | 2023-Q4 / 42.30억원 / MS 29.95%" in trend_fact
    assert "trough_after_peak | 2024-Q2 / 19.08억원 / MS 15.70%" in trend_fact
    assert "latest | 2025-Q4 / 35.16억원 / MS 25.36%" in trend_fact
    assert "allowed_periods | 2023-Q3, 2023-Q4, 2024-Q1" in trend_fact
    assert "2025-Q4 / 35.16억원에서 2025-Q4" not in trend_fact


def test_single_brand_trend_fact_marks_sparse_series_without_recovery_template() -> None:
    fact_md = """### 신규브랜드 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-Q3 | 3.00억원 | 1.00% |
| 2025-Q4 | 4.00억원 | 1.20% |
"""
    calls = [
        {
            "tool": "get_brand_metric",
            "render_data": {
                "brand": "신규브랜드",
                "answer_scope": "single_brand_trend",
                "brand_value_series_10pt": [
                    {"period": "2025-Q3", "value_억원": 3.0, "ms_pct": 1.0},
                    {"period": "2025-Q4", "value_억원": 4.0, "ms_pct": 1.2},
                ],
            },
        }
    ]
    trend_fact = single_brand_trend_fact_markdown(fact_md, calls)

    assert "shape | rising" in trend_fact
    assert "first | 2025-Q3 / 3.00억원 / MS 1.00%" in trend_fact
    assert "latest | 2025-Q4 / 4.00억원 / MS 1.20%" in trend_fact
    assert "내려간 뒤" not in trend_fact
    assert "회복했습니다" not in trend_fact


def test_single_brand_trend_prompt_receives_structured_trend_fact() -> None:
    fact_md = """### 리바로 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-11 | 80.35억원 | 3.92% |
| 2025-12 | 90.86억원 | 3.93% |
| 2026-01 | 83.03억원 | 3.81% |
| 2026-02 | 75.08억원 | 3.80% |
| 2026-03 | 87.11억원 | 3.81% |
| 2026-04 | 84.93억원 | 3.76% |

### 리바로 시장규모 시계열 fact
| 기간 | 시장규모 | YoY |
| --- | --- | --- |
| 2025-11 | 2,049.27억원 | 13.73% |
| 2025-12 | 2,310.96억원 | 10.25% |
| 2026-01 | 2,177.00억원 | 8.10% |
| 2026-02 | 1,978.43억원 | 7.62% |
| 2026-03 | 2,288.39억원 | 9.25% |
| 2026-04 | 2,256.77억원 | 7.52% |
"""
    trend_fact = single_brand_trend_fact_markdown(fact_md)
    messages = GenosClient._markdown_messages("리바로 최근 매출 추이 어때", {"fact_md": fact_md}, trend_fact)
    system_prompt = messages[0]["content"]
    user_prompt = messages[1]["content"]

    assert "단일 브랜드 추이 산문용 trend fact가 있으면 표만 쓰지 말고" in system_prompt
    assert "shape가 flat이면 회복·반등이라고 쓰지 말고" in system_prompt
    assert "단일 브랜드 추이 산문용 trend fact:" in user_prompt
    assert "brand | 리바로" in user_prompt
    assert "shape | recovery" in user_prompt
    assert "peak | 2025-12 / 90.86억원 / MS 3.93%" in user_prompt
    assert "trough_after_peak | 2026-02 / 75.08억원 / MS 3.80%" in user_prompt


def test_single_brand_trend_flat_fact_keeps_flat_shape_for_llm() -> None:
    fact_md = """### 베노훼럼 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2024-Q2 | 6.40억원 | 4.20% |
| 2024-Q3 | 7.10억원 | 4.50% |
| 2024-Q4 | 6.95억원 | 4.30% |
| 2025-Q1 | 7.05억원 | 4.40% |
| 2025-Q2 | 6.90억원 | 4.35% |
| 2025-Q3 | 7.00억원 | 4.42% |
"""
    trend_fact = single_brand_trend_fact_markdown(fact_md)

    assert "brand | 베노훼럼" in trend_fact
    assert "grain | quarter" in trend_fact
    assert "shape | flat" in trend_fact
    assert "내려간 뒤" not in trend_fact
    assert "회복했습니다" not in trend_fact


def test_single_brand_trend_weak_rebound_is_not_marked_recovery() -> None:
    fact_md = """### 베노훼럼 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2023-Q3 | 8.83억원 | 6.34% |
| 2024-Q2 | 7.10억원 | 4.50% |
| 2025-Q1 | 6.29억원 | 4.11% |
| 2025-Q4 | 6.61억원 | 4.30% |
"""
    trend_fact = single_brand_trend_fact_markdown(fact_md)

    assert "brand | 베노훼럼" in trend_fact
    assert "shape | flat" in trend_fact
    assert "peak | 2023-Q3 / 8.83억원 / MS 6.34%" in trend_fact
    assert "trough_after_peak | 2025-Q1 / 6.29억원 / MS 4.11%" in trend_fact
    assert "latest | 2025-Q4 / 6.61억원 / MS 4.30%" in trend_fact


def test_single_brand_trend_table_only_answer_requests_llm_prose() -> None:
    table_only = """**베노훼럼 분기별 매출 및 시장 점유율**
| 기간 | 매출액 | 시장 점유율(MS) |
| --- | --- | --- |
| 2025-Q4 | 6.61억원 | 4.77% |
"""
    collapsed_table_only = "**가드렛 매출 시계열**| 기간 | 매출 | MS || --- | --- | --- || 2025-07 | 1.96억원 | 0.23% || 2025-09 | 2.40억원 | 0.26% || 2025-11 | 1.88억원 | 0.23% || 2025-12 | 2.29억원 | 0.25% || 2026-04 | 1.95억원 | 0.22% |"
    with_prose = """베노훼럼은 최근 분기 6억원대 후반에서 좁게 등락했습니다.

| 기간 | 매출액 | 시장 점유율(MS) |
| --- | --- | --- |
| 2025-Q4 | 6.61억원 | 4.77% |
"""

    assert _needs_trend_fact_prose("베노훼럼 매출 추이", table_only)
    assert _needs_trend_fact_prose("가드렛 매출 추이", collapsed_table_only)
    assert not _needs_trend_fact_prose("베노훼럼 매출 추이", with_prose)

    flat_fact = """### 단일 브랜드 추이 산문용 trend fact
| 항목 | 값 |
| --- | --- |
| brand | 베노훼럼 |
| shape | flat |
| first | 2023-Q3 / 8.83억원 / MS 6.34% |
| peak | 2023-Q3 / 8.83억원 / MS 6.34% |
| trough_after_peak | 2025-Q1 / 6.29억원 / MS 4.11% |
| latest | 2025-Q4 / 6.61억원 / MS 4.77% |
"""
    mismatched_prose = "베노훼럼은 2025-Q1 저점 이후 2025-Q4에 반등하며 회복했습니다."

    assert _needs_trend_fact_prose("베노훼럼 매출 추이", mismatched_prose, flat_fact)


def test_single_brand_trend_endpoint_only_answer_requests_rich_trend_prose() -> None:
    trend_fact = """### 단일 브랜드 추이 산문용 trend fact
| 항목 | 값 |
| --- | --- |
| brand | 리바로 |
| shape | recovery |
| first | 2025-07 / 84.76억원 / MS 3.92% |
| peak | 2025-12 / 90.86억원 / MS 3.93% |
| trough_after_peak | 2026-02 / 75.08억원 / MS 3.79% |
| latest | 2026-04 / 84.93억원 / MS 3.76% |
"""
    endpoint_only = "리바로 매출은 2025-07 84.76억원에서 2026-04 84.93억원으로 움직였고, 시장점유율은 3.92%에서 3.76%로 변했습니다."
    rich_prose = "리바로는 2025-07 84.76억원에서 출발해 2025-12 90.86억원으로 고점을 찍은 뒤 2026-02 75.08억원까지 내려갔습니다. 이후 2026-04 84.93억원으로 회복했지만 MS는 3.76%입니다."
    table_with_key_periods_but_weak_prose = f"""| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-07 | 84.76억원 | 3.92% |
| 2025-12 | 90.86억원 | 3.93% |
| 2026-02 | 75.08억원 | 3.79% |
| 2026-04 | 84.93억원 | 3.76% |

{endpoint_only}
"""

    assert _needs_trend_fact_prose("리바로 매출 추이 어때", endpoint_only, trend_fact)
    assert _needs_trend_fact_prose("리바로 매출 추이 어때", table_with_key_periods_but_weak_prose, trend_fact)
    assert not _needs_trend_fact_prose("리바로 매출 추이 어때", rich_prose, trend_fact)


def test_remove_endpoint_only_trend_sentence_after_rich_prose() -> None:
    trend_fact = """### 단일 브랜드 추이 산문용 trend fact
| 항목 | 값 |
| --- | --- |
| brand | 가드렛 |
| shape | recovery |
| first | 2025-07 / 1.96억원 / MS 0.23% |
| peak | 2025-09 / 2.40억원 / MS 0.26% |
| trough_after_peak | 2026-02 / 1.67억원 / MS 0.21% |
| latest | 2026-04 / 1.95억원 / MS 0.22% |
"""
    answer = """가드렛은 2025-07 1.96억원에서 출발해 2025-09 2.40억원으로 고점을 찍은 뒤 2026-02 1.67억원까지 내려갔고, 2026-04 1.95억원으로 일부 회복했습니다.

가드렛 매출은 2025-07 1.96억원에서 2026-04 1.95억원으로 움직였고, 시장점유율은 0.23%에서 0.22%로 변했습니다.
"""

    cleaned = _remove_endpoint_only_trend_sentence(answer, trend_fact)

    assert "2025-09 2.40억원" in cleaned
    assert "2026-02 1.67억원" in cleaned
    assert "움직였고" not in cleaned


def test_trend_prose_fail_closed_reinserts_verified_prose_when_final_cleanup_leaves_only_table() -> None:
    trend_fact = """### 단일 브랜드 추이 산문용 trend fact
| 항목 | 값 |
| --- | --- |
| brand | 리바로 |
| shape | recovery |
| first | 2025-07 / 84.76억원 / MS 3.92% |
| peak | 2025-12 / 90.86억원 / MS 3.93% |
| trough_after_peak | 2026-02 / 75.08억원 / MS 3.79% |
| latest | 2026-04 / 84.93억원 / MS 3.76% |
"""
    table_only = """| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-07 | 84.76억원 | 3.92% |
| 2025-12 | 90.86억원 | 3.93% |
| 2026-02 | 75.08억원 | 3.79% |
| 2026-04 | 84.93억원 | 3.76% |
"""
    rich_prose = "리바로는 2025-07 84.76억원에서 출발해 2025-12 90.86억원으로 고점을 찍은 뒤 2026-02 75.08억원까지 하락했습니다. 이후 2026-04 84.93억원으로 회복했지만 MS는 3.76%입니다."

    revised = _ensure_trend_prose_fail_closed("리바로 매출 추이 어때", table_only, trend_fact, rich_prose)

    assert revised.startswith("리바로는 2025-07")
    assert "| 2025-12 | 90.86억원 | 3.93% |" in revised
    assert not _needs_trend_fact_prose("리바로 매출 추이 어때", revised, trend_fact)


def test_trend_prose_fail_closed_uses_fact_fallback_when_llm_prose_was_stripped() -> None:
    trend_fact = """### 단일 브랜드 추이 산문용 trend fact
| 항목 | 값 |
| --- | --- |
| brand | 리바로 |
| shape | recovery |
| first | 2025-07 / 84.76억원 / MS 3.92% |
| peak | 2025-12 / 90.86억원 / MS 3.93% |
| trough_after_peak | 2026-02 / 75.08억원 / MS 3.79% |
| latest | 2026-04 / 84.93억원 / MS 3.76% |
"""
    table_only = """| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-07 | 84.76억원 | 3.92% |
| 2025-12 | 90.86억원 | 3.93% |
| 2026-02 | 75.08억원 | 3.79% |
| 2026-04 | 84.93억원 | 3.76% |
"""

    revised = _ensure_trend_prose_fail_closed("리바로 매출 추이 어때", table_only, trend_fact, "")

    assert "리바로는 2025-07 84.76억원" in revised
    assert "2025-12 90.86억원" in revised
    assert "2026-02 75.08억원" in revised
    assert "2026-04 84.93억원" in revised
    assert not _needs_trend_fact_prose("리바로 매출 추이 어때", revised, trend_fact)


def test_trend_prose_fail_closed_recognizes_sales_tendency_question() -> None:
    trend_fact = """### 단일 브랜드 추이 산문용 trend fact
| 항목 | 값 |
| --- | --- |
| brand | 리바로 |
| shape | recovery |
| first | 2025-07 / 84.76억원 / MS 3.92% |
| peak | 2025-12 / 90.86억원 / MS 3.93% |
| trough_after_peak | 2026-02 / 75.08억원 / MS 3.79% |
| latest | 2026-04 / 84.93억원 / MS 3.76% |
"""
    table_only = """### 리바로 매출 시계열
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-07 | 84.76억원 | 3.92% |
| 2025-12 | 90.86억원 | 3.93% |
| 2026-02 | 75.08억원 | 3.79% |
| 2026-04 | 84.93억원 | 3.76% |
"""

    revised = _ensure_trend_prose_fail_closed("리바로 매출 경향성 알려줘", table_only, trend_fact, "")

    prose = revised[: revised.index("### 리바로 매출 시계열")]
    assert "2025-12 90.86억원" in prose
    assert "2026-02 75.08억원" in prose
    assert "2026-04 84.93억원" in prose
    assert "회복 흐름" in prose


def test_default_narrative_classification_accepts_natural_market_phrasing() -> None:
    for question in (
        "리바로 매출 경향성 알려줘",
        "리바로 어때",
        "리바로 요즘 상황",
        "리바로 성장하나",
        "리바로 분석해줘",
        "리바로 매출 추세",
        "리바로 매출 알려줘",
        "리바로 점유율",
        "고지혈증 경쟁구도",
    ):
        assert _question_wants_trend_output(question), question

    assert not _question_wants_trend_output("리바로 2025-Q2 매출 얼마?")
    assert not _question_wants_trend_output("리바로 임상시험 어때")


def test_genos_sales_tendency_answer_restores_verified_narrative_before_table(monkeypatch) -> None:
    fact_md = """### 리바로 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-07 | 84.76억원 | 3.92% |
| 2025-12 | 90.86억원 | 3.93% |
| 2026-02 | 75.08억원 | 3.79% |
| 2026-04 | 84.93억원 | 3.76% |
"""
    calls = [
        {
            "tool": "get_brand_metric",
            "source": "UBIST",
            "render_data": {
                "brand": "리바로",
                "answer_scope": "single_brand_trend",
                "brand_value_series_10pt": [
                    {"period": "2025-07", "value_억원": 84.76, "ms_pct": 3.92},
                    {"period": "2025-12", "value_억원": 90.86, "ms_pct": 3.93},
                    {"period": "2026-02", "value_억원": 75.08, "ms_pct": 3.79},
                    {"period": "2026-04", "value_억원": 84.93, "ms_pct": 3.76},
                ],
            },
        }
    ]
    table_only = """### 리바로 매출 시계열
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-07 | 84.76억원 | 3.92% |
| 2025-12 | 90.86억원 | 3.93% |
| 2026-02 | 75.08억원 | 3.79% |
| 2026-04 | 84.93억원 | 3.76% |
"""
    monkeypatch.setattr(GenosClient, "_chat_text", lambda *_args: table_only)

    answer = GenosClient(token="dummy-token")._markdown_answer(
        "리바로 매출 경향성 알려줘",
        {"fact_md": fact_md},
        tool_calls=calls,
    )

    narrative_end = answer.index("**리바로 매출 시계열**")
    narrative = answer[:narrative_end]
    assert "2025-12 90.86억원" in narrative
    assert "2026-02 75.08억원" in narrative
    assert "2026-04 84.93억원" in narrative
    assert answer.index("**리바로 매출 시계열**") < answer.index("| 기간 | 매출 | MS |")


def test_direct_metric_fact_answer_preserves_full_rank_denominator() -> None:
    fact_md = """### 리바로 지표 fact
| 항목 | 값 |
| --- | --- |
| 브랜드/시장 | 리바로 |
| 지표 | market_share |
| 기간 | 2026-04 |
| 매출 | 84.93억원 |
| 시장점유율 | 3.76% |
| 순위 | 6/470 |
"""
    answer = "리바로는 상위권 순위를 수성하고 있으나 시장 확장 속도를 따라잡지 못하고 있습니다."

    revised = _ensure_direct_metric_fact_answer("리바로 점유율 순위", answer, fact_md)

    assert "시장점유율 3.76%" in revised
    assert "순위 6/470" in revised
    assert revised.startswith("리바로는 2026-04 기준")


def test_direct_market_size_question_preserves_market_total() -> None:
    fact_md = """### 리바로젯 지표 fact
| 항목 | 값 |
| --- | --- |
| 브랜드/시장 | 리바로젯 |
| 지표 | market_size |
| 기간 | 2026-04 |
| 매출 | 120.09억원 |
| 시장점유율 | 5.32% |
| 순위 | 3/516 |
| 시장규모 | 2,256.77억원 |
"""
    answer = "리바로젯은 2026-04 기준 매출 120.09억원, 시장점유율 5.32%입니다."

    revised = _ensure_direct_metric_fact_answer("리바로젯 시장 규모 얼마나 돼", answer, fact_md)

    assert "시장 전체는 2026-04 기준 시장규모 2,256.77억원입니다." in revised


def test_ensure_trend_key_period_table_when_prose_mentions_periods_without_table() -> None:
    trend_fact = """### 단일 브랜드 추이 산문용 trend fact
| 항목 | 값 |
| --- | --- |
| brand | 리바로 |
| shape | recovery |
| first | 2025-07 / 84.76억원 / MS 3.92% |
| peak | 2025-12 / 90.86억원 / MS 3.93% |
| trough_after_peak | 2026-02 / 75.08억원 / MS 3.79% |
| latest | 2026-04 / 84.93억원 / MS 3.76% |
"""
    answer = """리바로는 2025-07 84.76억원에서 2025-12 90.86억원으로 고점을 기록한 후 2026-02 75.08억원까지 하락했습니다. 이후 2026-04 84.93억원으로 반등했습니다.

## 처리 시간
- 총 소요: 1초
"""

    revised = _ensure_trend_key_period_table(answer, trend_fact)

    assert "**리바로 핵심 추이 시점**" in revised
    assert "| 2025-12 (Peak) | 90.86억원 | 3.93% |" in revised
    assert "| 2026-02 (Trough) | 75.08억원 | 3.79% |" in revised
    assert revised.find("**리바로 핵심 추이 시점**") < revised.find("## 처리 시간")


def test_code_rendered_trend_table_replaces_llm_key_only_table_with_fact_rows() -> None:
    fact_md = """### 리바로 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-07 | 84.76억원 | 3.92% |
| 2025-11 | 80.35억원 | 3.92% |
| 2025-12 | 90.86억원 | 3.93% |
| 2026-01 | 82.44억원 | 3.84% |
| 2026-02 | 75.08억원 | 3.79% |
| 2026-03 | 87.11억원 | 3.82% |
| 2026-04 | 84.93억원 | 3.76% |
"""
    trend_fact = """### 단일 브랜드 추이 산문용 trend fact
| 항목 | 값 |
| --- | --- |
| brand | 리바로 |
| shape | recovery |
| first | 2025-07 / 84.76억원 / MS 3.92% |
| peak | 2025-12 / 90.86억원 / MS 3.93% |
| trough_after_peak | 2026-02 / 75.08억원 / MS 3.79% |
| latest | 2026-04 / 84.93억원 / MS 3.76% |
"""
    answer = """리바로는 2025-12 고점 이후 2026-02 저점을 거쳐 2026-04 회복했습니다.

**리바로 핵심 추이 시점**
| 기간 | 리바로 매출 | 시장점유율(MS) |
| --- | --- | --- |
| 2025-07 | 84.76억원 | 3.92% |
| 2025-12 | 90.86억원 | 3.93% |
| 2026-02 | 75.08억원 | 3.79% |
| 2026-04 | 84.93억원 | 3.76% |

## 처리 시간
- 총 소요: 1초
"""

    revised = _ensure_code_rendered_trend_table(answer, fact_md, trend_fact)

    assert "**리바로 매출 시계열**" in revised
    assert "| 2025-11 | 80.35억원 | 3.92% |" in revised
    assert "| 2026-01 | 82.44억원 | 3.84% |" in revised
    assert "| 2026-03 | 87.11억원 | 3.82% |" in revised
    assert "**리바로 핵심 추이 시점**" not in revised
    assert revised.find("**리바로 매출 시계열**") < revised.find("## 처리 시간")


def test_insert_trend_llm_prose_before_first_table() -> None:
    answer = """**베노훼럼 분기별 매출 및 시장 점유율**
| 기간 | 매출액 | 시장 점유율(MS) |
| --- | --- | --- |
| 2025-Q4 | 6.61억원 | 4.77% |
"""
    revised = _insert_before_first_table(answer, "베노훼럼은 뚜렷한 반전 없이 6억원대 후반에서 등락했습니다.")

    assert revised.find("뚜렷한 반전 없이") < revised.find("| 기간 | 매출액 |")
    assert revised.count("뚜렷한 반전 없이") == 1


def test_single_brand_trend_fact_table_renders_full_10pt_brand_series() -> None:
    call = {
        "tool": "get_brand_metric",
        "source": "cache",
        "render_data": {
            "brand": "페린젝트",
            "answer_scope": "single_brand_trend",
            "metric": "series",
            "brand_value_series_10pt": [
                {"period": "2023-Q3", "value_억원": 41.53, "ms_pct": 29.34},
                {"period": "2023-Q4", "value_억원": 42.30, "ms_pct": 29.95},
                {"period": "2024-Q1", "value_억원": 36.69, "ms_pct": 26.89},
                {"period": "2024-Q2", "value_억원": 19.08, "ms_pct": 15.70},
                {"period": "2024-Q3", "value_억원": 24.32, "ms_pct": 17.33},
                {"period": "2024-Q4", "value_억원": 26.78, "ms_pct": 18.34},
                {"period": "2025-Q1", "value_억원": 25.91, "ms_pct": 20.56},
                {"period": "2025-Q2", "value_억원": 27.73, "ms_pct": 26.67},
                {"period": "2025-Q3", "value_억원": 31.84, "ms_pct": 24.75},
                {"period": "2025-Q4", "value_억원": 35.16, "ms_pct": 25.36},
            ],
            "market_size_series": [
                {"period": "2023-Q3", "value_억원": 141.55},
                {"period": "2023-Q4", "value_억원": 141.25},
                {"period": "2024-Q1", "value_억원": 136.47},
                {"period": "2024-Q2", "value_억원": 121.52},
                {"period": "2024-Q3", "value_억원": 140.38},
                {"period": "2024-Q4", "value_억원": 146.04},
                {"period": "2025-Q1", "value_억원": 126.03},
                {"period": "2025-Q2", "value_억원": 103.97},
                {"period": "2025-Q3", "value_억원": 128.65},
                {"period": "2025-Q4", "value_억원": 138.63},
            ],
        },
    }

    fact_md = answer_fact_markdown([call], ["cache"])
    brand_section = fact_md.split("### 페린젝트 매출 시계열 fact", 1)[1].split(
        "### 페린젝트 시장규모 시계열 fact",
        1,
    )[0]
    market_section = fact_md.split("### 페린젝트 시장규모 시계열 fact", 1)[1]

    for period in (
        "2023-Q3",
        "2023-Q4",
        "2024-Q1",
        "2024-Q2",
        "2024-Q3",
        "2024-Q4",
        "2025-Q1",
        "2025-Q2",
        "2025-Q3",
        "2025-Q4",
    ):
        assert f"| {period} |" in brand_section
    assert brand_section.find("| 2023-Q3 |") < brand_section.find("| 2023-Q4 |") < brand_section.find("| 2024-Q1 |")
    assert "| 2024-Q1 | 36.69억원 | 26.89% |" in brand_section
    assert "| 2024-Q2 | 19.08억원 | 15.70% |" in brand_section
    assert "| 2025-Q4 | 35.16억원 | 25.36% |" in brand_section
    assert "| 2023-Q4 | 141.25억원 | - |" in market_section
    assert "| 2024-Q2 | 121.52억원 | - |" in market_section
    assert "| 2024-Q1 | 136.47억원 | - |" not in market_section


def test_fact_lookup_markdown_keeps_data_md_when_fact_md_exists() -> None:
    response = {
        "fact_md": "### 필수 답변 fact\n- 리바로는 2026-04 기준 매출 75.04억원입니다.",
        "data_md": "### 페린젝트 매출 시계열 fact\n| 기간 | 매출 | MS |\n| --- | --- | --- |\n| 2025-Q4 | 35.16억원 | 25.36% |",
    }

    lookup = _fact_lookup_markdown(response)

    assert "필수 답변 fact" in lookup
    assert "페린젝트 매출 시계열 fact" in lookup


def test_strict_allowed_numbers_accept_quarter_period_tokens() -> None:
    fact_md = "### 단일 브랜드 추이 산문용 trend fact\n| peak | 2023-Q4 / 매출 42.30억원 |\n| latest | 2025-Q4 / 매출 35.16억원 |"
    allowed = strict_allowed_numbers(fact_md, ())

    assert fact_token_allowed("2023-Q4", allowed)
    assert fact_token_allowed("2025-Q4", allowed)


def test_issue_question_table_only_answer_gets_quant_analysis_prose() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 브랜드 핵심 지표 | 리바로 2026-04 매출 84.93억원 시장점유율 3.76% 순위 6/516 |

### 리바로 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-11 | 80.35억원 | 3.92% |
| 2025-12 | 90.86억원 | 3.93% |
| 2026-01 | 83.03억원 | 3.81% |
| 2026-02 | 75.08억원 | 3.80% |
| 2026-03 | 87.11억원 | 3.81% |
| 2026-04 | 84.93억원 | 3.76% |
"""
    answer = """**2. 스타틴 계열 주요 이슈 및 시장 환경**
최근 리바로가 속한 스타틴 시장의 주요 이슈는 다음과 같습니다.
* 스타틴 기피 현상 대응

**3. 분석 및 시사점**
| 기간 | 매출액 | 시장점유율(MS) |
| --- | --- | --- |
| 2025-11 | 80.35억원 | 3.92% |
| 2026-04 | 84.93억원 | 3.76% |
"""

    revised = ensure_issue_question_quant_analysis("리바로 관련 최근 이슈 뭐 있어", answer, fact_md)

    assert "정량 지표로 보면 리바로는 2025-11 80.35억원에서 2026-04 84.93억원" in revised
    assert "시장점유율은 3.92%에서 3.76%" in revised
    assert "시장 내 순위는 6/516" in revised
    assert revised.find("정량 지표로 보면") < revised.find("| 기간 | 매출액 | 시장점유율(MS) |")


def test_hira_required_rows_prefer_high_level_segments_over_age_tail() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "hira_disease_stats",
                "source": "hira_disease",
                "render_data": {
                    "request": {"year": "2024"},
                    "items": [
                        {"inpatOpat": "입원", "sickCd": "I10", "sickNm": "본태성(원발성) 고혈압", "ptntCnt": 16171},
                        {"inpatOpat": "외래", "sickCd": "I10", "sickNm": "본태성(원발성) 고혈압", "ptntCnt": 3769201},
                        {"age": "0_9세", "sickCd": "I10", "sickNm": "본태성(원발성) 고혈압", "ptntCnt": 129},
                    ]
                },
            }
        ],
        ["hira_disease"],
    )
    lines = mandatory_fact_lines(fact_md)

    assert "- HIRA 환자수: 본태성(원발성) 고혈압(I10) 2024년 입원: 16171명" in lines
    assert "- HIRA 환자수: 본태성(원발성) 고혈압(I10) 2024년 외래: 3769201명" in lines
    assert all("0_9세" not in line for line in lines)
    assert all("129명" not in line for line in lines)


def test_required_facts_render_requested_axes_independently_for_hira_sales() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로하이",
                    "metric": "sales",
                    "period": "2026-04",
                    "answer_scope": "single_brand_trend",
                    "sales_억원": 0.67,
                    "ms_recent_pct": 0.03,
                    "rank": 411,
                    "total_brands_in_market": 1015,
                    "brand_value_series_10pt": [
                        {"period": "2026-02", "value_억원": 0.17, "ms_pct": 0.01},
                        {"period": "2026-03", "value_억원": 0.42, "ms_pct": 0.02},
                        {"period": "2026-04", "value_억원": 0.67, "ms_pct": 0.03},
                    ],
                },
            },
            {
                "tool": "hira_disease_stats",
                "source": "hira_disease",
                "render_data": {
                    "request": {"year": "2024"},
                    "items": [
                        {"inpatOpat": "입원", "sickCd": "I10", "sickNm": "본태성(원발성) 고혈압", "ptntCnt": 16171},
                        {"inpatOpat": "외래", "sickCd": "I10", "sickNm": "본태성(원발성) 고혈압", "ptntCnt": 3769201},
                    ]
                },
            },
        ],
        ["cache", "hira_disease"],
    )
    lines = mandatory_fact_lines(fact_md)

    assert "- 매출 추이: 리바로하이 매출 시계열 2026-02 0.17억원 → 2026-04 0.67억원, MS 0.01% → 0.03%" in lines
    assert "- 브랜드 핵심 지표: 리바로하이 2026-04 매출 0.67억원 시장점유율 0.03% 순위 411/1015" in lines
    assert "- HIRA 환자수: 본태성(원발성) 고혈압(I10) 2024년 입원: 16171명" in lines
    assert fact_md.index("| 매출 추이 |") < fact_md.index("| HIRA 환자수 |")


def test_required_fact_block_is_axis_based_without_cross_suppression_flags() -> None:
    required_source = inspect.getsource(answer_facts_module._required_fact_block)
    module_source = inspect.getsource(answer_facts_module)

    assert "elif " not in required_source
    assert "_REQUIRED_METRIC_AXES" in module_source
    assert "suppress_level_segments" not in module_source
    assert "suppress_top_summary" not in module_source
    assert "has_market_vs_brand_delta" not in module_source
    assert "has_brand_trend_comparison" not in module_source


def test_single_brand_trend_does_not_emit_empty_brand_position_fact() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "sales",
                    "answer_scope": "single_brand_trend",
                    "brand_value_series_10pt": [
                        {"period": "2025-12", "value_억원": 90.86, "ms_pct": 3.93},
                        {"period": "2026-04", "value_억원": 84.93, "ms_pct": 3.76},
                    ],
                },
            }
        ],
        sources=["cache"],
    )

    assert "매출 추이 | 리바로 매출 시계열 2025-12 90.86억원 → 2026-04 84.93억원" in response.fact_md
    assert "브랜드 핵심 지표 | 리바로" not in response.fact_md


def test_cleanup_renumbers_orphaned_section_headings() -> None:
    raw = """**3. 시사점 및 한계**
리바로는 시장 반등 구간에서 점유율 방어가 핵심 과제입니다.

### 5. 후속 관찰
점유율 재상승 여부를 확인해야 합니다.
"""

    cleaned = cleanup_markdown_answer(raw)

    assert "**1. 시사점 및 한계**" in cleaned
    assert "### 2. 후속 관찰" in cleaned
    assert "**3. 시사점 및 한계**" not in cleaned
    assert "### 5. 후속 관찰" not in cleaned


def test_empty_news_shell_is_replaced_with_cited_fact() -> None:
    fact_md = """### 인사이트 근거 fact - 뉴스/이슈
| 날짜 | 제목 | 출처 | 요약 | 매칭 발췌 |
| --- | --- | --- | --- | --- |
| 2026-04-01 | 아토젯 시장 이슈 | 약업신문 | 아토젯 처방 경쟁 맥락이 기사 요약에 포함됐다. | 아토젯 관련 본문 발췌 |
"""
    answer = "정량 지표는 확인됩니다.\n\n- 뉴스: 약업신문 관련 기사에서 아토젯 언급이 확인됐습니다.\n\n출처: UBIST, 내부 심층분석"

    revised = replace_empty_news_shells(answer, fact_md)

    assert "관련 기사에서" not in revised
    assert "언급이 확인됐습니다" not in revised
    assert "약업신문(2026-04-01) 「아토젯 시장 이슈」" in revised
    assert "아토젯 처방 경쟁 맥락" in revised


def test_fallback_fact_answer_weaves_sales_delta_and_news_without_numeric_article_leakage() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 매출 변화 | 리바로 2026-03→2026-04: 87.11억원 → 84.93억원, 변화 -2.18억원(-2.50%) |
| 매출 변화 | 아토젯 2026-03→2026-04: 119.49억원 → 116.49억원, 변화 -3.00억원(-2.51%) |

### 인사이트 근거 fact - 뉴스/이슈
| 날짜 | 제목 | 출처 | 요약 | 매칭 발췌 |
| --- | --- | --- | --- | --- |
| 2026-04-01 | 상장제약사 1분기 제품 분석 | 약업신문 | 리바로 패밀리 매출 511억원, 아토젯 261억원으로 전년 대비 -1.80% 감소 | 리바로 패밀리 매출 511억원, 아토젯 261억원 |

### 출처 유형 fact
| 출처 |
| --- |
| UBIST |
| 내부 심층분석 |
"""

    answer = fallback_fact_answer({"fact_md": fact_md})

    assert "리바로와 아토젯의 2026-03→2026-04 구간 매출 흐름은 감소했습니다" in answer
    assert "감소했습니다" in answer
    assert "같은 방향으로 움직인 것으로 해석됩니다" in answer
    assert "단기 경쟁 압력의 배경 근거" in answer
    assert "약업신문(2026-04-01) 「상장제약사 1분기 제품 분석」" in answer
    assert "511억원" not in answer
    assert "261억원" not in answer
    assert "-1.80%" not in answer
    assert "수치" not in answer


def test_fallback_fact_answer_does_not_claim_same_direction_when_sales_delta_diverges() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 매출 변화 | 아토젯 2026-04→2026-04: 116.49억원 → 116.49억원, 변화 0.00억원(0.00%) |
| 매출 변화 | 리바로 2026-03→2026-04: 87.11억원 → 84.93억원, 변화 -2.18억원(-2.50%) |
"""

    answer = fallback_fact_answer({"fact_md": fact_md})

    assert "아토젯와" not in answer
    assert "아토젯과 리바로" in answer
    assert "엇갈렸습니다" in answer
    assert "서로 다른 방향 또는 강도" in answer
    assert "같은 방향으로 움직인" not in answer


def test_fallback_fact_answer_summarizes_top_brand_trend() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| Brand 상위 | 1위 로수젯 시장점유율 9.17% 매출 206.85억원 |
| Brand 상위 | 2위 리피토 시장점유율 6.39% 매출 144.22억원 |
| Brand 상위 | 3위 리바로젯 시장점유율 5.32% 매출 120.09억원 |
| 상위 브랜드 추이 | 1위 로수젯 최신 시장점유율 9.17% 점유율 변화 -0.11%p 최신 매출 206.85억원 매출 변화 -7.68억원 |
| 상위 브랜드 추이 | 2위 리피토 최신 시장점유율 6.39% 점유율 변화 -0.56%p 최신 매출 144.22억원 매출 변화 -15.00억원 |
| 상위 브랜드 추이 | 3위 리바로젯 최신 시장점유율 5.32% 점유율 변화 +0.53%p 최신 매출 120.09억원 매출 변화 +12.00억원 |
"""

    answer = fallback_fact_answer({"fact_md": fact_md})

    assert answer.startswith("조회 결과에서 로수젯이 선두를 지키고 있으며")
    assert "시장점유율 9.17%(매출 206.85억원)" in answer.split("\n\n", 1)[0]
    assert "확인된 값은" not in answer
    assert "상승 폭이 큰 쪽은 리바로젯(+0.53%p)입니다" in answer
    assert "하락 폭이 큰 쪽은 리피토(-0.56%p)입니다" in answer
    assert "상위권 점유율·매출 변화" in answer


def test_natural_fact_lead_precedes_existing_sales_table_without_replacing_it() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 브랜드 핵심 지표 | 리바로 2026-05 매출 80.39억원 시장점유율 3.52% 순위 7/516 |
"""
    answer = """### 리바로 핵심 지표 (2026-05)
| 지표 | 값 |
| --- | --- |
| 매출 | 80.39억원 |
| 시장점유율 | 3.52% |

### 뉴스/이슈
- 관련 뉴스가 이어지고 있습니다.
"""

    revised = ensure_natural_fact_lead("리바로 최근 매출 어때", answer, fact_md)

    assert revised.startswith(
        "리바로는 2026-05 기준 매출 80.39억원을 기록하고 있으며, "
        "시장점유율 3.52%와 순위 7/516으로 확인됩니다."
    )
    assert "### 리바로 핵심 지표 (2026-05)" in revised
    assert "| 매출 | 80.39억원 |" in revised
    assert "### 뉴스/이슈" in revised


def test_natural_fact_lead_precedes_verified_competition_table_without_replacing_it() -> None:
    answer = """구체적으로는 로수젯 시장점유율 9.13%, 매출 195.24억원입니다.

| 순위 | 브랜드 | 점유율 | 매출 |
| --- | --- | --- | --- |
| 1위 | 로수젯 | 9.13% | 195.24억원 |
| 2위 | 리피토 | 6.13% | 131.09억원 |
| 3위 | 리바로젯 | 5.12% | 109.46억원 |

관련 이슈 맥락

- 뉴스: 경쟁 관련 기사
"""

    revised = ensure_natural_fact_lead("리바로 경쟁구도 어떻게 변하고 있어", answer, "")

    assert revised.startswith(
        "리바로 경쟁구도를 보면 로수젯이 9.13%(195.24억원)로 선두이며, "
        "리피토·리바로젯이 뒤를 잇고 있습니다."
    )
    assert "| 1위 | 로수젯 | 9.13% | 195.24억원 |" in revised
    assert "관련 이슈 맥락" in revised
    assert "- 뉴스: 경쟁 관련 기사" in revised


def test_fallback_fact_answer_uses_agent2_insight_signals() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| Brand 상위 | 1위 로수젯 시장점유율 9.17% 매출 206.85억원 |
| Brand 상위 | 2위 리피토 시장점유율 6.39% 매출 144.22억원 |
| Brand 상위 | 3위 리바로젯 시장점유율 5.32% 매출 120.09억원 |
| 상위 브랜드 추이 | 2위 리피토 최신 시장점유율 6.39% 2025-07→2026-04 점유율 변화 -0.56%p 최신 매출 144.22억원 매출 변화 -15.00억원 |
| 상위 브랜드 추이 | 3위 리바로젯 최신 시장점유율 5.32% 2025-07→2026-04 점유율 변화 +0.53%p 최신 매출 120.09억원 매출 변화 +12.00억원 |
| 인사이트 계산 | 리바로젯 share-of-growth 24.00% 성장분해 시장 +12.00%p/점유 +0.53%p, cohort 백분위 100.00% |
"""

    answer = fallback_fact_answer({"fact_md": fact_md})

    assert "share-of-growth 24.00%" in answer
    assert "시장 성장 기여" in answer
    assert "리바로젯이 시장 성장 기여" in answer
    assert "하락폭 대비" not in answer
    assert "so-what" not in answer.lower()


def test_presentable_mandatory_lines_hide_raw_insight_fact_label() -> None:
    lines = (
        "- 인사이트 계산: 리바로젯 share-of-growth 17.35% 성장분해 시장 4.39% 점유 0.53%p 시장 변화 94.84억원 cohort z-score 1.65 백분위 100.00%",
        "- 인사이트 계산: 리바로젯 2025-07→2026-04 상승폭 0.53%p 리피토 2025-07→2026-04 하락폭 -0.56%p 근거 기반 인과 분석: 두 브랜드 점유율 반대 방향 변화, 직접 처방 이동 미확인",
    )

    rendered = "\n".join(presentable_mandatory_lines(lines))

    assert "인사이트 계산" not in rendered
    assert "- 인사이트:" in rendered
    assert "share-of-growth 17.35%" in rendered
    assert "리피토 하락폭 -0.56%p" in rendered
    assert "2026-04 하락폭" not in rendered
    assert "직접 처방 이동은 확인할 수 없습니다" in rendered


def test_market_brand_heading_only_answer_gets_deterministic_conclusion() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 시장/브랜드 변화율 대조 | 리바로 2026-01→2026-02 브랜드 매출 83.03억원 → 75.08억원 브랜드 변화율 -9.58% 시장 매출 2,177.00억원 → 1,978.43억원 시장 변화율 -9.12% 변화율 차이 -0.46%p 근거 기반 인과 분석: 시장 동반 하락이 주요 배경으로 해석됨 |
"""
    answer = """**시장 및 브랜드 매출 변화 (2026-01 ~ 2026-02)**
| 구분 | 2026-01 매출 | 2026-02 매출 | 변화율 |
| --- | --- | --- | --- |
| 리바로 | 83.03억원 | 75.08억원 | -9.58% |
| 시장 전체 | 2,177.00억원 | 1,978.43억원 | -9.12% |

**근거 기반 인과 분석 및 시사점**

해당 기간 리바로의 매출 하락 원인을 직접적으로 설명하는 뉴스나 외부 이슈는 확인되지 않았습니다."""

    revised = ensure_judgment_insight("리바로 2월 매출 하락이 시장 영향인지 브랜드 고유인지 봐줘", answer, fact_md)

    assert revised.startswith("결론:")
    assert "리바로 변화율은 -9.58%" in revised
    assert "시장 변화율은 -9.12%" in revised
    assert "차이가 -0.46%p" in revised
    assert "시장 전반 조정" in revised


def test_source_line_moves_after_safety_added_insight_bullets() -> None:
    answer = """경쟁 구도는 로수젯이 선두입니다.

출처: UBIST, 내부 심층분석

- 인사이트: 리바로젯이 share-of-growth 17.35%로 상위권 변화의 질을 설명합니다."""

    revised = normalize_source_line_position(answer)

    assert revised.rfind("출처:") > revised.rfind("- 인사이트:")
    assert revised.count("출처:") == 1


def test_cleanup_markdown_answer_fixes_common_korean_particle_artifacts() -> None:
    answer = cleanup_markdown_answer(
        "아토젯는 119.49억원에서 116.49억원로 변했습니다. "
        "점유율 이동은 93.62%에 해당합니다이며, 인과는 단정하지 않습니다. "
        "리피토은 하락했습니다."
    )

    assert "아토젯은" in answer
    assert "116.49억원으로" in answer
    assert "해당하며" in answer
    assert "리피토는" in answer
    assert "아토젯는" not in answer
    assert "억원로" not in answer
    assert "해당합니다이며" not in answer


def test_cleanup_markdown_answer_repairs_common_itneun_typo_only() -> None:
    answer = cleanup_markdown_answer("리바로젯은 성장하고 있은 반면, 시장은 정체되고 있은 흐름입니다.")

    assert "성장하고 있는" in answer
    assert "정체되고 있는" in answer
    assert "있은" not in answer


def test_cleanup_markdown_answer_preserves_article_titles_verbatim() -> None:
    raw = "뉴스: 메디칼업저버 (2026-06-11) 「뇌졸중 후유증 막는 '스마트 나노 치료 기술' 개발」 https://example.test"

    cleaned = cleanup_markdown_answer(raw)

    assert "「뇌졸중 후유증 막는 '스마트 나노 치료 기술' 개발」" in cleaned
    assert "「뇌졸중 후유증 막은 '스마트 나노 치료 기술' 개발」" not in cleaned


def test_answer_facts_prefer_top_trend_when_snapshot_segments_are_zero() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "페린젝트",
                    "level": "Brand",
                    "level_segments": [
                        {"rank": 1, "name": "노페로", "ms_recent_pct": 0.0, "value": 0.0},
                        {"rank": 2, "name": "모노퍼", "ms_recent_pct": 0.0, "value": 0.0},
                    ],
                    "level_top5_trend_series": [
                        {
                            "rank": 1,
                            "brand": "훼로바유",
                            "ms_recent_pct": 25.85,
                            "share_delta_pctp": 1.23,
                            "value_recent_억원": 125.0,
                            "value_delta_억원": 3.0,
                            "series": [{"period": "2025-Q4", "ms_pct": 25.85, "value_억원": 125.0, "rank": 1}],
                        },
                        {
                            "rank": 2,
                            "brand": "페린젝트",
                            "ms_recent_pct": 25.36,
                            "share_delta_pctp": -1.47,
                            "value_recent_억원": 122.0,
                            "value_delta_억원": -2.0,
                            "series": [{"period": "2025-Q4", "ms_pct": 25.36, "value_억원": 122.0, "rank": 2}],
                        },
                    ],
                },
            }
        ],
        ["cache"],
    )

    assert "Brand 상위: 1위 노페로 시장점유율 0.00%" not in fact_md
    assert "| 상위 브랜드 추이 | 1위 훼로바유 2025-Q4 MS 25.85% → 2025-Q4 MS 25.85%" in fact_md
    assert "| 상위 브랜드 추이 | 2위 페린젝트 2025-Q4 MS 25.36% → 2025-Q4 MS 25.36%" in fact_md
    assert "1.23%p" not in fact_md
    assert "-1.47%p" not in fact_md
    assert "점유율 변화 표시 보류" in fact_md


def test_top_trend_fact_uses_actual_axis_label_for_dosage_form() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "level": "dosage_form",
                    "level_top5_trend_series": [
                        {
                            "rank": 1,
                            "brand": "Statin/EZE",
                            "name": "Statin/EZE",
                            "ms_recent_pct": 59.05,
                            "share_delta_pctp": 1.05,
                            "value_recent_억원": 330.0,
                            "value_delta_억원": 10.0,
                            "series": [
                                {"period": "2025-07", "ms_pct": 58.0, "value_억원": 320.0, "rank": 1},
                                {"period": "2026-04", "ms_pct": 59.05, "value_억원": 330.0, "rank": 1},
                            ],
                        },
                        {
                            "rank": 2,
                            "brand": "Statin",
                            "name": "Statin",
                            "ms_recent_pct": 40.95,
                            "share_delta_pctp": -1.05,
                            "value_recent_억원": 228.0,
                            "value_delta_억원": -10.0,
                            "series": [
                                {"period": "2025-07", "ms_pct": 42.0, "value_억원": 238.0, "rank": 2},
                                {"period": "2026-04", "ms_pct": 40.95, "value_억원": 228.0, "rank": 2},
                            ],
                        }
                    ],
                },
            }
        ],
        ["cache"],
    )

    assert "| 상위 제형 추이 | 1위 Statin/EZE 2025-07 MS 58.00% → 2026-04 MS 59.05%" in fact_md
    assert "상위 브랜드 추이 | 1위 Statin/EZE" not in fact_md
    assert "### 상위 제형 점유율 추이 fact" in fact_md
    assert "| 최신 순위 | 제형 | 시작 MS | 최신 MS | MS 변화 | 최신 매출 | 매출 변화 |" in fact_md
    assert "※ 본 시장의 제형 구분은 성분 조합 기준(예: Statin/EZE vs Statin)입니다." in fact_md


def test_top_trend_fact_does_not_add_dosage_form_note_for_physical_forms() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "level": "dosage_form",
                    "level_top5_trend_series": [
                        {
                            "rank": 1,
                            "brand": "정제",
                            "name": "정제",
                            "ms_recent_pct": 70.0,
                            "share_delta_pctp": 1.0,
                            "value_recent_억원": 70.0,
                            "series": [
                                {"period": "2025-07", "ms_pct": 69.0, "value_억원": 69.0, "rank": 1},
                                {"period": "2026-04", "ms_pct": 70.0, "value_억원": 70.0, "rank": 1},
                            ],
                        },
                        {
                            "rank": 2,
                            "brand": "캡슐",
                            "name": "캡슐",
                            "ms_recent_pct": 30.0,
                            "share_delta_pctp": -1.0,
                            "value_recent_억원": 30.0,
                            "series": [
                                {"period": "2025-07", "ms_pct": 31.0, "value_억원": 31.0, "rank": 2},
                                {"period": "2026-04", "ms_pct": 30.0, "value_억원": 30.0, "rank": 2},
                            ],
                        },
                    ],
                },
            }
        ],
        ["cache"],
    )

    assert "### 상위 제형 점유율 추이 fact" in fact_md
    assert "성분 조합 기준" not in fact_md


def test_top_trend_fact_uses_latest_series_sales_when_recent_sales_field_is_missing() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "level": "dosage_form",
                    "level_top5_trend_series": [
                        {
                            "rank": 1,
                            "brand": "Statin/EZE",
                            "name": "Statin/EZE",
                            "from_period": "2025-05",
                            "from_ms_pct": 55.63,
                            "to_period": "2026-04",
                            "to_ms_pct": 59.05,
                            "share_delta_pctp": 3.42,
                            "series": [
                                {"period": "2025-05", "ms_pct": 55.63, "value_억원": 1_100.00, "rank": 1},
                                {"period": "2026-04", "ms_pct": 59.05, "value_억원": 1_332.65, "rank": 1},
                            ],
                        }
                    ],
                },
            }
        ],
        ["cache"],
    )

    assert "최신 매출 1,332.65억원" in fact_md
    assert "| 1 | Statin/EZE | 2025-05 55.63% | 2026-04 59.05% | 3.42%p | 1,332.65억원 | - |" in fact_md
    assert "3.42p" not in fact_md


def test_top_brand_trend_fact_surfaces_reproducible_start_latest_and_delta() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "series",
                    "period": "2026-04",
                    "level_top5_trend_series": [
                        {
                            "rank": 3,
                            "brand": "리바로젯",
                            "ms_recent_pct": 5.32,
                            "from_period": "2025-07",
                            "from_ms_pct": 4.79,
                            "to_period": "2026-04",
                            "to_ms_pct": 5.32,
                            "share_delta_pctp": 0.53,
                            "value_recent_억원": 120.09,
                            "value_delta_억원": 8.0,
                            "series": [
                                {"period": "2025-07", "ms_pct": 4.79, "value_억원": 112.09, "rank": 3},
                                {"period": "2026-04", "ms_pct": 5.32, "value_억원": 120.09, "rank": 3},
                            ],
                        }
                    ],
                },
            }
        ],
        ["cache"],
    )

    assert "2025-07 MS 4.79% → 2026-04 MS 5.32%" in fact_md
    assert "2025-07→2026-04 점유율 변화 0.53%p" in fact_md
    assert "| 최신 순위 | 브랜드 | 시작 MS | 최신 MS | MS 변화 | 최신 매출 | 매출 변화 |" in fact_md
    assert "| 3 | 리바로젯 | 2025-07 4.79% | 2026-04 5.32% | 0.53%p | 120.09억원 | 8.00억원 |" in fact_md


def test_top_brand_trend_fact_blocks_non_reproducible_delta() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "series",
                    "period": "2026-04",
                    "level_top5_trend_series": [
                        {
                            "rank": 3,
                            "brand": "리바로젯",
                            "ms_recent_pct": 5.31,
                            "from_period": "2025-07",
                            "from_ms_pct": 4.79,
                            "to_period": "2026-04",
                            "to_ms_pct": 5.31,
                            "share_delta_pctp": 0.53,
                            "value_recent_억원": 120.09,
                            "value_delta_억원": 8.0,
                            "series": [
                                {"period": "2025-07", "ms_pct": 4.79, "value_억원": 112.09, "rank": 3},
                                {"period": "2026-04", "ms_pct": 5.31, "value_억원": 120.09, "rank": 3},
                            ],
                        }
                    ],
                },
            }
        ],
        ["cache"],
    )

    assert "2025-07 MS 4.79% → 2026-04 MS 5.31%" in fact_md
    assert "0.53%p" not in fact_md
    assert "점유율 변화 표시 보류" in fact_md
    assert "| 3 | 리바로젯 | 2025-07 4.79% | 2026-04 5.31% | 표시 보류 | 120.09억원 | 8.00억원 |" in fact_md


def test_missing_mandatory_requires_top_brand_start_latest_and_delta_values() -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "series",
                    "period": "2026-04",
                    "level_top5_trend_series": [
                        {
                            "rank": 3,
                            "brand": "리바로젯",
                            "from_period": "2025-07",
                            "from_ms_pct": 4.79,
                            "to_period": "2026-04",
                            "to_ms_pct": 5.32,
                            "share_delta_pctp": 0.53,
                            "series": [
                                {"period": "2025-07", "ms_pct": 4.79, "rank": 3},
                                {"period": "2026-04", "ms_pct": 5.32, "rank": 3},
                            ],
                        }
                    ],
                },
            }
        ],
        ["cache"],
    )
    mandatory = mandatory_fact_lines(fact_md)

    missing = missing_mandatory_lines(
        "리바로젯의 2025-07→2026-04 점유율 상승폭 0.53%p입니다. "
        "| 3위 | 리바로젯 | 5.32% |",
        mandatory,
    )

    assert any("상위 브랜드 추이" in line and "4.79%" in line for line in missing)


def test_top_brand_trend_table_is_code_rendered_when_llm_omits_start_value() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 내용 |
| --- | --- |
| 상위 브랜드 추이 | 3위 리바로젯 2025-07 MS 4.79% → 2026-04 MS 5.32% 2025-07→2026-04 점유율 변화 0.53%p 최신 매출 120.09억원 매출 변화 16.46억원 |
"""

    answer = "리바로젯은 2026-04 MS 5.32%, 점유율 변화 0.53%p입니다."

    revised = ensure_top_brand_trend_table(answer, fact_md)

    assert "| 최신 순위 | 브랜드 | 시작 MS | 최신 MS | MS 변화 | 최신 매출 | 매출 변화 |" in revised
    assert "| 3 | 리바로젯 | 2025-07 4.79% | 2026-04 5.32% | 0.53%p | 120.09억원 | 16.46억원 |" in revised


def test_top_brand_trend_table_replaces_llm_table_when_latest_sales_is_missing() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 내용 |
| --- | --- |
| 상위 제형 추이 | 1위 Statin/EZE 2025-05 MS 55.63% → 2026-04 MS 59.05% 2025-05→2026-04 점유율 변화 3.42%p 최신 매출 1,332.65억원 |
"""
    answer = """### 상위 제형 추이
| 최신 순위 | 제형 | 시작 MS | 최신 MS | MS 변화 | 최신 매출 | 매출 변화 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | Statin/EZE | 2025-05 55.63% | 2026-04 59.05% | 3.42%p | - | - |
"""

    revised = ensure_top_brand_trend_table(answer, fact_md)

    assert "| 1 | Statin/EZE | 2025-05 55.63% | 2026-04 59.05% | 3.42%p | 1,332.65억원 | - |" in revised
    assert "| 1 | Statin/EZE | 2025-05 55.63% | 2026-04 59.05% | 3.42%p | - | - |" not in revised


def test_top_brand_trend_table_replaces_raw_mandatory_completion_line() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 내용 |
| --- | --- |
| 상위 브랜드 추이 | 3위 리바로젯 2025-07 MS 4.79% → 2026-04 MS 5.32% 2025-07→2026-04 점유율 변화 0.53%p 최신 매출 120.09억원 매출 변화 16.46억원 |
"""
    raw_line = (
        "- 상위 브랜드 추이: 3위 리바로젯 2025-07 MS 4.79% → 2026-04 MS 5.32% "
        "2025-07→2026-04 점유율 변화 0.53%p 최신 매출 120.09억원 매출 변화 16.46억원"
    )

    revised = ensure_top_brand_trend_table(f"요약\n\n{raw_line}", fact_md)

    assert raw_line not in revised
    assert "| 3 | 리바로젯 | 2025-07 4.79% | 2026-04 5.32% | 0.53%p | 120.09억원 | 16.46억원 |" in revised


def test_top_trend_table_uses_dynamic_axis_label_for_non_brand_dimensions() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 내용 |
| --- | --- |
| 상위 제형 추이 | 1위 Statin/EZE 2025-07 MS 58.00% → 2026-04 MS 59.05% 2025-07→2026-04 점유율 변화 1.05%p 최신 매출 330.00억원 매출 변화 10.00억원 |
| 상위 제형 추이 | 2위 Statin 2025-07 MS 42.00% → 2026-04 MS 40.95% 2025-07→2026-04 점유율 변화 -1.05%p 최신 매출 228.00억원 매출 변화 -10.00억원 |
"""
    raw_line = (
        "- 상위 제형 추이: 1위 Statin/EZE 2025-07 MS 58.00% → 2026-04 MS 59.05% "
        "2025-07→2026-04 점유율 변화 1.05%p 최신 매출 330.00억원 매출 변화 10.00억원"
    )

    revised = ensure_top_brand_trend_table(f"요약\n\n{raw_line}", fact_md)

    assert "### 상위 제형 추이" in revised
    assert "| 최신 순위 | 제형 | 시작 MS | 최신 MS | MS 변화 | 최신 매출 | 매출 변화 |" in revised
    assert "| 최신 순위 | 브랜드 |" not in revised
    assert raw_line not in revised
    assert "※ 본 시장의 제형 구분은 성분 조합 기준(예: Statin/EZE vs Statin)입니다." in revised


def test_top_trend_table_deduplicates_dosage_form_combination_note() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 내용 |
| --- | --- |
| 상위 제형 추이 | 1위 Statin/EZE 2025-07 MS 58.00% → 2026-04 MS 59.05% 2025-07→2026-04 점유율 변화 1.05%p 최신 매출 330.00억원 매출 변화 10.00억원 |
| 상위 제형 추이 | 2위 Statin 2025-07 MS 42.00% → 2026-04 MS 40.95% 2025-07→2026-04 점유율 변화 -1.05%p 최신 매출 228.00억원 매출 변화 -10.00억원 |
"""
    answer = """### 상위 제형 추이
| 최신 순위 | 제형 | 시작 MS | 최신 MS | MS 변화 | 최신 매출 | 매출 변화 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | Statin/EZE | 2025-07 58.00% | 2026-04 59.05% | 1.05%p | - | 10.00억원 |
※ 본 시장의 제형 구분은 성분 조합 기준(예: Statin/EZE vs Statin)입니다.
"""

    revised = ensure_top_brand_trend_table(answer, fact_md)

    assert revised.count("성분 조합 기준") == 1
    assert "| 1 | Statin/EZE | 2025-07 58.00% | 2026-04 59.05% | 1.05%p | 330.00억원 | 10.00억원 |" in revised


def test_model_compare_config_sets_bounded_runtime_timeouts(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_COMPARE_TIMEOUT_S", "222")
    monkeypatch.setenv("MODEL_COMPARE_GENERATION_ATTEMPTS", "4")
    for key in (
        "GENOS_PLANNER_SERVING_ID",
        "GENOS_FINAL_SERVING_ID",
        "GENOS_PLANNER_BEARER_TOKEN",
        "GENOS_FINAL_BEARER_TOKEN",
        "GENOS_BEARER_TOKEN",
        "GENOS_AGENT_TIMEOUT_S",
        "GENOS_FINAL_TIMEOUT_S",
        "GENOS_ROUTER_TIMEOUT_S",
        "GENOS_GENERATION_ATTEMPTS",
    ):
        monkeypatch.setenv(key, "")
    from scripts.runtime_model_compare_runner import _configure_model

    _configure_model("163", "76", "planner-token", "final-token")

    assert GenosToolPlanner().timeout_s == 222
    assert GenosClient().timeout_s == 222
    assert GenosClient().token == "final-token"
    assert GenosToolPlanner().token == "planner-token"
    assert GenosClient().base_url.endswith("/serving/76")
    assert GenosToolPlanner().base_url.endswith("/serving/163")


def test_hira_sales_markdown_stream_uses_llm_fact_path(monkeypatch) -> None:
    seen_prompt: dict[str, str] = {}

    def stream_chat(_self: GenosClient, messages: list[dict[str, str]]):
        seen_prompt["user"] = messages[1]["content"]
        yield "리바로하이는 최근 매출 31.00억원과 HIRA I10 입원 환자수 16,171명을 함께 확인했습니다.\n\n출처: UBIST, HIRA 질병정보서비스"

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)
    response = MarkdownResponseBuilder().build(
        brand="리바로하이",
        calls=[
            {
                "tool": "get_disease_stats",
                "source": "hira_disease",
                "render_data": {
                    "calls": [
                        {
                            "render_data": {
                                "request": {"year": "2024"},
                                "items": [
                                    {
                                        "inpatOpat": "입원",
                                        "sickCd": "I10",
                                        "sickNm": "본태성(원발성) 고혈압",
                                        "ptntCnt": 16171,
                                    }
                                ]
                            }
                        }
                    ]
                },
            },
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로하이",
                    "metric": "sales",
                    "period": "2026-04",
                    "sales_억원": 31.0,
                },
            }
        ],
        sources=["cache", "hira_disease"],
    )

    answer = "".join(GenosClient(token="dummy-token").stream_answer("리바로하이 질병 환자수랑 최근 매출", {"markdown_response": response.to_dict()}))

    assert "최근 매출 31.00억원" in answer
    assert "입원 환자수 16,171명" in answer
    assert "## 해석" not in answer
    assert "데이터 표에서 한 번" not in answer
    assert "31.00억원" in seen_prompt["user"]
    assert "HIRA 환자수" in seen_prompt["user"]
    assert "HIRA 질병통계 fact" in seen_prompt["user"]
    assert "숫자 검증" not in answer


def test_genos_answer_drops_unsupported_claim_when_top_trend_fact_exists(monkeypatch) -> None:
    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield "아토젯 시계열은 확인 안 됨입니다. 리바로는 2026-04 기준 3.76%입니다.\n\n출처: UBIST"

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "series",
                    "period": "2026-04",
                    "level_top5_trend_series": [
                        {
                            "brand": "아토젯",
                            "rank": 4,
                            "ms_recent_pct": 5.162,
                            "share_delta_pctp": 0.362,
                            "value_recent": 11_648_132_500.0,
                            "value_delta_krw": 648_132_500.0,
                            "series": [
                                {"period": "2025-01", "value_krw": 10_700_000_000.0, "value_억원": 107.0, "ms_pct": 4.8, "rank": 4},
                                {"period": "2026-04", "value_krw": 11_648_132_500.0, "value_억원": 116.481325, "ms_pct": 5.162, "rank": 4},
                            ],
                        }
                    ],
                },
            }
        ],
        sources=["cache"],
    )

    answer = "".join(GenosClient(token="dummy-token").stream_answer("아토젯 점유율이 오르는 동안 리바로는?", {"markdown_response": response.to_dict()}))

    assert "아토젯 시계열은 확인 안 됨" not in answer
    assert "아토젯 월별 MS" in answer
    assert "2025-01 4.80%" in answer
    assert "2026-04 5.16%" in answer


def test_cleanup_markdown_answer_repairs_common_korean_spacing_gaps() -> None:
    raw = (
        "1위로수젯은 점유율9.17%, 매출206.85억원입니다. "
        "리바로시장점유율은 3.76%이고 데이터미보유는 아닙니다.\n"
        "-Brand 상위: 5위 로수바미브 시장점유율4.29%, 매출96.81억 원증가\n"
        "분석 결과,아토젯과 리바로 월별시장점유율을 비교합니다. "
        "리바로는 6위 라는 안정적 위치와 6위 권을 유지합니다."
    )

    cleaned = cleanup_markdown_answer(raw)

    assert "1위 로수젯" in cleaned
    assert "점유율 9.17%" in cleaned
    assert "매출 206.85억원" in cleaned
    assert "리바로 시장점유율" in cleaned
    assert "데이터 미보유" in cleaned
    assert "- Brand 상위" in cleaned
    assert "시장점유율 4.29%" in cleaned
    assert "매출 96.81억 원 증가" in cleaned
    assert "결과, 아토젯" in cleaned
    assert "월별 시장점유율" in cleaned
    assert "6위라는 안정적 위치" in cleaned
    assert "6위권을 유지" in cleaned


def test_cleanup_markdown_answer_splits_joined_period_metric_tokens() -> None:
    raw = "아토젯 월별 MS:2025-11 5.02% → 2025-125.14% →2026-02 5.09% → 2026-035.22%"

    cleaned = cleanup_markdown_answer(raw)

    assert "MS: 2025-11 5.02%" in cleaned
    assert "2025-12 5.14%" in cleaned
    assert "→ 2026-02 5.09%" in cleaned
    assert "2026-03 5.22%" in cleaned
    assert "125.14%" not in cleaned
    assert "035.22%" not in cleaned


def test_cleanup_markdown_answer_removes_heading_before_source_only_block() -> None:
    raw = "### 분석의 한계\n\n출처: UBIST\n\n## 처리 시간\n\n- 총 소요: 1.00초"

    cleaned = cleanup_markdown_answer(raw)

    assert "### 분석의 한계" not in cleaned
    assert "출처: UBIST" in cleaned


def test_clinical_answer_keeps_notice_and_renders_external_table() -> None:
    result = ChatAgent().answer("리바로젯 임상")

    answer = result["answer"]

    assert "| ID | 제목 | 상태 |" in answer
    assert "NCT" in answer
    assert "복합제 조합 임상" in answer
    assert "\n\n## 주의\n- 리바로젯 임상은" in answer


def test_sales_activity_answer_keeps_observed_csd_and_market_evidence_separate() -> None:
    result = ChatAgent().answer("리바로 영업활동 Impact는?")

    answer = result["answer"]

    assert result["sources"] == ["cache"]
    assert [call["tool"] for call in result["tool_calls"]] == ["csd_activity_trend", "get_brand_metric"]
    assert not answer.startswith("## 답변")
    assert "**요약:**" not in answer
    assert "CSD 월별 aggregate 콜수/활동량" in answer
    assert "2026-03" in answer
    assert "2026-05" in answer
    assert "impact level·HCP/의사별·기관별 세부는 이 데이터에 포함되지 않습니다" in answer
    assert "현재 데이터로 답변 불가" not in answer
    assert "84.93" in answer
    assert "## 출처" in answer


def test_genos_markdown_interpretation_filters_new_numbers(monkeypatch) -> None:
    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield "표의 84.93억원 매출은 확인됩니다.\n"
        yield "하지만 999억원이라는 새 숫자는 생성하면 안 됩니다."

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)
    client = GenosClient(token="dummy-token")
    result = ChatAgent().answer("리바로 경쟁 상황이랑 임상 현황?")

    chunks = list(client.stream_answer("리바로 경쟁 상황이랑 임상 현황?", result))
    answer = "".join(chunks)

    assert len(chunks) > 1
    assert "84.93억원" in answer
    assert "999억원" not in answer
    assert "| 지표 | 값 |" not in answer
    assert "## 데이터" not in answer
    assert "숫자 검증" not in answer


def test_genos_markdown_interpretation_reports_fail_closed_when_numbers_are_removed(monkeypatch) -> None:
    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield "근거에 없는 999억원만 답합니다."

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)
    client = GenosClient(token="dummy-token")
    result = ChatAgent().answer("리바로 경쟁 상황이랑 임상 현황?")

    answer = "".join(client.stream_answer("리바로 경쟁 상황이랑 임상 현황?", result))

    assert "999억원" not in answer
    assert "표에 있는 확정 수치를 기준으로 정리했습니다." in answer
    assert "표에 포함된 확정 데이터만" in answer


def test_genos_markdown_generation_failure_returns_fact_fallback(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "agent_calculation",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "market_share_delta",
                    "period": "2026-03→2026-04",
                    "from_ms_pct": 3.46,
                    "to_ms_pct": 3.33,
                    "ms_delta_pct": -0.13,
                },
            }
        ],
        sources=["cache"],
    )
    attempts = 0

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        nonlocal attempts
        attempts += 1
        raise requests.Timeout("Flash generation timed out")
        yield ""

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    chunks = list(
        GenosClient(token="dummy-token").stream_answer(
            "리바로 3달전 대비 점유율 변화",
            {"markdown_response": response.to_dict()},
        )
    )
    answer = "".join(chunks)

    assert attempts == GENERATION_ATTEMPTS
    assert chunks
    assert "답변 생성이 지연" not in answer
    assert "최소 정리" not in answer
    assert "점유율 변화" in answer
    assert "2026-03→2026-04" in answer
    assert "3.46%" in answer
    assert "3.33%" in answer
    assert "-0.13%" in answer


def test_genos_markdown_interpretation_filters_wrong_units(monkeypatch) -> None:
    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield "표의 84.93억원 매출은 확인됩니다.\n"
        yield "시장점유율은 84.93%입니다."

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)
    client = GenosClient(token="dummy-token")
    result = ChatAgent().answer("리바로 매출/시장")

    answer = "".join(client.stream_answer("리바로 매출/시장", result))

    assert "84.93억원" in answer
    assert "84.93%" not in answer
    assert "| 매출 | 84.93억원 |" not in answer
    assert "숫자 검증" not in answer


def test_query_failed_fact_is_rendered_as_lookup_failure_not_missing_data() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "query_failed",
                "source": "cache",
                "render_data": {
                    "status": "query_failed",
                    "message": "요청한 지표 조회 실행이 실패했습니다. 데이터가 없다는 뜻은 아니며, 수치를 추정하지 않습니다.",
                    "tool_name": "query",
                    "error_type": "LookupError",
                },
            }
        ],
        sources=["cache"],
    )

    mandatory = "\n".join(mandatory_fact_lines(response.fact_md))
    messages = GenosClient._markdown_messages("리바로 채널별 매출", response.to_dict())
    system_prompt = messages[0]["content"]

    assert "조회 실패" in mandatory
    assert "데이터 미보유" not in mandatory
    assert "데이터가 없다는 뜻" in mandatory
    assert "조회 실패 행" in system_prompt
    assert "error 또는 query_failed" in system_prompt


def test_blocked_metric_values_hide_failed_zero_from_fact_and_data() -> None:
    response = MarkdownResponseBuilder().build(
        brand="악템라",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "악템라",
                    "metric": "sales",
                    "period": "2026-04",
                    "sales_krw": None,
                    "sales_억원": None,
                    "ms_recent_pct": None,
                    "rank": None,
                    "total_brands_in_market": None,
                    "source_status": "query_failed",
                    "blocked_metric_values": [
                        {
                            "period": "2026-04",
                            "status": "query_failed",
                            "message": "2026-04 값은 조회 실패/시장 매핑 불완전으로 표시하지 않습니다.",
                        }
                    ],
                },
            }
        ],
        sources=["cache"],
    )

    combined = f"{response.data_md}\n{response.fact_md}"

    assert "조회 차단" in combined
    assert "2026-04 값은 조회 실패/시장 매핑 불완전으로 표시하지 않습니다." in combined
    assert "0.00억원" not in combined
    assert "0.00%" not in combined
    assert "23/26" not in combined


def test_genos_markdown_appends_blocked_metric_notice_once(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="악템라",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "악템라",
                    "metric": "sales",
                    "period": "2025-Q4",
                    "requested_period": "2026-04",
                    "fallback_period": "2025-Q4",
                    "sales_억원": 48.19,
                    "ms_recent_pct": 4.34,
                    "rank": 8,
                    "total_brands_in_market": 26,
                    "source_status": "OK",
                    "blocked_metric_values": [
                        {
                            "period": "2026-04",
                            "status": "query_failed",
                            "message": "2026-04 값은 조회 실패/시장 매핑 불완전으로 표시하지 않습니다.",
                        }
                    ],
                },
            }
        ],
        sources=["cache"],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield "악템라는 사용 가능한 최신 기준 2025-Q4 매출 48.19억원, MS 4.34%입니다."

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(GenosClient(token="dummy-token").stream_answer("악템라 2026-04 매출 알려줘", {"markdown_response": response.to_dict()}))

    notice = "2026-04 값은 조회 실패/시장 매핑 불완전으로 표시하지 않습니다."
    assert answer.count(notice) == 1
    assert answer.rfind("## 출처") > answer.rfind(notice)
    assert "0.00억원" not in answer
    assert "0.00%" not in answer
    assert "23/26" not in answer


def test_genos_markdown_appends_blocked_metric_notice_from_data_md(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="악템라",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "악템라",
                    "metric": "sales",
                    "period": "2025-Q4",
                    "requested_period": "2026-04",
                    "fallback_period": "2025-Q4",
                    "sales_억원": 48.19,
                    "ms_recent_pct": 4.34,
                    "rank": 8,
                    "total_brands_in_market": 26,
                    "source_status": "OK",
                    "blocked_metric_values": [
                        {
                            "period": "2026-04",
                            "status": "query_failed",
                            "message": "2026-04 값은 조회 실패/시장 매핑 불완전으로 표시하지 않습니다.",
                        }
                    ],
                },
            }
        ],
        sources=["cache"],
    )
    payload = response.to_dict()
    payload["fact_md"] = "### 출처 유형 fact\n| 출처 |\n| --- |\n| UBIST |\n"

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield "악템라는 사용 가능한 최신 기준 2025-Q4 매출 48.19억원, MS 4.34%입니다."

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(GenosClient(token="dummy-token").stream_answer("악템라 2026-04 매출 알려줘", {"markdown_response": payload}))

    notice = "2026-04 값은 조회 실패/시장 매핑 불완전으로 표시하지 않습니다."
    assert answer.count(notice) == 1
    assert "0.00억원" not in answer
    assert "23/26" not in answer


def test_genos_markdown_strips_news_only_metric_claims(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "agent_calculation",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "sales_delta",
                    "period": "2026-03→2026-04",
                    "from_sales_억원": 87.11,
                    "to_sales_억원": 84.93,
                    "sales_delta_억원": -2.18,
                    "sales_delta_pct": -2.50,
                },
            },
            {
                "tool": "unsupported_metric",
                "source": "cache",
                "render_data": {
                    "brand": "아토젯",
                    "metric": "sales_delta",
                    "status": "unsupported",
                    "message": "아토젯 매출 변화는 현재 지원 브랜드 목록에서 지표 조회 대상을 확정하지 못했습니다.",
                },
            },
            {
                "tool": "deep_analysis_related_news",
                "source": "deep_analysis_events",
                "render_data": {
                    "items": [
                        {
                            "title": "상장제약사 1분기 제품 분석",
                            "source": "약업신문",
                            "match_excerpt": "리바로 패밀리 매출 511억원, 아토젯 261억원으로 전년 대비 -1.80% 감소",
                        }
                    ]
                },
            },
        ],
        sources=["cache", "deep_analysis_events"],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield (
            "리바로 매출은 2026년 3월 87.11억원에서 2026년 4월 84.93억원으로 2.18억원(-2.50%) 감소했습니다.\n\n"
            "### 뉴스 이슈\n"
            "2026년 1분기 리바로 패밀리 매출은 511억원이고, 아토젯은 261억원으로 전년 대비 -1.80% 하락했습니다.\n\n"
            "**아토젯 관련 이슈 및 데이터 한계**\n\n"
            "출처: UBIST, 약업신문"
        )

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            "리바로 뉴스에서 아토젯 이슈랑 매출 변화 같이 봐줘",
            {"markdown_response": response.to_dict()},
        )
    )

    assert "87.11억원" in answer
    assert "84.93억원" in answer
    assert "2.18억원" in answer
    assert "아토젯 매출 변화는 현재 지원 브랜드 목록" in answer
    assert "뉴스: 약업신문(날짜 미상) 「상장제약사 1분기 제품 분석」" in answer
    assert "관련 기사에서" not in answer
    assert "511억원" not in answer
    assert "261억원" not in answer
    assert "-1.80%" not in answer
    assert "### 뉴스 이슈" not in answer


def test_genos_markdown_preserves_llm_analysis_when_final_number_check_fails(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "series",
                    "period": "2026-04",
                    "level_segments": [
                        {"rank": 1, "name": "로수젯", "ms_recent_pct": 9.17, "value": 20_685_000_000.0},
                        {"rank": 2, "name": "리피토", "ms_recent_pct": 6.39, "value": 14_422_000_000.0},
                        {"rank": 3, "name": "리바로젯", "ms_recent_pct": 5.32, "value": 12_009_000_000.0},
                    ],
                    "level_top5_trend_series": [
                        {
                            "brand": "리바로젯",
                            "rank": 3,
                            "ms_recent_pct": 5.32,
                            "share_delta_pctp": 0.53,
                            "value_recent_억원": 120.09,
                            "value_delta_억원": 8.0,
                            "series": [{"period": "2026-04", "ms_pct": 5.32, "value_억원": 120.09, "rank": 3}],
                        },
                        {
                            "brand": "리피토",
                            "rank": 2,
                            "ms_recent_pct": 6.39,
                            "share_delta_pctp": -0.56,
                            "value_recent_억원": 144.22,
                            "value_delta_억원": -3.0,
                            "series": [{"period": "2026-04", "ms_pct": 6.39, "value_억원": 144.22, "rank": 2}],
                        },
                    ],
                },
            },
            {
                "tool": "agent_calculation",
                "source": "UBIST",
                "render_data": {
                    "metric": "competitive_insight_signals",
                    "signals": [
                        {
                            "brand": "리바로젯",
                            "share_of_growth_pct": 17.35,
                            "share_delta_pctp": 0.53,
                            "cohort_percentile": 83.0,
                        },
                        {
                            "brand": "리피토",
                            "share_of_growth_pct": -6.46,
                            "share_delta_pctp": -0.56,
                            "cohort_percentile": 12.0,
                        },
                    ],
                    "gain_loss": {
                        "gainer": "리바로젯",
                        "faller": "리피토",
                        "ratio_pct": 93.6,
                        "gainer_delta_pctp": 0.53,
                        "faller_delta_pctp": -0.56,
                        "period_from": "2025-07",
                        "period_to": "2026-04",
                    },
                },
            },
        ],
        sources=["cache"],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield (
            "리바로 시장은 로수젯 9.17%가 선두를 유지하는 가운데, 리바로젯 5.32%와 리피토 6.39%의 방향이 갈립니다. "
            "리바로젯은 share-of-growth 17.35%로 시장 성장 기여도가 높은 반면 리피토는 -6.46%로 시장 확대에도 점유를 내주고, "
            "리바로젯 상승폭 0.53%p가 리피토 하락폭 0.56%p의 93.6%에 해당합니다. 다만 집계 데이터만으로 직접 처방 이동은 확인할 수 없습니다.\n\n"
            "출처: UBIST"
        )

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)
    monkeypatch.setattr("jw_chat_agent_poc.service.genos_client.answer_has_only_fact_numbers", lambda _answer, _numbers: False)

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            "리바로 시장 경쟁 구도 변화는 어때",
            {"markdown_response": response.to_dict()},
        )
    )

    assert "직접 처방 이동은 확인할 수 없습니다" in answer
    assert "2025-07→2026-04" in answer
    assert "93.6%" not in answer
    assert "전월 대비" not in answer
    assert "확정 데이터상" not in answer
    assert "| 순위 | 브랜드 | 점유율 | 매출 |" not in answer


def test_genos_markdown_allows_news_date_but_not_news_only_figures(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {"brand": "리바로", "metric": "sales", "period": "2026-04", "sales_억원": 84.93},
            },
            {
                "tool": "deep_analysis_related_news",
                "source": "deep_analysis_events",
                "render_data": {
                    "items": [
                        {
                            "date": "2026-04-12",
                            "title": "리바로 시장 반응",
                            "source": "약업신문",
                            "summary": "리바로 시장 반응 기사",
                            "match_excerpt": "기사 본문에는 511억원 같은 기사 자체 수치도 있다.",
                        }
                    ]
                },
            },
        ],
        sources=["cache", "deep_analysis_events"],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield (
            "리바로 매출은 84.93억원입니다.\n"
            "2026-04-12 약업신문 기사 '리바로 시장 반응'도 함께 확인됩니다.\n"
            "기사에는 511억원도 언급됩니다."
        )

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(GenosClient(token="dummy-token").stream_answer("리바로 매출", {"markdown_response": response.to_dict()}))

    assert "84.93억원" in answer
    assert "2026-04-12" in answer
    assert "약업신문" in answer
    assert "511억원" not in answer


def test_genos_markdown_cleans_empty_headings_tables_and_spacing(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로하이",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로하이",
                    "metric": "sales",
                    "period": "2026-04",
                    "sales_억원": 31.0,
                },
            }
        ],
        sources=["cache"],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield (
            "###리바로하이 환자수 현황\n\n"
            "###리바로하이 매출\n"
            "리바로하이및 매출을 확인했습니다. 2026-04:31.00억원\n\n"
            "| 항목 | 값 |\n"
            "| --- |--- |\n"
            "|매출 | 31.00억원|\n"
            "| 2026-04|31.00억원 |\n\n"
            "출처:cache"
        )

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(GenosClient(token="dummy-token").stream_answer("리바로하이 최근 매출", {"markdown_response": response.to_dict()}))

    assert "###리바로하이" not in answer
    assert "환자수 현황" not in answer
    assert "### 리바로하이 매출" in answer
    assert "리바로하이 및 매출" in answer
    assert "2026-04: 31.00억원" in answer
    assert "| --- | --- |" in answer
    assert "| 매출 | 31.00억원 |" in answer
    assert "| 2026-04 | 31.00억원 |" in answer
    assert "## 출처" in answer
    assert "| UBIST | 2026-04 | — | — | — | 전체 | 억원 |" in answer


def test_genos_markdown_restores_share_delta_when_generated_answer_only_lists_points(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "agent_calculation",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "market_share_delta",
                    "period": "2026-03→2026-04",
                    "from_ms_pct": 3.81,
                    "to_ms_pct": 3.76,
                    "ms_delta_pct": -0.04,
                },
            }
        ],
        sources=["cache"],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield (
            "| 기간 | 시장점유율 |\n"
            "| --- | --- |\n"
            "| 2026-03 | 3.81% |\n"
            "| 2026-04 | 3.76% |\n"
            "출처: cache"
        )

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(GenosClient(token="dummy-token").stream_answer("리바로 3달전 대비 점유율 변화", {"markdown_response": response.to_dict()}))

    assert "점유율 변화" in answer
    assert "-0.04%" in answer


def test_genos_markdown_computes_share_delta_from_table_when_fact_has_no_delta(monkeypatch) -> None:
    fact_md = """## 확정 fact set

### 리바로 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2026-03 | 87.11억원 | 3.81% |
| 2026-04 | 84.93억원 | 3.76% |

### 출처 유형 fact
| 출처 |
| --- |
| cache |
"""
    markdown_response = {"fact_md": fact_md, "allowed_numbers": allowed_numbers(fact_md)}

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield (
            "리바로의 2026년 3월(3달 전) 대비 시장점유율은 소폭 하락했습니다.\n\n"
            "| 기간 | 매출액 | 시장점유율 | 시장규모 |\n"
            "| --- | --- | --- | --- |\n"
            "| 2026-03 | 87.11억원 | 3.81% | 2,288.39억원 |\n"
            "| 2026-04 | 84.93억원 | 3.76% | 2,256.77억원 |\n\n"
            "출처: cache"
        )

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(GenosClient(token="dummy-token").stream_answer("리바로 3달전 대비 점유율 변화", {"markdown_response": markdown_response}))

    assert "점유율 변화" in answer
    assert "-0.05%p" in answer


def test_genos_markdown_interpretation_filters_unlisted_kcd_codes(monkeypatch) -> None:
    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield "- 표의 E78 코드는 유지합니다.\n"
        yield "- 표에 없는 E98 코드도 확인됩니다."

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)
    client = GenosClient(token="dummy-token")
    result = ChatAgent().answer("이상지질혈증 환자 통계")

    answer = "".join(client.stream_answer("이상지질혈증 환자 통계", result))

    assert "E78" in answer
    assert "E98" not in answer
    assert "숫자 검증" not in answer


def test_genos_markdown_interpretation_filters_mixed_kcd_codes_on_same_line(monkeypatch) -> None:
    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield "표의 E78 코드는 유지합니다. E98은 새 코드입니다."

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)
    client = GenosClient(token="dummy-token")
    result = ChatAgent().answer("이상지질혈증 환자 통계")

    answer = "".join(client.stream_answer("이상지질혈증 환자 통계", result))
    body = answer.split("## 출처", 1)[0]

    assert "E78" not in body
    assert "E98" not in body
    assert "E78" in answer
    assert "표에 있는 확정 수치를 기준으로 정리했습니다." in answer


def test_genos_markdown_path_generates_full_answer_from_fact_set(monkeypatch) -> None:
    seen_prompt: dict[str, str] = {}

    def stream_chat(_self: GenosClient, messages: list[dict[str, str]]):
        seen_prompt["system"] = messages[0]["content"]
        seen_prompt["user"] = messages[1]["content"]
        yield "리바로의 최근 매출은 84.93억원입니다. 시장점유율은 3.33%, 순위는 7위입니다.\n\n출처: UBIST"

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)
    client = GenosClient(token="dummy-token")
    result = ChatAgent().answer("리바로 매출/시장")

    answer = "".join(client.stream_answer("리바로 매출/시장", result))

    assert "리바로의 최근 매출은 84.93억원입니다." in answer
    assert "## 데이터" not in answer
    assert "## 근거" not in answer
    assert "확정 fact set" in seen_prompt["user"]
    assert "매출 변화, 증감, 추이, 대비 질문" in seen_prompt["system"]
    assert "unsupported" in seen_prompt["system"]
    assert "LLM" not in answer
    assert "★" not in seen_prompt["system"]


def test_genos_fact_prompt_prioritizes_mandatory_rows() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "agent_calculation",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "sales_delta",
                    "period": "2026-03→2026-04",
                    "from_sales_억원": 87.11,
                    "to_sales_억원": 84.93,
                    "sales_delta_억원": -2.18,
                    "sales_delta_pct": -2.50,
                },
            },
            {
                "tool": "unsupported_metric",
                "source": "cache",
                "render_data": {
                    "brand": "아토젯",
                    "metric": "sales_delta",
                    "status": "unsupported",
                    "message": "아토젯 매출 변화는 현재 지원 브랜드 목록에서 지표 조회 대상을 확정하지 못했습니다.",
                },
            },
        ],
        sources=["cache"],
    )

    messages = GenosClient._markdown_messages("리바로 뉴스에서 아토젯 이슈랑 매출 변화 같이", response.to_dict())
    system_prompt = messages[0]["content"]
    user_prompt = messages[1]["content"]

    assert "데이터 미보유/미지원 행과 조회 실패 행은 별도 문장" in system_prompt
    assert user_prompt.index("필수 답변 fact") < user_prompt.index("확정 fact set")
    assert "리바로 2026-03→2026-04" in user_prompt
    assert "아토젯 매출 변화는 현재 지원 브랜드 목록" in user_prompt


def test_market_vs_brand_required_facts_suppress_generic_latest_sales_delta() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "agent_calculation",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "sales_delta",
                    "period": "2026-03→2026-04",
                    "from_sales_억원": 87.11,
                    "to_sales_억원": 84.93,
                    "sales_delta_억원": -2.18,
                    "sales_delta_pct": -2.50,
                },
            },
            {
                "tool": "agent_calculation",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "market_vs_brand_delta",
                    "period": "2026-01→2026-02",
                    "brand_from_sales_억원": 83.03,
                    "brand_to_sales_억원": 75.08,
                    "brand_delta_pct": -9.58,
                    "market_from_sales_억원": 2177.00,
                    "market_to_sales_억원": 1978.43,
                    "market_delta_pct": -9.12,
                    "delta_pct_gap": -0.46,
                },
            },
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "series",
                    "period": "2026-04",
                    "level_segments": [
                        {"name": "로수젯", "rank": 1, "ms_recent_pct": 9.17, "value": 206_850_000_000},
                    ],
                    "level_top5_trend_series": [
                        {
                            "brand": "로수젯",
                            "rank": 1,
                            "ms_recent_pct": 9.17,
                            "series": [{"period": "2026-04", "ms_pct": 9.17, "value_krw": 206_850_000_000, "rank": 1}],
                        }
                    ],
                },
            },
        ],
        sources=["cache"],
    )

    fact_md = response.fact_md

    assert "시장/브랜드 변화율 대조" in fact_md
    assert "2026-01→2026-02" in fact_md
    assert "매출 변화 | 리바로 2026-03→2026-04" not in fact_md
    assert "Brand별 점유율 fact" not in fact_md
    assert "상위 브랜드 점유율 추이 fact" not in fact_md
    assert "상위 브랜드 월별 MS fact" not in fact_md


def test_brand_trend_required_facts_suppress_duplicate_top_brand_rank_rows() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "series",
                    "period": "2026-04",
                    "level_segments": [
                        {"name": "로수젯", "rank": 1, "ms_recent_pct": 9.17, "value": 206_850_000_000},
                        {"name": "아토젯", "rank": 4, "ms_recent_pct": 5.16, "value": 116_480_000_000},
                    ],
                    "level_top5_trend_series": [
                        {
                            "brand": "아토젯",
                            "rank": 4,
                            "ms_recent_pct": 5.16,
                            "share_delta_pctp": -0.01,
                            "series": [
                                {"period": "2025-11", "ms_pct": 5.17, "value_krw": 110_000_000_000, "rank": 4},
                                {"period": "2026-04", "ms_pct": 5.16, "value_krw": 116_480_000_000, "rank": 4},
                            ],
                        }
                    ],
                },
            },
            {
                "tool": "agent_calculation",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "comparison_brand": "아토젯",
                    "metric": "brand_trend_comparison",
                    "period": "2025-11→2026-04",
                    "brand_from_ms_pct": 3.92,
                    "brand_to_ms_pct": 3.76,
                    "brand_share_delta_pctp": -0.16,
                    "comparison_from_ms_pct": 5.17,
                    "comparison_to_ms_pct": 5.16,
                    "comparison_share_delta_pctp": -0.01,
                    "brand_sales_delta_pct": 0.20,
                    "comparison_sales_delta_pct": 4.21,
                },
            },
        ],
        sources=["cache"],
    )

    fact_md = response.fact_md

    assert "브랜드 추세 비교" in fact_md
    assert "리바로 vs 아토젯" in fact_md
    assert "| Brand 상위 |" not in fact_md
    assert "| 상위 브랜드 추이 |" not in fact_md
    assert "Brand별 점유율 fact" not in fact_md
    assert "상위 브랜드 점유율 추이 fact" not in fact_md
    assert "상위 브랜드 월별 MS fact" in fact_md


def test_level_segments_are_mandatory_and_survive_empty_llm_table(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "market_share",
                    "period": "latest",
                    "level": "Brand",
                    "level_segments": [
                        {"name": "로수젯", "rank": 1, "ms_recent_pct": 9.1659, "value": 20_685_385_934.33},
                        {"name": "리피토", "rank": 2, "ms_recent_pct": 6.3904, "value": 14_421_756_866.72},
                    ],
                },
            }
        ],
        sources=["cache"],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield (
            "상위 브랜드는 로수젯과 리피토입니다.\n\n"
            "| 순위 | 브랜드명 | 시장점유율 |\n"
            "| --- | --- | --- |\n\n"
            "출처: UBIST"
        )

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            "리바로 시장에서 상위 브랜드 뭐 있어",
            {"markdown_response": response.to_dict()},
        )
    )

    assert "Brand 상위" in response.fact_md
    assert "로수젯 시장점유율 9.17%" in response.fact_md
    assert "매출 206.85억원" in response.fact_md
    assert "로수젯 시장점유율 9.17%" in answer
    assert "리피토 시장점유율 6.39%" in answer


def test_genos_markdown_removes_segment_numbers_not_in_query_facts(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "query_spec",
                    "period": "2026-04",
                    "level": "Molecule",
                    "applied_filters": {"channel": "의원"},
                    "level_segments": [
                        {"name": "PTV", "rank": 1, "ms_recent_pct": 34.70, "value": 10_000_000_000.0},
                        {"name": "RSV/EZE", "rank": 2, "ms_recent_pct": 33.19, "value": 9_000_000_000.0},
                    ],
                },
            }
        ],
        sources=["cache"],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield "의원 채널에서 PTV 점유율은 33.19%입니다.\n\n출처: UBIST"

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            "리바로 의원 채널에서 성분별 점유율",
            {"markdown_response": response.to_dict()},
        )
    )

    assert "PTV 점유율은 33.19%" not in answer
    assert "PTV 시장점유율 34.70%" in answer
    assert "RSV/EZE 시장점유율 33.19%" in answer


def test_level_segment_table_rows_satisfy_mandatory_without_duplicate(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "market_share",
                    "period": "latest",
                    "level": "Brand",
                    "level_segments": [
                        {"name": "로수젯", "rank": 1, "ms_recent_pct": 9.1659, "value": 20_685_385_934.33},
                        {"name": "리피토", "rank": 2, "ms_recent_pct": 6.3904, "value": 14_421_756_866.72},
                    ],
                },
            }
        ],
        sources=["cache"],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield (
            "### 리바로 시장 상위 브랜드 점유율 현황\n"
            "| 순위 | 브랜드명 | 시장점유율 |\n"
            "| --- | --- | --- |\n"
            "| 1위 | 로수젯 | 9.17% |\n"
            "| 2위 | 리피토 | 6.39% |\n\n"
            "출처: UBIST"
        )

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            "리바로 시장에서 상위 브랜드 뭐 있어",
            {"markdown_response": response.to_dict()},
        )
    )

    assert "Brand 상위" not in answer
    assert answer.count("로수젯") == 1
    assert answer.count("리피토") == 1


def test_market_member_snapshot_mandatory_fact_requires_sales_number(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "아토젯",
                    "metric": "market_member_snapshot",
                    "period": "latest",
                    "rank": 4,
                    "ms_recent_pct": 5.162,
                    "sales_krw": 11_648_132_500.0,
                    "sales_억원": 116.48,
                },
            }
        ],
        sources=["cache"],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield "아토젯은 시장점유율 5.16%입니다.\n\n출처: UBIST"

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            "리바로 뉴스에서 아토젯 이슈랑 매출 변화 같이 봐줘",
            {"markdown_response": response.to_dict()},
        )
    )

    assert "비교 브랜드 지표: 아토젯 최신 시장 멤버 지표" in answer
    assert "5.16%" in answer
    assert "116.48억원" in answer


def test_genos_markdown_uses_yoy_query_fact_when_llm_outputs_wrong_growth(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "yoy_growth",
                    "period": "2025-04→2026-04",
                    "from_period": "2025-04",
                    "to_period": "2026-04",
                    "from_sales_억원": 79.19,
                    "to_sales_억원": 84.93,
                    "sales_delta_억원": 5.74,
                    "growth_pct": 7.25,
                },
            }
        ],
        sources=["cache"],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield "리바로는 작년 동기 대비 4.00% 성장했습니다.\n\n출처: UBIST"

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            "리바로 작년 동기 대비 성장률",
            {"markdown_response": response.to_dict()},
        )
    )

    assert "4.00%" not in answer
    assert "YoY 성장률: 리바로 YoY 2025-04→2026-04" in answer
    assert "79.19억원" in answer
    assert "84.93억원" in answer
    assert "7.25%" in answer


def test_cleanup_removes_duplicate_top_brand_rank_prose() -> None:
    raw = (
        "리바로 시장의 브랜드별 점유율 현황을 살펴보면, 1위 로수젯 시장점유율 9.17%, "
        "2위 리피토 시장점유율 6.39%, 3위 리바로젯 시장점유율 5.32%를 기록하고 있습니다.\n\n"
        "최근 지표 기준 상위 브랜드는 다음과 같습니다.\n\n"
        "* **1위 로수젯:** 9.17%\n"
        "* **2위 리피토:** 6.39%\n"
        "* **3위 리바로젯:** 5.32%\n\n"
        "출처: UBIST"
    )

    answer = cleanup_markdown_answer(raw)

    assert "브랜드별 점유율 현황을 살펴보면" not in answer
    assert answer.count("1위 로수젯") == 1
    assert answer.count("2위 리피토") == 1
    assert answer.count("3위 리바로젯") == 1
    assert "출처: UBIST" in answer


def test_cleanup_removes_duplicate_top_brand_prose_when_later_table_has_rank_cells() -> None:
    raw = (
        "현재 시장 내 브랜드 상위 순위는 1위 로수젯 시장점유율 9.17%, "
        "2위 리피토 시장점유율 6.39%, 3위 리바로젯 시장점유율 5.32%를 기록하고 있습니다.\n\n"
        "### 리바로 시장 상위 브랜드점유율 현황\n"
        "| 순위 | 브랜드명 | 시장점유율 |\n"
        "| --- | --- | --- |\n"
        "| 1위 | 로수젯 | 9.17% |\n"
        "| 2위 | 리피토 | 6.39% |\n"
        "| 3위 | 리바로젯 | 5.32% |\n\n"
        "출처: UBIST"
    )

    answer = cleanup_markdown_answer(raw)

    assert "현재 시장 내 브랜드 상위 순위" not in answer
    assert "| 1위 | 로수젯 | 9.17% |" in answer
    assert "| 2위 | 리피토 | 6.39% |" in answer


def test_cleanup_removes_duplicate_top_brand_share_prose_before_table() -> None:
    raw = (
        "로수젯이 9.17%의 점유율로 1위를 기록하고 있으며, "
        "리피토(6.39%)와 리바로젯(5.32%)이 각각 2위와 3위를 차지하고 있습니다. "
        "이어 아토젯이 5.16%로 4위입니다.\n\n"
        "### 리바로 시장 상위 브랜드 점유율\n"
        "| 순위 | 브랜드명 | 시장점유율 |\n"
        "| --- | --- | --- |\n"
        "| 1위 | 로수젯 | 9.17% |\n"
        "| 2위 | 리피토 | 6.39% |\n"
        "| 3위 | 리바로젯 | 5.32% |\n"
        "| 4위 | 아토젯 | 5.16% |\n\n"
        "출처: UBIST"
    )

    answer = cleanup_markdown_answer(raw)

    assert "로수젯이 9.17%" not in answer
    assert "| 1위 | 로수젯 | 9.17% |" in answer


def test_cleanup_preserves_market_movement_analysis_when_tables_repeat_its_values() -> None:
    raw = (
        "수치로 보면, 리바로 점유율은 20.00%에서 19.35%로 0.65%p 감소했으나, "
        "처방조제액은 0.80억원에서 0.84억원으로 0.04억원 증가했습니다. "
        "브랜드 성장률 5.00% · 시장 성장률 8.50% · 초과성장 -3.50%p입니다.\n\n"
        "**리바로 매출 시계열**\n"
        "| 기간 | 매출 | MS |\n"
        "| --- | --- | --- |\n"
        "| 2026-01 | 0.80억원 | 20.00% |\n"
        "| 2026-03 | 0.84억원 | 19.35% |\n\n"
        "### 상위 브랜드 추이\n"
        "| 브랜드 | 시작 점유율 | 최신 MS | MS 변화 |\n"
        "| --- | --- | --- | --- |\n"
        "| 리바로 | 20.00% | 19.35% | -0.65%p |"
    )

    answer = cleanup_markdown_answer(raw)

    assert "수치로 보면" in answer
    assert "초과성장 -3.50%p" in answer
    assert "| 2026-03 | 0.84억원 | 19.35% |" in answer


def test_cleanup_removes_adjacent_duplicate_metric_sentence_without_touching_numbers() -> None:
    raw = (
        "리바로는 2026-04 기준 매출 84.93억원, 시장점유율 3.76%, 순위 6/516입니다. "
        "리바로는 2026-04 기준 매출 84.93억원, 시장점유율 3.76%, 순위 6/516입니다.\n\n"
        "리바로는 2026-04 기준 매출 84.93억원, 시장점유율 3.76%, 순위 6/516입니다. "
        "브랜드 매출·점유율·순위는 시장 내 침투 수준과 경쟁 방어 과제를 보여줍니다.\n\n"
        "## 출처\n"
        "- 데이터: UBIST"
    )

    answer = cleanup_markdown_answer(raw)

    assert answer.count("리바로는 2026-04 기준 매출 84.93억원") == 1
    assert "84.93억원" in answer
    assert "6/516" in answer
    assert "## 출처" in answer


def test_source_facts_use_user_friendly_names() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {"brand": "리바로", "metric": "sales", "period": "2026-04", "sales_억원": 84.93},
            },
            {
                "tool": "deep_analysis_related_news",
                "source": "deep_analysis_events",
                "render_data": {
                    "items": [
                        {
                            "date": "2026-04-12",
                            "title": "리바로 처방 동향",
                            "source": "약업신문",
                            "summary": "리바로 시장 반응 기사",
                        }
                    ]
                },
            },
        ],
        sources=["cache", "deep_analysis_events"],
    )

    assert "UBIST" in response.markdown
    assert "뉴스/이슈" in response.markdown
    assert "내부 심층분석" not in response.markdown
    assert "cache" not in response.sources_md
    assert "deep_analysis_events" not in response.sources_md
    assert "cache" not in response.fact_md
    assert "deep_analysis_events" not in response.fact_md


def test_hira_required_patient_facts_dedupe_same_label_and_disease() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로하이",
        calls=[
            {
                "tool": "get_disease_stats",
                "source": "hira_disease",
                "render_data": {
                    "calls": [
                        {
                            "render_data": {
                                "request": {"year": "2024"},
                                "items": [
                                    {
                                        "inpatOpat": "입원",
                                        "sickCd": "I10",
                                        "sickNm": "본태성(원발성) 고혈압",
                                        "ptntCnt": 16171,
                                    }
                                ]
                            }
                        },
                        {
                            "render_data": {
                                "request": {"year": "2024"},
                                "items": [
                                    {
                                        "inpatOpat": "입원",
                                        "sickCd": "I10",
                                        "sickNm": "본태성(원발성) 고혈압",
                                        "ptntCnt": 18136,
                                    }
                                ]
                            }
                        },
                        {
                            "render_data": {
                                "request": {"year": "2024"},
                                "items": [
                                    {
                                        "inpatOpat": "외래",
                                        "sickCd": "I10",
                                        "sickNm": "본태성(원발성) 고혈압",
                                        "ptntCnt": 3769201,
                                    }
                                ]
                            }
                        },
                    ]
                },
            }
        ],
        sources=["hira_disease"],
    )

    mandatory = "\n".join(mandatory_fact_lines(response.fact_md))

    assert mandatory.count("본태성(원발성) 고혈압(I10) 2024년 입원") == 1
    assert "18136명" not in mandatory
    assert "외래: 3769201명" in mandatory


def test_genos_markdown_appends_mandatory_fact_without_retry(monkeypatch) -> None:
    calls = [
        {
            "tool": "unsupported_metric",
            "source": "cache",
            "render_data": {
                "brand": "아토젯",
                "metric": "sales_delta",
                "status": "unsupported",
                "message": "아토젯 매출 변화는 현재 지원 브랜드 목록에서 지표 조회 대상을 확정하지 못했습니다.",
            },
        }
    ]
    response = MarkdownResponseBuilder().build(brand="리바로", calls=calls, sources=["cache"])
    prompts: list[str] = []

    def stream_chat(_self: GenosClient, messages: list[dict[str, str]]):
        prompts.append(messages[1]["content"])
        if len(prompts) == 1:
            yield "아토젯 관련 내용을 확인했습니다.\n\n출처: cache"
        else:
            yield "아토젯 매출 변화는 현재 지원 브랜드 목록에서 지표 조회 대상을 확정하지 못했습니다.\n\n출처: cache"

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)
    answer = "".join(GenosClient(token="dummy-token").stream_answer("리바로 뉴스에서 아토젯 이슈", {"markdown_response": response.to_dict()}))

    assert len(prompts) == 1
    assert "확정하지 못했습니다" in answer


def test_genos_markdown_answer_drops_internal_notice_md(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {"brand": "리바로", "metric": "sales", "period": "2026-04", "sales_억원": 84.93},
            }
        ],
        sources=["cache"],
        notices=["반복 도구 호출을 감지해 agent loop를 중단하고 확인된 도구 결과만 표시했습니다."],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield "리바로 2026-04 매출은 84.93억원입니다.\n\n출처: UBIST"

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(GenosClient(token="dummy-token").stream_answer("리바로 매출", {"markdown_response": response.to_dict()}))

    assert "84.93억원" in answer
    assert "## 주의" not in answer
    assert "반복 도구" not in answer


def test_genos_fact_prompt_hides_news_internal_metadata() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "deep_analysis_related_news",
                "source": "deep_analysis_events",
                "summary_text": "리바로 관련 뉴스 1건을 확인했습니다 (text_contains=아토젯 필터 적용).",
                "applied_filters": {"text_contains": "아토젯"},
                "render_data": {
                    "items": [
                        {
                            "date": "2026-04-12",
                            "title": "리바로 처방 동향",
                            "source": "약업신문",
                            "impact_score": 82,
                            "on_list": True,
                            "summary": "리바로 시장 반응 기사",
                            "match_excerpt": "아토젯 261억원, 리바로는 500억원대 매출",
                        }
                    ],
                    "applied_filters": {"text_contains": "아토젯"},
                    "data_basis": {"date_grain": "event_date", "latest_event_date": "2026-04-12"},
                    "selection": "on_list=true 우선",
                },
            }
        ],
        sources=["deep_analysis_events"],
    )

    prompt = GenosClient._markdown_messages("리바로 뉴스에서 아토젯 이슈", response.to_dict())[1]["content"]

    assert "아토젯 261억원" in prompt
    assert "text_contains" not in prompt
    assert "date_grain" not in prompt
    assert "on_list" not in prompt
    assert "impact_score" not in prompt
    assert "내부 심층분석" not in prompt
    assert "deep_analysis_events" not in prompt
    assert "cache" not in prompt


def test_external_aggregate_nested_calls_are_rendered_as_answer_facts() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "search_clinical",
                "source": "external_api",
                "render_data": {
                    "calls": [
                        {
                            "tool": "clinicaltrials_v2_search",
                            "render_data": {
                                "nct_ids": ["NCT01764178"],
                                "briefTitle": "Livalo safety study",
                            },
                        },
                        {
                            "tool": "mfds_clinical_trial_kr",
                            "render_data": {
                                "items": [
                                    {
                                        "GOODS_NAME": "CJ-20001",
                                        "CLINC_EXAM_TITLE": "리바로 안전성 연구",
                                        "CLINC_EXAM_STTUS": "완료",
                                        "CLINIC_STEP_NAME": "2상",
                                        "CLNC_TEST_SN": "201002160",
                                    }
                                ]
                            },
                        },
                    ]
                },
            },
            {
                "tool": "search_patent",
                "source": "external_api",
                "render_data": {
                    "calls": [
                        {
                            "tool": "mfds_patent",
                            "render_data": {
                                "items": [
                                    {
                                        "ITEM_NAME": "리바로정2밀리그램",
                                        "INGR_NAME": "피타바스타틴칼슘",
                                        "DOMESTIC_PATENT_NO": "10-0777553",
                                        "DOMESTIC_PATENT_STATUS": "소멸",
                                        "DOMESTIC_END_DATE": "2010-11-12",
                                        "PATENTEE": "닛산 가가쿠",
                                    }
                                ]
                            },
                        }
                    ]
                },
            },
        ],
        sources=["external_api"],
    )

    assert "조회 결과 없음" not in response.fact_md
    assert "### 임상시험 fact" in response.fact_md
    assert "NCT01764178" in response.fact_md
    assert "201002160" in response.fact_md
    assert "리바로 안전성 연구" in response.fact_md
    assert "### 특허 fact" in response.fact_md
    assert "10-0777553" in response.fact_md
    assert "소멸" in response.fact_md
    assert "2010-11-12" in response.fact_md
    assert "닛산 가가쿠" in response.fact_md


def test_external_patent_fact_table_uses_actual_api_fields() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "search_patent",
                "source": "external_api",
                "render_data": {
                    "calls": [
                        {
                            "tool": "mfds_patent",
                            "render_data": {
                                "items": [
                                    {
                                        "ITEM_NAME": "리바로정2밀리그램",
                                        "INGR_NAME": "피타바스타틴칼슘",
                                        "DOMESTIC_PATENT_NO": "10-0777553",
                                        "DOMESTIC_PATENT_STATUS": "소멸",
                                        "DOMESTIC_END_DATE": "2010-11-12",
                                        "PATENTEE": "다이셀 | 닛산",
                                    }
                                ]
                            },
                        },
                        {
                            "tool": "mfds_fda_orangebook",
                            "render_data": {
                                "items": [
                                    {
                                        "PRT_NAME": "LIVALO",
                                        "INGR_NAME": "Pitavastatin Calcium",
                                        "KOR_PAT_NO": "8557993",
                                        "KOR_STATUS": "소멸",
                                        "KOR_EXP_DATE": "2024-02-02 00:00:00",
                                        "KOR_APPLICANT": "NISSAN CHEMICAL CORPORATION",
                                    }
                                ]
                            },
                        },
                    ]
                },
            }
        ],
        sources=["external_api"],
    )

    assert "| 출처 | 제품/성분 | 특허번호 | 상태 | 만료일 | 권리자/출원인 |" in response.fact_md
    assert "mfds_patent" in response.fact_md
    assert "리바로정2밀리그램 / 피타바스타틴칼슘" in response.fact_md
    assert "10-0777553" in response.fact_md
    assert "2010-11-12" in response.fact_md
    assert "다이셀" in response.fact_md
    assert "mfds_fda_orangebook" in response.fact_md
    assert "8557993" in response.fact_md
    assert "2024-02-02" in response.fact_md


def test_competitor_patent_fact_surfaces_market_candidates_and_coverage() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "search_patent",
                "source": "external_api",
                "render_data": {
                    "competitor_ingredient_candidates": [
                        {
                            "rank": 1,
                            "molecule": "로수바스타틴",
                            "brand": "로수젯",
                            "source": "UBIST",
                            "market": "ml_006",
                            "period": "2026-04",
                            "sales": "220.00억원",
                            "market_share": "28.35%",
                        },
                        {
                            "rank": 2,
                            "molecule": "아토르바스타틴",
                            "brand": "리피토",
                            "source": "UBIST",
                            "market": "ml_006",
                            "period": "2026-04",
                            "sales": "145.00억원",
                            "market_share": "18.68%",
                        },
                    ],
                    "competitor_patent_coverage": {
                        "status": "attempted",
                        "message": "경쟁 성분 후보별 MFDS/OrangeBook 조회를 시도했습니다.",
                        "sources": "MFDS 의약품특허목록, FDA OrangeBook",
                    },
                    "calls": [
                        {
                            "tool": "mfds_patent",
                            "render_data": {
                                "items": [
                                    {
                                        "ITEM_NAME": "크레스토정",
                                        "INGR_NAME": "로수바스타틴칼슘",
                                        "DOMESTIC_PATENT_NO": "10-1234567",
                                        "DOMESTIC_PATENT_STATUS": "소멸",
                                        "DOMESTIC_END_DATE": "2021-01-02",
                                        "PATENTEE": "원천제약",
                                    }
                                ]
                            },
                        }
                    ],
                },
            }
        ],
        sources=["external_api"],
    )

    assert "### 경쟁 성분 후보군 fact" in response.fact_md
    assert "| 1 | 로수바스타틴 | 로수젯 | UBIST | ml_006 | 2026-04 | 220.00억원 | 28.35% |" in response.fact_md
    assert "| 2 | 아토르바스타틴 | 리피토 | UBIST | ml_006 | 2026-04 | 145.00억원 | 18.68% |" in response.fact_md
    assert "### 경쟁 성분 특허 조회 커버리지 fact" in response.fact_md
    assert "MFDS 의약품특허목록" in response.fact_md
    assert "현재 특허 DB에서 확인되는 항목만 표시" in response.fact_md
    assert "10-1234567" in response.fact_md


def test_competitor_patent_coverage_block_is_appended_when_final_omits_scope_heading() -> None:
    # Given: verified competitor patent facts exist but final prose used a loose heading.
    fact_md = "\n".join(
        (
            "### 경쟁 성분 후보군 fact",
            "| 순위 | 성분 | 대표 브랜드 | 출처 | 시장 | 기간 | 매출 | MS |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
            "| 1 | RSV/EZE | 로수젯 | UBIST | ml_006 | 2026-04 | 206.85억원 | 9.17% |",
            "| 2 | ATV | 리피토 | UBIST | ml_006 | 2026-04 | 144.22억원 | 6.39% |",
            "",
            "### 경쟁 성분 특허 조회 커버리지 fact",
            "| 항목 | 내용 |",
            "| --- | --- |",
            "| 출처 | MFDS 의약품특허목록, FDA OrangeBook |",
            "| 범위 | 현재 특허 DB에서 확인되는 항목만 표시하며, 전체 독점권을 단정하지 않습니다. |",
        )
    )
    answer = "경쟁 성분 특허 정보는 일부 미보유입니다."

    # When: the final answer is post-processed deterministically.
    repaired = append_competitor_patent_coverage_block(answer, fact_md)

    # Then: the user-facing answer preserves candidate, source, and coverage labels.
    assert "### 경쟁 성분 후보군·특허 커버리지" in repaired
    assert "| 1 | RSV/EZE | 로수젯 | UBIST | — | 2026-04 | 206.85억원 | 9.17% |" in repaired
    assert "ml_006" not in repaired
    assert "#### 출처·커버리지" in repaired
    assert "MFDS 의약품특허목록, FDA OrangeBook" in repaired
    assert "현재 특허 DB에서 확인되는 항목만 표시" in repaired


def test_markdown_cells_escape_raw_html() -> None:
    response = MarkdownResponseBuilder().build(
        brand="<script>alert(1)</script>",
        calls=[
            {
                "tool": "document_rag",
                "source": "document",
                "summary_text": "<script>alert(2)</script>",
                "render_data": {"chunks": [{"document": "x.md", "quote": "<script>alert(3)</script>"}]},
            }
        ],
        sources=["document"],
    )

    assert "<script" not in response.markdown.lower()
    assert "&lt;script" in response.markdown


def test_mfds_drug_info_splits_permission_and_ingredient_tables() -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "search_drug_info",
                "source": "external_api",
                "render_data": {
                    "calls": [
                        {
                            "tool": "mfds_permission_detail",
                            "status": "live",
                            "render_data": {
                                "items": [
                                    {
                                        "ITEM_NAME": "리바로정1밀리그램(피타바스타틴칼슘수화물)",
                                        "ENTP_NAME": "제이더블유중외제약(주)",
                                        "ITEM_PERMIT_DATE": "20050106",
                                        "ETC_OTC_CODE": "전문의약품",
                                        "MATERIAL_NAME": " ".join(
                                            (
                                                "총량 : 1정(85.102mg) 중\\",
                                                "| 성분명 : 피타바스타틴칼슘수화물\\",
                                                "| 분량 : 1.0\\",
                                                "| 단위 : 밀리그램\\",
                                                "| 규격 : JP\\",
                                                "| 성분정보 : \\",
                                                "| 비고 : 5수화물",
                                            )
                                        ),
                                        "STORAGE_METHOD": "차광기밀용기, 실온(1-30℃)보관",
                                        "VALID_TERM": "제조일로부터 36 개월",
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
        ],
        sources=["external_api"],
    )

    assert "| 품목명 | 업체 | 허가일 | 구분 | 저장법 | 유효기간 |" in response.markdown
    assert (
        "| 리바로정1밀리그램(피타바스타틴칼슘수화물) | 제이더블유중외제약(주) | "
        "2005-01-06 | 전문의약품 | 차광기밀용기, 실온(1-30℃)보관 | 제조일로부터 36 개월 |"
    ) in response.markdown
    assert "| 총량 | 성분명 | 분량 | 단위 | 규격 | 비고 |" in response.markdown
    assert "| 1정(85.102mg) 중 | 피타바스타틴칼슘수화물 | 1.0 | 밀리그램 | JP | 5수화물 |" in response.markdown
    mfds_rows = [
        line
        for line in response.markdown.splitlines()
        if line.startswith("| 품목명 ")
        or line.startswith("| 리바로정1밀리그램")
        or line.startswith("| 총량 ")
        or line.startswith("| 1정(")
    ]
    assert mfds_rows
    for line in mfds_rows:
        assert len([cell.strip() for cell in line.strip("|").split("|")]) == 6


def test_mfds_drug_info_missing_basic_fields_stay_in_aligned_table() -> None:
    markdown = drug_info_md(
        {
            "calls": [
                {
                    "tool": "mfds_permission_detail",
                    "status": "live",
                    "render_data": {
                        "items": [
                            {
                                "ITEM_NAME": "리바로정1밀리그램",
                                "ENTP_NAME": "제이더블유중외제약(주)",
                            }
                        ]
                    },
                }
            ]
        }
    )

    assert "| 품목명 | 업체 | 허가일 | 구분 | 저장법 | 유효기간 |" in markdown
    assert "| 리바로정1밀리그램 | 제이더블유중외제약(주) | - | - | - | - |" in markdown
    assert "\\|" not in markdown


def test_mfds_table_shape_mismatch_uses_key_value_fallback() -> None:
    markdown = _safe_table("### MFDS 성분 상세", ("총량", "성분명"), (("1정", "피타바스타틴", "extra"),))

    assert "| 항목 | 값 |" in markdown
    assert "| MFDS 성분 상세 행 1 | 1정 / 피타바스타틴 / extra |" in markdown


def test_fallback_top_brand_answer_keeps_insight_shape() -> None:
    fact_md = "\n".join(
        [
            "### 필수 답변 fact",
            "| 구분 | 값 |",
            "| --- | --- |",
            "| Brand 상위 | 1위 로수젯 시장점유율 9.17% 매출 206.85억원 |",
            "| Brand 상위 | 2위 리피토 시장점유율 6.39% 매출 144.22억원 |",
            "| Brand 상위 | 3위 리바로젯 시장점유율 5.32% 매출 120.09억원 |",
            "",
            "### 출처",
            "출처: UBIST, 내부 심층분석",
        ]
    )

    answer = fallback_fact_answer({"fact_md": fact_md})

    assert "확정 데이터 기준으로 정리하면" not in answer
    assert "로수젯이 선두" in answer
    assert "경쟁 구도" in answer
    assert "| 순위 | 브랜드 | 점유율 | 매출 |" in answer
    assert "| 1위 | 로수젯 | 9.17% | 206.85억원 |" in answer
    assert "경쟁 압력의 근거" in answer


def test_csd_fallback_renders_user_prose_instead_of_internal_fact_rows() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| CSD aggregate 콜수 | 리바로 CSD ChannelDynamics aggregate 콜수/활동량 2026-03 120건 → 2026-04 135건 |
| CSD 세부 미지원 | impact level, HCP/의사별, 기관별 |
"""

    answer = fallback_fact_answer({"fact_md": fact_md})

    assert "2026-03 120건" in answer
    assert "2026-04 135건" in answer
    assert "영업활동" in answer
    assert "반드시 반영할 내용" not in answer
    assert "CSD aggregate 콜수" not in answer
    assert "확정 데이터 기준으로 정리하면" not in answer


def test_generated_csd_internal_fact_dump_is_replaced_with_user_prose() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| CSD aggregate 콜수 | 리바로 CSD ChannelDynamics aggregate 콜수/활동량 2026-03 120건 → 2026-04 135건 |
| CSD 세부 미지원 | impact level, HCP/의사별, 기관별 |
"""
    generated = """요청한 값은 현재 조회 결과에 존재합니다.

## 확정 데이터

### 핵심 데이터
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| CSD aggregate 콜수 | 리바로 CSD ChannelDynamics aggregate 콜수/활동량 2026-03 120건 → 2026-04 135건 |
| CSD 세부 미지원 | impact level, HCP/의사별, 기관별 |
"""

    answer = replace_internal_fact_dump("리바로 영업활동 추이 어때?", generated, {"fact_md": fact_md})

    assert "2026-03 120건" in answer
    assert "2026-04 135건" in answer
    assert "영업활동" in answer
    assert "확정 데이터" not in answer
    assert "반드시 반영할 내용" not in answer
    assert "CSD 세부 미지원" not in answer


def test_cache_only_csd_answer_applies_internal_fact_dump_boundary() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| CSD aggregate 콜수 | 리바로 CSD ChannelDynamics aggregate 콜수/활동량 2026-03 120건 → 2026-04 135건 |
| CSD 세부 미지원 | impact level, HCP/의사별, 기관별 |
"""
    generated = """요청한 값은 현재 조회 결과에 존재합니다.

## 확정 데이터

| 구분 | 내용 |
| --- | --- |
| CSD aggregate 콜수 | 리바로 CSD ChannelDynamics aggregate 콜수/활동량 2026-03 120건 → 2026-04 135건 |
| CSD 세부 미지원 | impact level, HCP/의사별, 기관별 |
"""

    answer = "".join(
        GenosClient(token=None).stream_answer(
            "리바로 영업활동 추이 어때?",
            {"answer": generated, "markdown_response": {"fact_md": fact_md}},
        )
    )

    assert "2026-03 120건" in answer
    assert "2026-04 135건" in answer
    assert "확정 데이터" not in answer
    assert "CSD aggregate 콜수" not in answer
    assert "CSD 세부 미지원" not in answer


def test_external_relay_csd_answer_applies_internal_fact_dump_boundary() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| CSD aggregate 콜수 | 리바로 CSD ChannelDynamics aggregate 콜수/활동량 2026-03 120건 → 2026-04 135건 |
| CSD 세부 미지원 | impact level, HCP/의사별, 기관별 |
"""

    answer = "".join(
        GenosClient(token="test-token").stream_answer(
            "리바로 영업활동 추이 어때?",
            {
                "markdown_response": {"fact_md": fact_md},
                "tool_calls": [{"tool": "search_drug_info"}, {"tool": "csd_activity_trend"}],
            },
        )
    )

    assert "2026-03 120건" in answer
    assert "2026-04 135건" in answer
    assert "영업활동" in answer
    assert "확정 데이터" not in answer
    assert "반드시 반영할 내용" not in answer
    assert "CSD aggregate 콜수" not in answer
    assert "CSD 세부 미지원" not in answer


def test_causal_structure_does_not_append_generic_block_to_existing_analysis() -> None:
    fact_md = "\n".join(
        [
            "### 필수 답변 fact",
            "| 구분 | 반드시 반영할 내용 |",
            "| --- | --- |",
            "| 인사이트 계산 | 리바로젯 share-of-growth 17.35%, 점유율 변화 0.53%p, cohort 백분위 83.00% |",
            "| 인사이트 계산 | 리피토 share-of-growth -6.46%, 점유율 변화 -0.56%p, cohort 백분위 12.00% |",
            "| 인사이트 계산 | 리바로젯 2025-07→2026-04 상승폭 0.53%p 리피토 2025-07→2026-04 하락폭 -0.56%p |",
        ]
    )
    answer = (
        "리바로젯이 share-of-growth 17.35%로 시장 성장 기여도가 높은 동안 리피토는 -6.46%로 시장 확대에도 점유를 내줍니다. "
        "리바로젯 2025-07→2026-04 상승폭 0.53%p와 리피토 하락폭 -0.56%p는 반대 방향이지만, 집계 데이터만으로 직접 처방 이동은 확인할 수 없습니다."
    )

    revised = ensure_causal_structure("리바로 시장 경쟁 구도 변화는 어때", answer, fact_md)

    assert "직접 처방 이동은 확인할 수 없습니다" in revised
    assert "93.60%" not in revised
    assert "## 인과 분석" not in revised
    assert "변화의 질을 설명" not in revised


def test_raw_top_brand_retry_dump_is_rewritten_to_insight() -> None:
    fact_md = "\n".join(
        [
            "### 필수 답변 fact",
            "| 구분 | 값 |",
            "| --- | --- |",
            "| Brand 상위 | 1위 로수젯 시장점유율 9.17% 매출 206.85억원 |",
            "| Brand 상위 | 2위 리피토 시장점유율 6.39% 매출 144.22억원 |",
            "| Brand 상위 | 3위 리바로젯 시장점유율 5.32% 매출 120.09억원 |",
            "",
            "### 출처",
            "출처: UBIST, 내부 심층분석",
        ]
    )
    raw = "\n".join(
        [
            "출처: UBIST, 내부 심층분석",
            "",
            "- Brand 상위: 1위 로수젯 시장점유율 9.17% 매출 206.85억원",
            "- Brand 상위: 2위 리피토 시장점유율 6.39% 매출 144.22억원",
            "- Brand 상위: 3위 리바로젯 시장점유율 5.32% 매출 120.09억원",
        ]
    )

    answer = ensure_judgment_insight("리바로 경쟁 구도 변화는 어때", raw, fact_md)

    assert "Brand 상위:" not in answer
    assert "확정 데이터" not in answer
    assert "조회 결과에서 로수젯이 선두" in answer
    assert "로수젯이 선두" in answer
    assert "| 순위 | 브랜드 | 점유율 | 매출 |" in answer


def test_partial_raw_top_brand_lines_are_rewritten_to_verified_table() -> None:
    fact_md = "\n".join(
        [
            "### 필수 답변 fact",
            "| 구분 | 값 |",
            "| --- | --- |",
            "| Brand 상위 | 1위 로수젯 시장점유율 9.13% 매출 195.24억원 |",
            "| Brand 상위 | 2위 리피토 시장점유율 6.13% 매출 131.09억원 |",
            "| Brand 상위 | 3위 리바로젯 시장점유율 5.12% 매출 109.46억원 |",
        ]
    )
    raw = "\n".join(
        [
            "리바로의 최신 실적을 확인했습니다.",
            "- Brand 상위: 1위 로수젯 시장점유율 9.13% 매출 195.24억원",
            "- Brand 상위: 3위 리바로젯 시장점유율 5.12% 매출 109.46억원",
        ]
    )

    answer = ensure_judgment_insight("리바로와 로수젯을 비교해줘", raw, fact_md)

    assert "Brand 상위:" not in answer
    assert "조회 결과에서 로수젯이 선두" in answer
    assert "| 2위 | 리피토 | 6.13% | 131.09억원 |" in answer


def test_competitive_movement_analysis_preserves_perioded_gain_loss_conclusion_without_ratio() -> None:
    fact_md = "\n".join(
        [
            "### 필수 답변 fact",
            "| 구분 | 값 |",
            "| --- | --- |",
            "| 인사이트 계산 | 리피토 share-of-growth -6.46% 성장분해 시장 4.39% 점유 -0.56%p |",
            "| 인사이트 계산 | 리바로젯 share-of-growth 17.35% 성장분해 시장 4.39% 점유 0.53%p |",
            "| 인사이트 계산 | 리바로젯 2025-07→2026-04 상승폭 0.53%p 리피토 2025-07→2026-04 하락폭 -0.56%p 근거 기반 인과 분석: 두 브랜드 점유율 반대 방향 변화, 직접 처방 이동 미확인 |",
        ]
    )
    answer = "확정 데이터상 로수젯이 선두이고, 리피토와 리바로젯이 뒤따르는 경쟁 구도입니다."

    revised = ensure_competitive_movement_analysis("리바로 시장 경쟁 구도 변화는 어때", answer, fact_md)

    assert "리바로젯의 2025-07→2026-04 점유율 상승폭 0.53%p" in revised
    assert "리피토는 같은 기간 -0.56%p" in revised
    assert "93.62%" not in revised
    assert "전월 대비" not in revised
    assert "직접 처방 이동은 확인할 수 없습니다" in revised
    assert "재편 후보 신호" in revised


def test_competitive_movement_analysis_adds_missing_perioded_movement_when_generic_movement_exists() -> None:
    fact_md = "\n".join(
        [
            "### 필수 답변 fact",
            "| 구분 | 값 |",
            "| --- | --- |",
            "| 인사이트 계산 | 리피토 share-of-growth -6.46% 성장분해 시장 4.39% 점유 -0.56%p |",
            "| 인사이트 계산 | 리바로젯 share-of-growth 17.35% 성장분해 시장 4.39% 점유 0.53%p |",
            "| 인사이트 계산 | 리바로젯 2025-07→2026-04 상승폭 0.53%p 리피토 2025-07→2026-04 하락폭 -0.56%p 근거 기반 인과 분석: 두 브랜드 점유율 반대 방향 변화, 직접 처방 이동 미확인 |",
        ]
    )
    answer = (
        "최근 변화는 상승 폭이 큰 쪽은 리바로젯(0.53%p)입니다, "
        "하락 폭이 큰 쪽은 리피토(-0.56%p)입니다. "
        "이 신호는 경쟁 압력과 재편 후보로 해석할 수 있습니다."
    )

    revised = ensure_competitive_movement_analysis("리바로 시장 경쟁 구도 변화는 어때", answer, fact_md)

    assert "리바로젯의 2025-07→2026-04 점유율 상승폭 0.53%p" in revised
    assert "리피토는 같은 기간 -0.56%p" in revised
    assert "93.62%" not in revised
    assert "직접 처방 이동은 확인할 수 없습니다" in revised


def test_gain_loss_mandatory_requires_perioded_movement_language_without_ratio() -> None:
    fact_md = "\n".join(
        [
            "### 필수 답변 fact",
            "| 구분 | 값 |",
            "| --- | --- |",
            "| 인사이트 계산 | 리바로젯 2025-07→2026-04 상승폭 0.53%p 리피토 2025-07→2026-04 하락폭 -0.56%p 근거 기반 인과 분석: 두 브랜드 점유율 반대 방향 변화, 직접 처방 이동 미확인 |",
        ]
    )
    answer = "리피토가 share-of-growth -6.46%로 시장 성장 기여도를 보여줍니다."

    assert missing_mandatory_lines(answer, mandatory_fact_lines(fact_md))


def test_claim_guardrails_downgrade_competitive_causality_without_touching_sources() -> None:
    fact_md = "\n".join(
        [
            "### 필수 답변 fact",
            "| 구분 | 값 |",
            "| --- | --- |",
            "| 인사이트 계산 | 리바로젯 2025-07→2026-04 상승폭 0.53%p 리피토 2025-07→2026-04 하락폭 -0.56%p 근거 기반 인과 분석: 두 브랜드 점유율 반대 방향 변화, 직접 처방 이동 미확인 |",
        ]
    )
    answer = "\n\n".join(
        [
            "리피토에서 빠진 수요를 리바로젯이 흡수·잠식하며 전환시킨 구도입니다.",
            "## 출처",
            "- 뉴스: 약업신문 (2026-06-11) 「흡수 관련 기사 제목」 https://news.example/a",
        ]
    )

    guarded = apply_claim_guardrails("리바로 시장 경쟁 구도 변화는 어때", answer, fact_md)
    body = guarded.split("## 출처", maxsplit=1)[0]

    assert "흡수" not in body
    assert "잠식" not in body
    assert "전환" not in body
    assert "리바로젯 점유율은 2025-07→2026-04 0.53%p" in body
    assert "리피토는 같은 기간 -0.56%p" in body
    assert "93.62%" not in body
    assert "직접 처방 이동은 확인할 수 없습니다" in body
    assert "「흡수 관련 기사 제목」" in guarded


def test_claim_guardrails_remove_hira_unavailable_derivatives_once() -> None:
    fact_md = "\n".join(
        [
            "### 필수 답변 fact",
            "| 구분 | 값 |",
            "| --- | --- |",
            "| HIRA 조회 상태 | 리바로하이 I10 환자수 수치 미반환(resultCode=99) |",
            "| 브랜드 핵심 지표 | 리바로하이 2026-04 매출 0.67억원 시장점유율 0.03% 순위 411/1015 |",
        ]
    )
    answer = (
        "리바로하이는 환자 1인당 처방액을 계산해 침투율을 봐야 합니다. "
        "환자 기반 수요를 실제 처방 성과로 전환할 여지가 큽니다."
    )

    guarded = apply_claim_guardrails("리바로하이 질병 환자수랑 최근 매출 한번에", answer, fact_md)

    assert "환자 1인당" not in guarded
    assert "수요를 실제 처방 성과로 전환" not in guarded
    assert guarded.count("HIRA 환자수 수치가 미반환되어") == 1
    assert "산출할 수 없습니다" in guarded


def test_genos_final_answer_applies_channel_claim_policy(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "query_spec",
                    "period": "2026-04",
                    "level": "channel",
                    "level_segments": [
                        {"name": "의원", "rank": 1, "ms_recent_pct": 3.37, "value": 4_193_000_000.0},
                        {"name": "종합병원", "rank": 2, "ms_recent_pct": 4.22, "value": 2_057_000_000.0},
                        {"name": "상급종합병원", "rank": 3, "ms_recent_pct": 4.49, "value": 1_764_000_000.0},
                    ],
                },
            }
        ],
        sources=["cache"],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield (
            "리바로는 의원 매출이 Cash Cow임을 입증합니다. "
            "상급종합병원은 임상적 근거와 처방 전이가 확인되는 채널입니다.\n\n"
            "- channel 상위: 1위 의원 시장점유율 3.37% 매출 41.93억원\n"
            "- channel 상위: 2위 종합병원 시장점유율 4.22% 매출 20.57억원\n"
            "- channel 상위: 3위 상급종합병원 시장점유율 4.49% 매출 17.64억원"
        )

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            "리바로 채널별 매출",
            {"markdown_response": response.to_dict()},
        )
    )

    body = answer.split("## 출처", maxsplit=1)[0]
    for forbidden in ("Cash Cow", "입증", "임상적 근거", "전이"):
        assert forbidden not in body
    assert "| 의원 | 3.37% | 41.93억원 |" in body
    assert "| 종합병원 | 4.22% | 20.57억원 |" in body
    assert "| 상급종합병원 | 4.49% | 17.64억원 |" in body


def test_genos_final_answer_renders_portfolio_declines_as_table(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="JW 주요 브랜드",
        calls=[
            {
                "tool": "portfolio_decline_analysis",
                "source": "UBIST",
                "render_data": {
                    "brand": "JW 주요 브랜드",
                    "metric": "portfolio_market_share_decline",
                    "period": "2024-Q4→2025-Q4",
                    "source_label": "UBIST",
                    "decliners": [
                        {
                            "brand": "위너프",
                            "market_name": "수액",
                            "period_from": "2024-Q4",
                            "period_to": "2025-Q4",
                            "from_ms_pct": 6.84,
                            "to_ms_pct": 5.08,
                            "share_delta_pctp": -1.75,
                            "to_sales_krw": 3_376_000_000,
                            "top_gainers": [{"brand": "오마프플러스원페리", "share_delta_pctp": 3.95}],
                        },
                        {
                            "brand": "리바로",
                            "market_name": "이상지질혈증",
                            "period_from": "2024-Q4",
                            "period_to": "2025-Q4",
                            "from_ms_pct": 3.93,
                            "to_ms_pct": 3.76,
                            "share_delta_pctp": -0.17,
                            "to_sales_krw": 8_493_000_000,
                            "top_gainers": [{"brand": "리바로젯", "share_delta_pctp": 0.53}],
                        },
                    ],
                    "interpretation_guardrail": "시장점유율 이동 후보이며 처방 이동 또는 인과를 직접 단정하지 않습니다.",
                },
            }
        ],
        sources=["UBIST"],
    )

    def stream_chat(_self: GenosClient, _messages: list[dict[str, str]]):
        yield (
            "- 포트폴리오 MS 하락: 위너프 2024-Q4→2025-Q4 MS 6.84% → 5.08% 변화 -1.75%p "
            "최신 매출 33.76억원 동시장 상승 후보 오마프플러스원페리 3.95%p 직접 인과/처방 이동 단정 불가\n"
            "- 포트폴리오 MS 하락: 리바로 2024-Q4→2025-Q4 MS 3.93% → 3.76% 변화 -0.17%p "
            "최신 매출 84.93억원 동시장 상승 후보 리바로젯 0.53%p 직접 인과/처방 이동 단정 불가"
        )

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            "JW 주요 브랜드 중 최근 시장점유율이 하락한 게 있으면 어떤 브랜드인지, 그 시장에서 누가 점유율을 가져갔는지 원인을 분석해줘",
            {"markdown_response": response.to_dict()},
        )
    )

    body = answer.split("## 출처", maxsplit=1)[0]
    assert "포트폴리오 MS 하락:" not in body
    assert "### JW 주요 브랜드 MS 하락 요약" in body
    assert "| 브랜드 | 기간 | MS 변화 | 최신 매출 | 동시장 상승 후보 |" in body
    assert "| 위너프 | 2024-Q4→2025-Q4 | 6.84% → 5.08% (-1.75%p) | 33.76억원 | 오마프플러스원페리 3.95%p |" in body
    assert "| 리바로 | 2024-Q4→2025-Q4 | 3.93% → 3.76% (-0.17%p) | 84.93억원 | 리바로젯 0.53%p |" in body
    assert "위너프가 -1.75%p로 가장 크게 하락했습니다" in body
    assert "직접 인과나 처방 이동은 단정하지 않습니다" in body
