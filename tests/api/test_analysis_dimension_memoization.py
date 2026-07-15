from pipeline.scripts.api.dynamic_market.analysis_level_dimensions import _general_dimensions_from_metrics
from pipeline.scripts.api.dynamic_market.general_analysis_levels import cause_builder
from pipeline.scripts.api.dynamic_market import analysis_levels
from pipeline.scripts.api.dynamic_market.types import MarketDefinition
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric
from contextlib import nullcontext


def test_dimension_helpers_reuse_request_row_values() -> None:
    row = {
        "by_dimension": '{"class": "A", "molecule": "M"}',
        "dimension_data": '{"class": {"A": {"2026-01": {"raw_value": 1}}}}',
    }

    first_values = cause_builder._dimension_values(row, "Class")
    second_values = cause_builder._dimension_values(row, "Class")
    first_series = cause_builder._dimension_series_map(row, "class")
    second_series = cause_builder._dimension_series_map(row, "class")

    assert first_values is second_values
    assert first_series is second_series


def test_analysis_level_channels_match_requires_same_ordered_channels() -> None:
    analysis_levels = {"channels": ["전체", "의원"]}

    assert cause_builder._analysis_level_channels_match(analysis_levels, ["전체", "의원"])
    assert not cause_builder._analysis_level_channels_match(analysis_levels, ["의원", "전체"])
    assert not cause_builder._analysis_level_channels_match(analysis_levels, ["전체"])


def test_dimension_segment_index_matches_individual_segment_selection() -> None:
    rows = [
        {
            "brand_name": "A",
            "by_dimension": '{"class": "A"}',
            "dimension_data": '{"class": {"A": {"2026-01": {"raw_value": 2}}}}',
            "metric_history": {"2026-01": {"raw_value": 2}},
        },
        {
            "brand_name": "B",
            "by_dimension": '{"class": "B"}',
            "dimension_data": "{}",
            "metric_history": {"2026-01": {"raw_value": 3}},
        },
    ]
    periods = ["2026-01"]

    indexed = cause_builder._rows_for_dimension_segments(
        rows,
        "Class",
        periods,
    )

    assert indexed["A"] == cause_builder._rows_for_dimension(
        rows,
        "Class",
        "A",
        periods,
        source="UBIST",
        channel="전체",
    )
    assert indexed["B"] == cause_builder._rows_for_dimension(
        rows,
        "Class",
        "B",
        periods,
        source="UBIST",
        channel="전체",
    )


def test_general_dimensions_reuse_request_rows() -> None:
    analysis_row = {
        "by_dimension": '{"seller": "JW중외제약"}',
        "dimension_data": '{"seller": {"JW중외제약": {"2026-01": {"raw_value": 1}}}}',
        "dimension_channel_data": "{}",
        "channel_data": "{}",
        "channel_specialty_matrix": {},
    }
    metric = BrandMetric(
        "brand-a",
        "Brand A",
        "C10A1",
        1.0,
        100.0,
        1,
        "2026-01",
        1.0,
        history_by_period={"2026-01": 1.0},
        analysis_row=analysis_row,
    )
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=1.0,
        hhi=10000.0,
        cagr=None,
        monthly_series=({"period": "2026-01", "market_size": 1.0},),
        brands=(metric,),
        all_brands=(metric,),
    )

    dimensions = _general_dimensions_from_metrics(metrics)

    assert dimensions[("brand-a", "C10A1")] == {
        "by_dimension": analysis_row["by_dimension"],
        "dimension_data": analysis_row["dimension_data"],
        "dimension_channel_data": analysis_row["dimension_channel_data"],
        "channel_data": analysis_row["channel_data"],
        "channel_specialty_matrix": analysis_row["channel_specialty_matrix"],
    }


def test_level_top5_reuses_identical_overall_brand_payload(monkeypatch) -> None:
    calls = []
    total_series_calls = []

    def fake_payload(**kwargs):
        calls.append(kwargs)
        return [{"brand": "A", "value_recent": 1.0}]

    monkeypatch.setattr(cause_builder, "_level_trend_brand_payloads", fake_payload)
    monkeypatch.setattr(cause_builder, "_total_series_for_rows", lambda *_args, **_kwargs: total_series_calls.append(True) or [1.0])
    analysis_levels = {
        "levels": ["Class", "Molecule"],
        "periods_monthly": ["2026-01"],
        "data": {
            "Class": {"by_channel": {"전체": [{"name": "전체", "is_overall": True, "value_series": [1.0]}]}},
            "Molecule": {"by_channel": {"전체": [{"name": "전체", "is_overall": True, "value_series": [1.0]}]}},
        },
    }
    rows = [{"brand_name": "A", "metric_history": {"2026-01": {"raw_value": 1.0}}}]

    result = cause_builder._level_top5_trend(
        analysis_levels,
        rows,
        "UBIST",
        None,
        channel="전체",
    )

    assert len(calls) == 1
    assert total_series_calls == []
    assert result["by_level"]["Class"]["values"][0]["brands_in_value"] == result["by_level"]["Molecule"]["values"][0]["brands_in_value"]


def test_strategic_analysis_builds_share_request_local_series_caches(monkeypatch) -> None:
    rows = [{"brand_key": "a", "brand_name": "A"}]
    build_calls = []

    monkeypatch.setattr(analysis_levels, "build_analysis_rows", lambda **_: rows)
    monkeypatch.setattr(analysis_levels, "resolve_market_channels", lambda **_: {"specialty_channels": ["전문"]})
    monkeypatch.setattr(cause_builder, "_channels_for_source", lambda _source: ["전체"])
    monkeypatch.setattr(cause_builder, "_strategic_levels", lambda _market, _rows: {"Class"})
    monkeypatch.setattr(cause_builder, "_history_periods", lambda _rows, _source: ["2026-01"])

    def fake_build(**kwargs):
        build_calls.append(kwargs)
        return {
            "levels": ["Class"],
            "periods_monthly": ["2026-01"],
            "data": {"Class": {"segments": [], "by_channel": {"전체": [], "전문": []}}},
        }

    monkeypatch.setattr(cause_builder, "_build_analysis_levels_from_mart", fake_build)
    monkeypatch.setattr(cause_builder, "_ensure_split_class_alias", lambda value: value)
    monkeypatch.setattr(cause_builder, "_level_rows_by_segment", lambda *_: {})
    monkeypatch.setattr(cause_builder, "_level_top5_trend", lambda *_args, **_kwargs: {"by_level": {}})
    monkeypatch.setattr(cause_builder, "_analysis_level_market_status_by_channel", lambda **_: {})
    monkeypatch.setattr(cause_builder, "_ensure_analysis_level_market_status_contract", lambda value: value)

    definition = MarketDefinition(
        view="strategic",
        filter_echo={},
        source="ubist",
        measure="sales",
        market_catalog_row={"ml_id": "ml_001"},
    )
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=1.0,
        hhi=None,
        cagr=None,
        monthly_series=({"period": "2026-01", "market_size": 1.0},),
        brands=(),
    )

    result = analysis_levels.build_analysis_level_sections(
        definition=definition,
        metrics=metrics,
        focus=None,
        mart_db="jw_mart",
    )

    assert result is not None
    assert len(build_calls) == 2
    assert build_calls[0]["series_value_cache"] is build_calls[1]["series_value_cache"]
    assert build_calls[0]["series_observed_cache"] is build_calls[1]["series_observed_cache"]


def test_legacy_build_response_reuses_analysis_levels_when_channels_match(monkeypatch) -> None:
    rows = [{"brand_key": "same-a", "brand_name": "Same A", "company_name": "Same Co"}]
    build_calls = []

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
    monkeypatch.setattr(cause_builder, "latest_market_series_payload", lambda *_: {
        "market_size_series": [],
        "market_yoy_series": [],
        "market_yoy_recent_pct": None,
    })
    monkeypatch.setattr(cause_builder, "series_cagr", lambda *_: None)
    monkeypatch.setattr(cause_builder, "top3_share", lambda *_: None)
    monkeypatch.setattr(cause_builder, "_measure_labels", lambda *_: [])
    cause_builder.ANALYSIS_LEVELS_CACHE.clear()
    cause_builder.ANALYSIS_LEVELS_BY_CHANNEL_CACHE.clear()
    cause_builder.LEVEL_ROW_GROUPS_CACHE.clear()

    def fake_build(**kwargs):
        build_calls.append(kwargs)
        return {
            "levels": ["Class"],
            "channels": ["전체"],
            "periods_monthly": ["2026-01"],
            "data": {"Class": {"segments": [], "by_channel": {"전체": []}}},
        }

    monkeypatch.setattr(cause_builder, "_build_analysis_levels_from_mart", fake_build)
    monkeypatch.setattr(cause_builder, "_ensure_split_class_alias", lambda value: value)
    monkeypatch.setattr(cause_builder, "_level_rows_by_segment", lambda *_: {})
    result = cause_builder.build_response(
        brand_row={
            "brand_name": "Same A",
            "brand_key": "same-a",
            "source": "ubist",
            "metric_history": {},
            "extended_metric_history": {},
            "is_jw": False,
            "is_target": False,
        },
        market_row={"market_size_series": {"2026-01": {"raw_value": 1.0}}},
        sibling_rows=rows,
        view_type="market_landscape",
        market_id="same-market",
        source="UBIST",
        measure="sales",
        view_source_id="same-source",
        market_name="Same Market",
        market_sources=["UBIST"],
        market_catalog_row={"ml_id": "ml_same_channels"},
    )

    assert result is not None
    assert len(build_calls) == 1
