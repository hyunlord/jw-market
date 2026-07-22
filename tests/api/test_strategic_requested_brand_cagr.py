from contextlib import nullcontext

import pytest

from pipeline.scripts.api.dynamic_market.general_analysis_levels import cause_builder


def _stub_build_response_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(cause_builder, "strategic_channel_totals_context", lambda _rows: nullcontext())
    monkeypatch.setattr(cause_builder, "resolve_market_channels", lambda **_: {})
    monkeypatch.setattr(cause_builder, "_strategic_levels", lambda _market, _rows: {"Class"})
    monkeypatch.setattr(cause_builder, "_history_periods", lambda _rows, _source: ["2026-01"])
    monkeypatch.setattr(cause_builder, "_channels_for_source", lambda _source: ["전체"])
    monkeypatch.setattr(cause_builder, "_catalog_members_for_market", lambda *_: [])
    monkeypatch.setattr(cause_builder, "current_analysis_level_source_epoch", lambda: None)
    monkeypatch.setattr(cause_builder, "metric_recent", lambda value: value if isinstance(value, dict) else {})
    monkeypatch.setattr(cause_builder, "_row_company", lambda row: row.get("company_name"))
    monkeypatch.setattr(cause_builder, "_stacked_ranking", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cause_builder, "_target_rank_overrides", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cause_builder, "_display_brand_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cause_builder, "_annual_share_hhi_from_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cause_builder, "_company_hhi_from_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cause_builder, "_data_period_coverage", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cause_builder, "_growth_contribution_payload", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cause_builder, "_target_customer_competition", lambda **_: {})
    monkeypatch.setattr(cause_builder, "_level_top5_trend", lambda *_args, **_kwargs: {"by_level": {}})
    monkeypatch.setattr(cause_builder, "_analysis_level_market_status_by_channel", lambda **_: {})
    monkeypatch.setattr(cause_builder, "_ensure_analysis_level_market_status_contract", lambda value: value)
    monkeypatch.setattr(cause_builder, "_matrix_payload", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        cause_builder,
        "latest_market_series_payload",
        lambda *_: {"market_size_series": [], "market_yoy_series": [], "market_yoy_recent_pct": None},
    )
    monkeypatch.setattr(cause_builder, "series_cagr", lambda *_: None)
    monkeypatch.setattr(cause_builder, "market_cagr_exclusive", lambda *_: (9.37, None))
    monkeypatch.setattr(cause_builder, "top3_share", lambda *_: None)
    monkeypatch.setattr(cause_builder, "_measure_labels", lambda *_: [])
    monkeypatch.setattr(
        cause_builder,
        "_build_analysis_levels_from_mart",
        lambda **_: {
            "levels": ["Class"],
            "channels": ["전체"],
            "periods_monthly": ["2026-01"],
            "data": {"Class": {"segments": [], "by_channel": {"전체": []}}},
        },
    )
    monkeypatch.setattr(cause_builder, "_ensure_split_class_alias", lambda value: value)
    monkeypatch.setattr(cause_builder, "_level_rows_by_segment", lambda *_: {})
    cause_builder.ANALYSIS_LEVELS_CACHE.clear()
    cause_builder.ANALYSIS_LEVELS_BY_CHANNEL_CACHE.clear()
    cause_builder.LEVEL_ROW_GROUPS_CACHE.clear()


@pytest.mark.parametrize(
    ("requested_brand", "representative_brand", "requested_cagr", "representative_cagr"),
    [
        ("리바로젯", "리바로", (None, 23.3769), (3.9056, None)),
        ("리바로하이", "리바로브이", (None, None), (-10.8417, None)),
    ],
)
def test_strategic_cagr_uses_requested_brand_when_catalog_target_differs(
    monkeypatch,
    requested_brand,
    representative_brand,
    requested_cagr,
    representative_cagr,
) -> None:
    # Given: the requested brand is not the catalog representative target.
    _stub_build_response_dependencies(monkeypatch)
    requested_history = {"requested-brand": {"raw_value": 1.0}}
    representative_history = {"catalog-target": {"raw_value": 2.0}}
    requested = {
        "brand_name": requested_brand,
        "brand_key": f"requested-{requested_brand}",
        "source": "ubist",
        "metric_history": requested_history,
        "extended_metric_history": {},
        "is_jw": True,
        "is_target": False,
    }
    representative = {
        "brand_name": representative_brand,
        "brand_key": f"representative-{representative_brand}",
        "company_name": "JW중외제약",
        "source": "ubist",
        "metric_history": representative_history,
        "extended_metric_history": {},
        "is_jw": True,
        "is_target": True,
    }

    def fake_brand_cagr(history):
        return requested_cagr if history is requested_history else representative_cagr

    monkeypatch.setattr(cause_builder, "brand_cagr_exclusive", fake_brand_cagr)

    # When: the strategic response is assembled for the requested brand.
    result = cause_builder.build_response(
        brand_row=requested,
        market_row={"market_size_series": {"2026-01": {"raw_value": 1.0}}},
        sibling_rows=[representative, requested],
        view_type="market_landscape",
        market_id="strategy_001",
        source="UBIST",
        measure="sales",
        view_source_id="ml_001",
        market_name="고지혈증 시장",
        market_sources=["UBIST"],
        market_catalog_row={"ml_id": "ml_001"},
    )

    # Then: CAGR follows the requested brand while legacy target metadata remains representative-based.
    assert result["brand"] == requested_brand
    assert result["data"]["kpi"]["brand_cagr_5y_pct"] == requested_cagr[0]
    assert result["data"]["kpi"]["brand_cagr_3y_pct"] == requested_cagr[1]
    assert result["data"]["kpi"]["target_brand"] == representative_brand
