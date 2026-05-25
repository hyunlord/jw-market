import pytest

from pipeline.scripts.validation.phase26_mart_loading_pipeline import (
    decode_json,
    evaluate_rows_for_period,
    latest_period,
    rank_ranges_by_raw,
)


def test_decode_json_accepts_dict_string_and_empty_values():
    assert decode_json({"a": 1}) == {"a": 1}
    assert decode_json('{"a": 1}') == {"a": 1}
    assert decode_json(None) == {}
    assert decode_json("") == {}


def test_latest_period_handles_monthly_and_quarterly_keys():
    assert latest_period({"2025-Q4": {}, "2026-Q1": {}, "2025-Q3": {}}) == "2026-Q1"
    assert latest_period({"2025-12": {}, "2026-01": {}, "2025-11": {}}) == "2026-01"


def test_rank_ranges_by_raw_is_tie_aware():
    ranges = rank_ranges_by_raw({"A": 100.0, "B": 100.0, "C": 50.0})
    assert ranges["A"] == (1, 2)
    assert ranges["B"] == (1, 2)
    assert ranges["C"] == (3, 3)


def test_evaluate_rows_for_period_passes_consistent_rows_with_ties():
    rows = [
        {"brand_name": "A", "metric_history": {"2026-04": {"raw_value": 100, "rank": 1, "ms": 40}}},
        {"brand_name": "B", "metric_history": {"2026-04": {"raw_value": 100, "rank": 2, "ms": 40}}},
        {"brand_name": "C", "metric_history": {"2026-04": {"raw_value": 50, "rank": 3, "ms": 20}}},
    ]
    issues, checked = evaluate_rows_for_period(
        table="mart_strategic_ml_brand_metric",
        market_id="ml_test",
        source="IQVIA",
        measure="sales",
        rows=rows,
        period="2026-04",
    )
    assert checked == 3
    assert issues == []


def test_evaluate_rows_for_period_detects_stale_general_rank_and_ms():
    rows = [
        {"brand_name": "헴리브라", "metric_history": {"2025-Q4": {"raw_value": 10876581805, "rank": 1, "ms": 46.46}}},
        {"brand_name": "애드베이트", "metric_history": {"2025-Q4": {"raw_value": 2979744452, "rank": 2, "ms": 12.73}}},
        {"brand_name": "애디노베이트", "metric_history": {"2025-Q4": {"raw_value": 2429369261, "rank": 3, "ms": 10.38}}},
        {"brand_name": "노보세븐알티", "metric_history": {"2025-Q4": {"raw_value": 1535233355, "rank": 1, "ms": 80.41}}},
    ]
    issues, checked = evaluate_rows_for_period(
        table="mart_strategic_ml_brand_metric",
        market_id="ml_013",
        source="IQVIA",
        measure="sales",
        rows=rows,
        period="2025-Q4",
    )
    assert checked == 4
    by_kind = {issue.kind for issue in issues if issue.brand_name == "노보세븐알티"}
    assert by_kind == {"rank", "ms"}


def test_evaluate_rows_for_period_recomputes_expected_ms():
    rows = [
        {"brand_name": "A", "metric_history": {"2026-04": {"raw_value": 100, "rank": 1, "ms": 50}}},
        {"brand_name": "B", "metric_history": {"2026-04": {"raw_value": 100, "rank": 2, "ms": 49}}},
    ]
    issues, _ = evaluate_rows_for_period(
        table="mart_strategic_ml_brand_metric",
        market_id="ml_test",
        source="UBIST",
        measure="sales",
        rows=rows,
        period="2026-04",
    )
    assert len(issues) == 1
    assert issues[0].brand_name == "B"
    assert issues[0].kind == "ms"
    assert issues[0].expected == pytest.approx(50.0)
