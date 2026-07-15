from pipeline.scripts.api.dynamic_market.general_analysis_levels import cause_builder


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
