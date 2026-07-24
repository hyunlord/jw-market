from contextlib import nullcontext
import json

from pipeline.scripts.api.dynamic_market.analysis_level_dimensions import (
    _analysis_rows,
    _general_dimensions_from_metrics,
    _general_sidecar_dimensions_by_pair,
)
from pipeline.scripts.api.dynamic_market.analysis_level_series import (
    with_dimension_series_from_labels_decoded,
)
from pipeline.scripts.api.dynamic_market.general_analysis_levels import cause_builder
from pipeline.scripts.api.dynamic_market import analysis_levels, general_analysis_levels
from pipeline.scripts.api.dynamic_market.types import MarketDefinition
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric


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


def test_target_customer_competition_forwards_request_series_cache(monkeypatch) -> None:
    rows = [{"brand_name": "A", "brand_key": "a"}]
    series_cache = {}
    captured = {}

    def fake_rows_for_channel(*args, **kwargs):
        captured["cache"] = kwargs.get("series_value_cache")
        return []

    monkeypatch.setattr(cause_builder, "_rows_for_channel", fake_rows_for_channel)
    monkeypatch.setattr(cause_builder, "_display_brand_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cause_builder, "_total_series_for_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cause_builder, "_period_rank_series_by_brand", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cause_builder, "_channel_data_quality", lambda *_args, **_kwargs: {})

    result = cause_builder._target_customer_competition(
        rows=rows,
        source="UBIST",
        target_name=None,
        periods=["2026-01"],
        channels=["의원"],
        series_value_cache=series_cache,
    )

    assert result["views"]
    assert captured["cache"] is series_cache


def test_period_rank_series_reuses_request_local_cache() -> None:
    rows = [{"brand_name": "A", "metric_history": {"2026-01": {"raw_value": 1.0}}}]
    periods = ["2026-01"]
    cache = {}

    first = cause_builder._period_rank_series_by_brand(
        rows,
        periods,
        rank_series_cache=cache,
    )
    second = cause_builder._period_rank_series_by_brand(
        rows,
        periods,
        rank_series_cache=cache,
    )

    assert second is first
    assert len(cache) == 1


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


def test_analysis_rows_reuse_windowed_channel_matrix_without_encoding() -> None:
    matrix = {"의원": {"내과": {"2025-06": 20.0, "2026-05": 30.0}}}
    brand = BrandMetric(
        brand_key="brand-a",
        brand_name="Brand A",
        atc4_code="A10N1",
        total_value=50.0,
        market_share_pct=100.0,
        rank=1,
        latest_period="2026-05",
        latest_value=30.0,
        history_by_period={"2025-06": 20.0, "2026-05": 30.0},
        channel_specialty_matrix=matrix,
        analysis_row={"channel_specialty_matrix": '{"stale": true}'},
    )
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=50.0,
        hhi=None,
        cagr=None,
        monthly_series=(
            {"period": "2025-06", "market_size": 20.0},
            {"period": "2026-05", "market_size": 30.0},
        ),
        brands=(brand,),
        all_brands=(brand,),
    )

    rows = _analysis_rows(
        metrics=metrics,
        focus=brand,
        general_dimensions={},
        sidecar_dimensions={},
        strategic_dimensions={},
    )

    assert rows[0]["channel_specialty_matrix"] == "{}"
    assert rows[0]["__channel_specialty_matrix"] is matrix


def test_analysis_rows_can_handoff_decoded_dimension_payloads() -> None:
    analysis_row = {
        "by_dimension": '{"seller": "JW중외제약"}',
        "dimension_data": '{"seller": {"JW중외제약": {"2026-01": {"raw_value": 1}}}}',
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

    default_rows = _analysis_rows(
        metrics=metrics,
        focus=metric,
        general_dimensions={},
        sidecar_dimensions={},
        strategic_dimensions={},
    )
    rows = _analysis_rows(
        metrics=metrics,
        focus=metric,
        general_dimensions={},
        sidecar_dimensions={},
        strategic_dimensions={},
        retain_decoded_dimensions=True,
    )

    assert "__by_dimension" not in default_rows[0]
    assert "__dimension_data" not in default_rows[0]
    assert rows[0]["__by_dimension"] == {"seller": "JW중외제약"}
    assert rows[0]["__dimension_data"] == {
        "seller": {"JW중외제약": {"2026-01": {"raw_value": 1}}}
    }


def test_analysis_rows_can_defer_intermediate_dimension_encoding() -> None:
    analysis_row = {
        "by_dimension": '{"seller": "JW중외제약"}',
        "dimension_data": '{"seller": {"JW중외제약": {"2026-01": {"raw_value": 1}}}}',
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

    materialized_rows = _analysis_rows(
        metrics=metrics,
        focus=metric,
        general_dimensions={},
        sidecar_dimensions={},
        strategic_dimensions={},
        retain_decoded_dimensions=True,
    )
    deferred_rows = _analysis_rows(
        metrics=metrics,
        focus=metric,
        general_dimensions={},
        sidecar_dimensions={},
        strategic_dimensions={},
        retain_decoded_dimensions=True,
        defer_dimension_data_encoding=True,
    )

    assert deferred_rows[0]["dimension_data"] == "{}"
    assert deferred_rows[0]["__dimension_data"] == {
        "seller": {"JW중외제약": {"2026-01": {"raw_value": 1}}}
    }
    specs = general_analysis_levels.GENERAL_LEVEL_SPECS["ubist"]
    assert general_analysis_levels._with_canonical_dimension_aliases(
        deferred_rows[0],
        specs,
    ) == general_analysis_levels._with_canonical_dimension_aliases(
        materialized_rows[0],
        specs,
    )


def test_general_sidecar_can_handoff_decoded_payloads(monkeypatch) -> None:
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
    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.analysis_level_dimensions.db.fetch_all",
        lambda *_args, **_kwargs: [
            {
                "brand_key": "brand-a",
                "brand_name": "Brand A",
                "atc4_code": "C10A1",
                "dimension_type": "seller",
                "dimension_value": "JW중외제약",
                "raw_value_history": '{"2026-01": {"raw_value": 1}}',
            }
        ],
    )

    default_payloads = _general_sidecar_dimensions_by_pair(
        metrics=metrics,
        mart_db="mart",
    )
    payloads = _general_sidecar_dimensions_by_pair(
        metrics=metrics,
        mart_db="mart",
        retain_decoded_dimensions=True,
    )

    assert default_payloads[("brand-a", "C10A1")] == {
        "by_dimension": '{"seller": "JW중외제약"}',
        "dimension_data": (
            '{"seller": {"JW중외제약": {"2026-01": {"raw_value": 1.0}}}}'
        ),
    }
    assert payloads[("brand-a", "C10A1")] == {
        "by_dimension": {"seller": "JW중외제약"},
        "dimension_data": {
            "seller": {"JW중외제약": {"2026-01": {"raw_value": 1.0}}}
        },
    }


def test_analysis_rows_do_not_redecode_decoded_sidecar_payloads(monkeypatch) -> None:
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
        analysis_row={"by_dimension": "{}", "dimension_data": "{}"},
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
    decode_inputs = []
    decode_json = cause_builder.decode_json

    def record_decode(raw):
        decode_inputs.append(raw)
        return decode_json(raw)

    monkeypatch.setattr(cause_builder, "decode_json", record_decode)

    rows = _analysis_rows(
        metrics=metrics,
        focus=metric,
        general_dimensions={},
        sidecar_dimensions={
            ("brand-a", "C10A1"): {
                "by_dimension": {"seller": "JW중외제약"},
                "dimension_data": {
                    "seller": {"JW중외제약": {"2026-01": {"raw_value": 1.0}}}
                },
            }
        },
        strategic_dimensions={},
        retain_decoded_dimensions=True,
    )

    assert rows[0]["__by_dimension"] == {"seller": "JW중외제약"}
    assert rows[0]["__dimension_data"] == {
        "seller": {"JW중외제약": {"2026-01": {"raw_value": 1.0}}}
    }
    assert not any(isinstance(raw, str) for raw in decode_inputs)


def test_general_aliases_reuse_handed_off_dimension_payloads(monkeypatch) -> None:
    row = {
        "by_dimension": '{"seller": "JW중외제약"}',
        "dimension_data": '{"seller": {"JW중외제약": {"2026-01": {"raw_value": 1}}}}',
        "dimension_channel_data": "{}",
        "dimension_specialty_data": "{}",
    }
    expected = general_analysis_levels._with_canonical_dimension_aliases(
        row,
        general_analysis_levels.GENERAL_LEVEL_SPECS["ubist"],
        defer_period_series_encoding=True,
    )
    encoded, dimension_data, by_dimension = with_dimension_series_from_labels_decoded(
        row["dimension_data"],
        row["by_dimension"],
        {"2026-01": 1.0},
    )
    cached_row = {
        **row,
        "dimension_data": encoded,
        "__dimension_data": dimension_data,
        "__by_dimension": by_dimension,
    }
    loads: list[str] = []
    json_loads = json.loads

    def record_loads(raw: str) -> object:
        loads.append(raw)
        return json_loads(raw)

    monkeypatch.setattr(general_analysis_levels.json, "loads", record_loads)

    actual = general_analysis_levels._with_canonical_dimension_aliases(
        cached_row,
        general_analysis_levels.GENERAL_LEVEL_SPECS["ubist"],
        defer_period_series_encoding=True,
    )

    assert actual == expected
    assert loads == ["{}", "{}"]


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
    brand_cohort = ("A", "B", "C", "D", "E", "F")
    build_calls = []
    trend_calls = []

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
    def fake_level_top5_trend(*_args, **kwargs):
        trend_calls.append(kwargs)
        return {"by_level": {}}

    monkeypatch.setattr(cause_builder, "_level_top5_trend", fake_level_top5_trend)
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
        brand_cohort=brand_cohort,
    )

    assert result is not None
    assert len(build_calls) == 2
    assert "brand_cohort" not in build_calls[0]
    assert build_calls[0]["series_value_cache"] is build_calls[1]["series_value_cache"]
    assert build_calls[0]["series_observed_cache"] is build_calls[1]["series_observed_cache"]
    assert trend_calls[0]["brand_cohort"] == brand_cohort
    assert trend_calls[0]["series_value_cache"] is build_calls[0]["series_value_cache"]


def test_strategic_ml_build_response_fixes_display_cohort_for_detail_builders(monkeypatch) -> None:
    rows = [{"brand_key": "same-a", "brand_name": "Same A", "company_name": "Same Co"}]
    brand_cohort = ("Same A", "Peer B", "Peer C", "Peer D", "Peer E", "Peer F")
    build_calls = []
    trend_calls = []
    competition_calls = []

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
    monkeypatch.setattr(
        cause_builder,
        "_display_brand_rows",
        lambda *_args, **_kwargs: [
            {"brand": brand, "is_target": brand == "Same A"}
            for brand in brand_cohort
        ],
    )
    monkeypatch.setattr(cause_builder, "_annual_share_hhi_from_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cause_builder, "_company_hhi_from_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cause_builder, "_data_period_coverage", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cause_builder, "_growth_contribution_payload", lambda *_args, **_kwargs: {})
    def fake_target_customer_competition(**kwargs):
        competition_calls.append(kwargs)
        return {}

    monkeypatch.setattr(cause_builder, "_target_customer_competition", fake_target_customer_competition)
    def fake_level_top5_trend(*_args, **kwargs):
        trend_calls.append(kwargs)
        return {"by_level": {}}

    monkeypatch.setattr(cause_builder, "_level_top5_trend", fake_level_top5_trend)
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
    assert result["data"]["kpi"]["brand_cagr_5y_pct"] is None
    assert result["data"]["kpi"]["brand_cagr_3y_pct"] is None
    assert len(build_calls) == 1
    assert competition_calls[0]["brand_cohort"] == brand_cohort
    assert trend_calls[0]["brand_cohort"] == brand_cohort
    assert trend_calls[0]["series_value_cache"] is build_calls[0]["series_value_cache"]
    assert competition_calls[0]["rank_series_cache"] is trend_calls[0]["rank_series_cache"]
