from pipeline.scripts.api.dynamic_market.analysis_level_dimensions import _general_dimensions_from_metrics
from pipeline.scripts.api.dynamic_market.general_analysis_levels import cause_builder
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

    def fake_payload(**kwargs):
        calls.append(kwargs)
        return [{"brand": "A", "value_recent": 1.0}]

    monkeypatch.setattr(cause_builder, "_level_trend_brand_payloads", fake_payload)
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
    assert result["by_level"]["Class"]["values"][0]["brands_in_value"] == result["by_level"]["Molecule"]["values"][0]["brands_in_value"]
