from __future__ import annotations

from jw_chat_agent_poc.orchestrator.agent import ChatAgent
from jw_chat_agent_poc.service.charts import build_charts
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.metrics.cache_live import StaticCausePayloadReader, StaticMetricsCacheReader

from test_metrics_cache import BRAND_CARDS, CACHE_BRANDS, CAUSE_READER, cause_payload_with_top_brand_trends


def test_cache_snapshot_without_chart_intent_does_not_emit_charts() -> None:
    result = {
        "resolution": {
            "canonical_brand": "리바로",
            "market_id": "strategy_006",
        },
        "sources": ["cache"],
        "tool_calls": [],
    }

    charts = build_charts(result, question="리바로 매출", answer="리바로 2026-04 매출은 84.93억원입니다.", cause_reader=CAUSE_READER)

    assert charts == []


def test_series_question_builds_fact_backed_brand_sales_chart() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=CAUSE_READER)

    result = ChatAgent(metrics=tool).answer("리바로 최근 매출 추이")
    charts = build_charts(result, question="리바로 최근 매출 추이", answer="리바로 매출 추이를 보면 최근 4개 시점이 이어집니다.", cause_reader=CAUSE_READER)

    sales_chart = next(chart for chart in charts if chart["title"] == "리바로 매출 추이")
    assert sales_chart["type"] == "line"
    assert sales_chart["labels"][-1] == "2026-04"
    assert sales_chart["datasets"][0]["label"] == "리바로 매출"
    assert sales_chart["datasets"][0]["data"][-1] == 8_493_234_217.11
    assert len(sales_chart["labels"]) >= 2


def test_single_point_share_and_rank_question_does_not_emit_charts() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=CAUSE_READER)

    result = ChatAgent(metrics=tool).answer("리바로 점유율이랑 순위 알려줘")
    charts = build_charts(result, question="리바로 점유율이랑 순위 알려줘", answer="리바로 점유율은 3.76%, 순위는 6위입니다.", cause_reader=CAUSE_READER)

    assert charts == []


def test_single_brand_focus_level_segments_do_not_emit_comparison_chart() -> None:
    result = {
        "resolution": {"canonical_brand": "리바로하이"},
        "sources": ["cache"],
        "tool_calls": [
            {
                "tool": "get_brand_metric",
                "render_data": {
                    "brand": "리바로하이",
                    "metric": "sales",
                    "period": "2026-04",
                    "answer_scope": "single_brand_focus",
                    "sales_krw": 3_100_000_000,
                    "brand_value_series_10pt": [
                        {"period": "2026-03", "value_krw": 2_900_000_000},
                        {"period": "2026-04", "value_krw": 3_100_000_000},
                    ],
                    "market_size_series": [
                        {"period": "2026-03", "value_krw": 210_000_000_000},
                        {"period": "2026-04", "value_krw": 220_000_000_000},
                    ],
                    "level_top5_trend_series": [
                        {
                            "brand": "트윈스타",
                            "series": [
                                {"period": "2026-03", "ms_pct": 4.1},
                                {"period": "2026-04", "ms_pct": 4.3},
                            ],
                        }
                    ],
                    "level": "Brand",
                    "level_segments": [
                        {"name": "트윈스타", "ms_recent_pct": 4.3, "value": 8_508_000_000},
                        {"name": "아모잘탄", "ms_recent_pct": 3.84, "value": 7_594_000_000},
                    ],
                },
            }
        ],
    }

    charts = build_charts(
        result,
        question="리바로하이 질병 환자수랑 최근 매출 한번에",
        answer="상위 브랜드 MS와 매출 추이도 참고됩니다.",
    )

    assert charts == []


def test_hira_single_point_payloads_do_not_emit_charts() -> None:
    result = ChatAgent().answer("이상지질혈증 환자 통계")

    assert build_charts(result, question="이상지질혈증 환자 통계", answer="환자수 통계를 확인했습니다.", cause_reader=CAUSE_READER) == []


def test_filtered_metric_charts_use_filtered_render_data() -> None:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=CAUSE_READER)

    result = ChatAgent(metrics=tool).answer("리바로 작년 매출 추이")
    charts = build_charts(result, question="리바로 작년 매출 추이", answer="리바로 작년 매출 추이입니다.", cause_reader=CAUSE_READER)

    sales_chart = next(chart for chart in charts if chart["title"] == "리바로 매출 추이")
    assert sales_chart["labels"] == ["2025-01", "2025-12"]
    assert sales_chart["datasets"][0]["data"] == [7_000_000_000.0, 9_000_000_000.0]


def test_clinical_questions_do_not_emit_charts() -> None:
    result = ChatAgent().answer("리바로젯 임상")

    assert build_charts(result, question="리바로젯 임상", answer=result["answer"], cause_reader=CAUSE_READER) == []


def test_top_brand_share_trend_question_builds_multi_series_chart() -> None:
    cause_reader = StaticCausePayloadReader(
        {("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): cause_payload_with_top_brand_trends()}
    )
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=cause_reader)

    result = ChatAgent(metrics=tool).answer("리바로 시장 상위 3개 점유율 변화 비교")
    charts = build_charts(result, question="리바로 시장 상위 3개 점유율 변화 비교", answer=result["answer"], cause_reader=cause_reader)

    assert "상위 브랜드 점유율 추이 fact" in result["markdown_response"]["fact_md"]
    assert "아토젯" in result["markdown_response"]["fact_md"]
    share_chart = next(chart for chart in charts if chart["title"] == "상위 브랜드 점유율 추이")
    assert share_chart["type"] == "line"
    assert share_chart["labels"] == ["2025-01", "2025-12", "2026-03", "2026-04"]
    assert [dataset["label"] for dataset in share_chart["datasets"][:3]] == ["로수젯 MS", "리피토 MS", "아토젯 MS"]
    assert share_chart["datasets"][2]["data"][-1] == 5.162


def test_top_brand_share_trend_series_is_available_to_answer_facts() -> None:
    cause_reader = StaticCausePayloadReader(
        {("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): cause_payload_with_top_brand_trends()}
    )
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=cause_reader)

    result = ChatAgent(metrics=tool).answer("아토젯 점유율이 오르는 동안 리바로는 어땠어")
    fact_md = result["markdown_response"]["fact_md"]

    assert "상위 브랜드 월별 MS fact" in fact_md
    assert "| 아토젯 | 2025-01 | 4.80% | 107.00억원 | 4 |" in fact_md
    assert "| 아토젯 | 2026-04 | 5.16% | 116.48억원 | 4 |" in fact_md
    assert "아토젯 월별 MS" in fact_md


def test_market_vs_brand_question_builds_single_comovement_chart() -> None:
    payload = cause_payload_with_top_brand_trends()
    payload["data"]["sources_data"]["market_size_series"] = {
        "2026-01": {"value": 230_000_000_000.0, "yoy_growth_pct": 21.0},
        "2026-02": {"value": 215_000_000_000.0, "yoy_growth_pct": 12.0},
        "2026-03": {"value": 228_838_670_570.0, "yoy_growth_pct": 25.59},
        "2026-04": {"value": 225_677_368_890.97986, "yoy_growth_pct": 36.88},
    }
    brand_block = payload["data"]["level_top5_trend"]["by_level"]["Brand"]
    brand_block["periods_10pt"] = ["2026-01", "2026-02", "2026-03", "2026-04"]
    livalo_row = brand_block["values"][0]["brands_in_value"][0]
    livalo_row["value_series_10pt"] = [9_000_000_000.0, 8_000_000_000.0, 8_711_248_139.54, 8_493_234_217.11]
    livalo_row["ms_series_10pt"] = [3.91, 3.72, 3.8067, 3.7634]
    cause_reader = StaticCausePayloadReader(
        {("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): payload}
    )
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=cause_reader)

    result = ChatAgent(metrics=tool).answer("리바로 2월 매출 하락이 시장 영향인지 브랜드 고유인지")
    charts = build_charts(result, question="리바로 2월 매출 하락이 시장 영향인지 브랜드 고유인지", answer=result["answer"], cause_reader=cause_reader)

    comovement_chart = next(chart for chart in charts if chart["title"] == "리바로와 시장 매출 추이")
    assert [dataset["label"] for dataset in comovement_chart["datasets"]] == ["리바로 매출", "시장 매출"]
    assert comovement_chart["labels"] == ["2026-01", "2026-02", "2026-03", "2026-04"]
    assert comovement_chart["datasets"][0]["data"][1] == 8_000_000_000.0
    assert comovement_chart["datasets"][1]["data"][1] == 215_000_000_000.0


def test_market_vs_brand_question_exposes_change_rate_fact() -> None:
    payload = cause_payload_with_top_brand_trends()
    payload["data"]["sources_data"]["market_size_series"] = {
        "2026-01": {"value": 230_000_000_000.0},
        "2026-02": {"value": 215_000_000_000.0},
        "2026-03": {"value": 228_838_670_570.0},
        "2026-04": {"value": 225_677_368_890.97986},
    }
    brand_block = payload["data"]["level_top5_trend"]["by_level"]["Brand"]
    brand_block["periods_10pt"] = ["2026-01", "2026-02", "2026-03", "2026-04"]
    livalo_row = brand_block["values"][0]["brands_in_value"][0]
    livalo_row["value_series_10pt"] = [9_000_000_000.0, 8_000_000_000.0, 8_711_248_139.54, 8_493_234_217.11]
    livalo_row["ms_series_10pt"] = [3.91, 3.72, 3.8067, 3.7634]
    cause_reader = StaticCausePayloadReader(
        {("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): payload}
    )
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=cause_reader)

    result = ChatAgent(metrics=tool).answer("리바로 2월 매출 하락이 시장 영향인지 브랜드 고유인지")

    calculations = [
        call for call in result["tool_calls"]
        if call.get("tool") == "agent_calculation" and call.get("render_data", {}).get("metric") == "market_vs_brand_delta"
    ]
    assert calculations
    data = calculations[0]["render_data"]
    assert data["period"] == "2026-01→2026-02"
    assert data["brand_delta_pct"] == -11.1111
    assert data["market_delta_pct"] == -6.5217
    assert "시장/브랜드 변화율 대조" in result["markdown_response"]["fact_md"]


def test_market_vs_brand_eval_wording_routes_to_agent_loop() -> None:
    payload = cause_payload_with_top_brand_trends()
    payload["data"]["sources_data"]["market_size_series"] = {
        "2026-01": {"value": 200_000_000_000.0},
        "2026-02": {"value": 190_000_000_000.0},
    }
    brand_block = payload["data"]["level_top5_trend"]["by_level"]["Brand"]
    brand_block["periods_10pt"] = ["2026-01", "2026-02"]
    livalo_row = brand_block["values"][0]["brands_in_value"][0]
    livalo_row["value_series_10pt"] = [9_000_000_000.0, 8_100_000_000.0]
    livalo_row["ms_series_10pt"] = [4.0, 3.8]
    cause_reader = StaticCausePayloadReader(
        {("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): payload}
    )
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    tool = MetricsTool(mode="cache", cache_reader=reader, cause_reader=cause_reader)

    result = ChatAgent(metrics=tool).answer("리바로 2월 매출이 떨어진 게 시장 전체 영향이야, 리바로만의 문제야?")
    metrics = [call.get("render_data", {}).get("metric") for call in result["tool_calls"]]

    assert "market_vs_brand_delta" in metrics
    assert "시장/브랜드 변화율 대조" in result["markdown_response"]["fact_md"]


def test_market_vs_brand_chart_uses_korean_particle_for_batchim_brand() -> None:
    result = {
        "resolution": {"canonical_brand": "리바로젯"},
        "sources": ["cache"],
        "tool_calls": [
            {
                "tool": "get_brand_metric",
                "render_data": {
                    "brand": "리바로젯",
                    "metric": "series",
                    "brand_value_series_10pt": [
                        {"period": "2026-03", "value_krw": 10.0},
                        {"period": "2026-04", "value_krw": 12.0},
                    ],
                    "market_size_series": [
                        {"period": "2026-03", "value_krw": 100.0},
                        {"period": "2026-04", "value_krw": 105.0},
                    ],
                },
            }
        ],
    }

    charts = build_charts(result, question="리바로젯 매출 하락이 시장 영향인지", answer="")

    assert any(chart["title"] == "리바로젯과 시장 매출 추이" for chart in charts)
