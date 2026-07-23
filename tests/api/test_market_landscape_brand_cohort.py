from __future__ import annotations

from pipeline.scripts.api.dynamic_market import cause_payload
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric, MarketDefinition
from pipeline.scripts.etl import build_cache_cause as cause_builder


def _row(brand: str, value: float, *, company: str | None = None) -> dict:
    return {
        "brand_name": brand,
        "company": company or f"{brand} 제조사",
        "metric_history": {"2026-05": {"raw_value": value, "value": value}},
    }


def test_display_brand_rows_uses_authoritative_market_landscape_cohort_order() -> None:
    rows = [
        _row("선택", 90.0),
        _row("경쟁1", 100.0),
        _row("경쟁2", 80.0),
        _row("경쟁3", 70.0),
        _row("경쟁4", 60.0),
        _row("경쟁5", 50.0),
        _row("위젯로컬상위", 200.0),
    ]
    cohort = ("선택", "경쟁1", "경쟁2", "경쟁3", "경쟁4", "경쟁5")

    selected = cause_builder._display_brand_rows(
        rows,
        target_name="선택",
        top_n=5,
        include_others=True,
        brand_cohort=cohort,
        cohort_rows=rows,
    )

    assert [row["brand"] for row in selected if not row["is_others"]] == list(cohort)
    assert selected[-1]["brand"] == "기타"
    assert selected[-1]["value_recent"] == 200.0


def test_display_brand_rows_zero_fills_cohort_member_missing_from_widget_scope() -> None:
    scoped_rows = [_row("선택", 90.0), _row("경쟁1", 100.0), _row("기타후보", 40.0)]
    market_rows = [*scoped_rows, _row("경쟁2", 80.0)]

    selected = cause_builder._display_brand_rows(
        scoped_rows,
        target_name="선택",
        top_n=5,
        include_others=True,
        brand_cohort=("선택", "경쟁1", "경쟁2"),
        cohort_rows=market_rows,
    )

    missing = next(row for row in selected if row["brand"] == "경쟁2")
    assert missing["value_recent"] == 0.0
    assert missing["share_pct"] == 0.0
    assert missing["company"] == "경쟁2 제조사"
    assert missing["data_quality"] == {"available": False, "reason": "no_data_in_widget_scope"}


def test_cause_builder_passes_matrix_cohort_to_all_brand_widget_builders(monkeypatch) -> None:
    captured: dict[str, tuple[str, ...] | None] = {}
    brands = tuple(
        BrandMetric(
            brand_key=f"b{index}",
            brand_name=f"브랜드{index}",
            atc4_code="C10A1",
            total_value=float(100 - index),
            market_share_pct=float(100 - index) / 7,
            rank=index,
            latest_period="2026-05",
            latest_value=float(100 - index),
            monthly_series=({"period": "2026-05", "value": float(100 - index)},),
        )
        for index in range(1, 8)
    )
    focus = brands[1]
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=700.0,
        hhi=None,
        cagr=None,
        monthly_series=({"period": "2026-05", "market_size": 700.0},),
        brands=brands,
        all_brands=brands,
    )
    definition = MarketDefinition(
        view="general",
        source="ubist",
        measure="sales",
        focus_brand_key=focus.brand_key,
        filter_echo={},
    )

    def fake_analysis_sections(**kwargs):
        captured["analysis"] = kwargs["brand_cohort"]
        return None

    def fake_target_competition(**kwargs):
        captured["customer"] = kwargs["brand_cohort"]
        return {}

    monkeypatch.setattr(cause_payload, "build_analysis_level_sections", fake_analysis_sections)
    monkeypatch.setattr(cause_payload, "_target_customer_competition_by_channel", fake_target_competition)

    data = cause_payload.build_cause_data(
        definition=definition,
        metrics=metrics,
        focus=focus,
    )

    expected = tuple(row["brand"] for row in data["ei_ms_matrix"]["data"])
    assert len(expected) == 6
    assert expected[0] == focus.brand_name
    assert captured == {"analysis": expected, "customer": expected}
    assert data["brand_ranking_stacked"]["top_brands"][:6] == list(expected)


def test_level_trend_zero_fills_missing_fixed_cohort_member() -> None:
    scoped_rows = [_row("선택", 90.0)]
    market_rows = [*scoped_rows, _row("경쟁1", 80.0)]

    payload = cause_builder._level_trend_brand_payloads(
        option_rows=scoped_rows,
        periods=["2026-05"],
        target_name="선택",
        total_series=[90.0],
        brand_cohort=("선택", "경쟁1"),
        cohort_rows=market_rows,
    )

    missing = next(row for row in payload if row["brand"] == "경쟁1")
    assert missing["value_series_10pt"] == [0.0]
    assert missing["ms_series_10pt"] == [0.0]
    assert missing["data_quality"] == {"available": False, "reason": "no_data_in_widget_scope"}
