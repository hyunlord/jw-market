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
