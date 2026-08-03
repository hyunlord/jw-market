from __future__ import annotations

from decimal import Decimal

from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.service.chart_utils import share_chart
from jw_chat_agent_poc.service.charts import build_charts as _build_charts, issue_render_authorization
from jw_chat_agent_poc.tools.metrics.cache_fixture import MetricsTool
from jw_chat_agent_poc.tools.metrics.cache_live import (
    CsdActivityTarget,
    StaticCsdActivityReader,
    StaticCsdActivityTargetReader,
    StaticMetricsCacheReader,
    _best_csd_activity_candidate,
)
from jw_chat_agent_poc.tools.query_layer.compute import brand_average_share_data, brand_yoy_data, top_trend
from jw_chat_agent_poc.tools.query_layer.render import level_segments, metric_summary
from jw_chat_agent_poc.tools.query_layer.store import MartRecord, MartSnapshot

from test_metrics_cache import BRAND_CARDS, CACHE_BRANDS


def build_charts(result, *, question="", answer="", cause_reader=None):
    authorization = issue_render_authorization(
        result,
        question=question,
        answer=answer,
        enforce_binding=False,
    )
    return _build_charts(
        result,
        authorization=authorization,
        question=question,
        answer=answer,
        cause_reader=cause_reader,
    )


def _snapshot(*, failed_middle: bool = True) -> MartSnapshot:
    periods = ("2025-03", "2026-02", "2026-03")
    target_values = (0.0, 20.0, 40.0)
    competitor_values = (100.0, 80.0, 60.0)

    def record(brand: str, values: tuple[float, ...], failed_index: int | None = None) -> MartRecord:
        history = {
            period: {
                "raw_value": value,
                "ms": value,
                "source_status": "query_failed" if index == failed_index else "OK",
            }
            for index, (period, value) in enumerate(zip(periods, values, strict=True))
        }
        return MartRecord(
            ml_id="ml_test",
            brand_name=brand,
            source="ubist",
            measure="sales",
            metric_history=history,
            channel_data={},
            specialty_data={},
            dimension_data={},
            by_dimension={"company": "JW" if brand == "대상" else "경쟁사"},
        )

    return MartSnapshot(
        records=(
            record("대상", target_values, 1 if failed_middle else None),
            record("경쟁", competitor_values),
        ),
        loaded_at=0.0,
    )


def test_missing_yoy_operand_is_not_coerced_to_negative_one_hundred_percent() -> None:
    snapshot = _snapshot()

    data = brand_yoy_data(snapshot, "ml_test", "ubist", "대상")

    assert data["from_sales_krw"] == 0.0
    assert data["to_sales_krw"] == 40.0
    assert data["growth_pct"] is None
    assert data["growth_unavailable_reason"] == "zero_denominator"
    assert "데이터가 없어" not in data.get("data_availability_note", "")


def test_missing_yoy_current_value_is_fail_closed_instead_of_zero() -> None:
    snapshot = _snapshot()
    target = snapshot.records[0]
    target.metric_history["2026-03"]["source_status"] = "query_failed"

    data = brand_yoy_data(snapshot, "ml_test", "ubist", "대상")

    assert data["to_sales_krw"] is None
    assert data["sales_delta_krw"] is None
    assert data["growth_pct"] is None
    assert data["missing_periods"] == ["2026-03"]
    assert data["data_availability_note"] == "2026-03 데이터가 없어 계산할 수 없습니다"


def test_trend_endpoint_missing_does_not_create_fake_delta() -> None:
    snapshot = _snapshot()
    snapshot.records[0].metric_history["2026-03"]["source_status"] = "query_failed"

    target = next(row for row in top_trend(snapshot, "ml_test", "ubist", "2026-03", "대상") if row["brand"] == "대상")

    assert target["to_ms_pct"] is None
    assert target["share_delta_pctp"] is None
    assert target["value_delta_krw"] is None
    assert target["missing_periods"] == ["2026-02", "2026-03"]
    assert "0.0" not in target["data_availability_note"]


def test_average_share_excludes_missing_period_from_numerator_and_denominator() -> None:
    data = brand_average_share_data(_snapshot(), "ml_test", "ubist", "대상", 3)

    assert data["avg_ms_pct"] == 20.0
    assert data["observation_count_used"] == 2
    assert data["missing_periods"] == ["2026-02"]
    assert data["data_availability_note"] == "2026-02 데이터가 없어 해당 기간을 제외하고 계산했습니다"
    fact = answer_fact_markdown(
        [{"tool": "get_brand_metric", "source": "UBIST", "render_data": data}],
        ["UBIST"],
    )
    assert "2026-02 데이터가 없어 해당 기간을 제외하고 계산했습니다" in fact


def test_rendering_preserves_missing_marker_and_real_zero() -> None:
    missing_summary = metric_summary("대상", {"period": "2026-03", "sales_억원": None, "ms_recent_pct": None, "rank": None}, "UBIST")
    zero_summary = metric_summary("대상", {"period": "2026-03", "sales_억원": 0.0, "ms_recent_pct": 0.0, "rank": 1}, "UBIST")
    segments = level_segments(
        [
            {"name": "결손", "value": None, "ms_recent_pct": None},
            {"name": "실제0", "value": 0.0, "ms_recent_pct": 0.0},
        ]
    )

    assert "매출 —" in missing_summary
    assert "MS —" in missing_summary
    assert "0.00" not in missing_summary
    assert "매출 0.00억원" in zero_summary
    assert segments[0]["value"] is None
    assert segments[0]["value_억원"] is None
    assert segments[1]["value"] == 0.0
    assert segments[1]["value_억원"] == 0.0


def test_chart_data_uses_null_for_missing_and_keeps_observed_zero() -> None:
    chart = share_chart(
        [
            {"brand": "결손", "rank": 1, "ms_recent_pct": None},
            {"brand": "실제0", "rank": 2, "ms_recent_pct": 0.0},
        ],
        target_brand=None,
    )

    assert chart["datasets"][0]["data"] == [None, 0.0]


def test_missing_market_denominator_blocks_share_but_keeps_real_zero_value() -> None:
    snapshot = _snapshot()
    target = snapshot.records[0]
    competitor = snapshot.records[1]
    competitor.metric_history["2025-03"]["source_status"] = "query_failed"

    assert snapshot.value_or_none(target, "2025-03") == 0.0
    assert snapshot.market_value_or_none("ml_test", "2025-03") is None
    assert snapshot.share_or_none("ml_test", target, "2025-03") is None
    assert snapshot.hhi("ml_test", "2025-03") is None


def test_zero_market_denominator_is_not_mislabeled_as_missing_data() -> None:
    snapshot = _snapshot(failed_middle=False)
    for record in snapshot.records:
        for row in record.metric_history.values():
            row["raw_value"] = 0.0

    data = brand_average_share_data(snapshot, "ml_test", "ubist", "대상", 3)

    assert data["avg_ms_pct"] is None
    assert "missing_periods" not in data
    assert data["ratio_unavailable_periods"] == ["2025-03", "2026-02", "2026-03"]
    assert "분모가 0이거나 없어" in data["ratio_availability_note"]


def test_comparison_chart_uses_gap_for_missing_segment() -> None:
    result = {
        "resolution": {"canonical_brand": "대상"},
        "sources": ["mart"],
        "tool_calls": [
            {
                "tool": "get_brand_metric",
                "render_data": {
                    "level": "Brand",
                    "level_segments": [
                        {"name": "결손", "ms_recent_pct": None, "value": None},
                        {"name": "실제0", "ms_recent_pct": 0.0, "value": 0.0},
                        {"name": "실제2", "ms_recent_pct": 2.0, "value": 20.0},
                    ],
                },
            }
        ],
    }

    charts = build_charts(result, question="브랜드별 비교", answer="브랜드별 비교입니다")

    comparison = next(chart for chart in charts if chart["title"] == "Brand별 점유율")
    assert comparison["datasets"][0]["data"] == [None, 0.0, 2.0]


def test_brand_sales_chart_keeps_real_zero_and_uses_null_gap() -> None:
    result = {
        "resolution": {"canonical_brand": "대상"},
        "sources": ["mart"],
        "tool_calls": [
            {
                "tool": "get_brand_metric",
                "render_data": {
                    "brand": "대상",
                    "brand_value_series_10pt": [
                        {"period": "2026-01", "value_krw": 1.0},
                        {"period": "2026-02", "value_krw": None},
                        {"period": "2026-03", "value_krw": 0.0},
                    ],
                },
            }
        ],
    }

    charts = build_charts(result, question="대상 매출 추이", answer="매출 추이입니다")

    series = next(chart for chart in charts if chart["title"] == "대상 매출 추이")
    assert series["datasets"][0]["data"] == [1.0, None, 0.0]


def test_all_missing_chart_is_suppressed() -> None:
    result = {
        "resolution": {"canonical_brand": "대상"},
        "sources": ["mart"],
        "tool_calls": [
            {
                "tool": "get_brand_metric",
                "render_data": {
                    "brand": "대상",
                    "brand_value_series_10pt": [
                        {"period": "2026-01", "value_krw": None},
                        {"period": "2026-02", "value_krw": None},
                    ],
                },
            }
        ],
    }

    assert build_charts(result, question="대상 매출 추이", answer="매출 추이입니다") == []


def test_csd_missing_activity_stays_null_while_real_zero_stays_zero() -> None:
    target = CsdActivityTarget("가드렛", "GUARDLET Market", "GUARDLET")
    tool = MetricsTool(
        mode="cache",
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS),
        csd_activity_reader=StaticCsdActivityReader(
            {("GUARDLET Market", "GUARDLET"): (("2026-03", None), ("2026-04", 0), ("2026-05", 5))}
        ),
        csd_activity_target_reader=StaticCsdActivityTargetReader((target,)),
    )

    result = tool.get_csd_activity_trend("가드렛", limit=3)

    assert result["status"] == "partial_data"
    assert result["render_data"]["series"] == [
        {"period": "2026-03", "product_details": None},
        {"period": "2026-04", "product_details": 0},
        {"period": "2026-05", "product_details": 5},
    ]
    assert result["render_data"]["missing_periods"] == ["2026-03"]
    assert "2026-03 데이터가 없어" in result["summary_text"]


def test_csd_target_selection_keeps_decimal_zero_distinct_from_missing() -> None:
    selected = _best_csd_activity_candidate(
        [
            {"market": "missing", "total_activity": None},
            {"market": "zero", "total_activity": Decimal("0")},
            {"market": "active", "total_activity": Decimal("3")},
        ]
    )

    assert selected == {"market": "active", "total_activity": Decimal("3")}
