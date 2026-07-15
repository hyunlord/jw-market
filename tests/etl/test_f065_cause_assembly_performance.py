from __future__ import annotations

import ast
import builtins
import inspect
import json
from pathlib import Path
import sys
import textwrap
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline" / "scripts" / "etl"))

from pipeline.scripts.etl import build_cache_cause as cause
from pipeline.scripts.api.composers import cache_to_response


class CountingHistory(dict[str, dict[str, float]]):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.items_calls = 0

    def items(self):  # type: ignore[override]
        self.items_calls += 1
        return super().items()


def test_annual_rank_rows_reuse_one_neutral_reduction_per_label() -> None:
    first_history = CountingHistory(
        {
            "2025-01": {"raw_value": 10.0},
            "2025-02": {"raw_value": 20.0},
        }
    )
    second_history = CountingHistory(
        {
            "2025-01": {"raw_value": 5.0},
            "2025-02": {"raw_value": 15.0},
        }
    )
    rows = [
        {"brand_key": "A", "company_name": "Company A", "metric_history": first_history},
        {"brand_key": "B", "company_name": "Company B", "metric_history": second_history},
    ]
    cache: cause._AnnualRankRowsCache = {}

    target_rows, first_counts = cause._annual_rank_rows_from_full_rows(
        rows,
        label_key="brand",
        target_name="A",
        annual_rank_cache=cache,
    )
    neutral_rows, second_counts = cause._annual_rank_rows_from_full_rows(
        rows,
        label_key="brand",
        target_name=None,
        annual_rank_cache=cache,
    )
    company_rows, company_counts = cause._annual_rank_rows_from_full_rows(
        rows,
        label_key="company",
        target_name=None,
        annual_rank_cache=cache,
    )

    assert first_counts == second_counts == company_counts == {2025: 2}
    assert first_history.items_calls == second_history.items_calls == 1
    assert {row["company"] for row in company_rows[2025]} == {"Company A", "Company B"}
    target_a = next(row for row in target_rows[2025] if row["brand"] == "A")
    neutral_a = next(row for row in neutral_rows[2025] if row["brand"] == "A")
    assert target_a["is_target"] is True
    assert target_a["is_jw"] is True
    assert neutral_a["is_target"] is False
    assert neutral_a["is_jw"] is False

    target_a["value"] = -1.0
    assert neutral_a["value"] == 30.0


def test_series_values_with_observed_preserves_zero_and_missing() -> None:
    series = {
        "2025-01": {"raw_value": 0.0},
        "2025-02": {"raw_value": None},
    }
    cache: cause._SeriesObservedCache = {}

    values, observed = cause._series_values_with_observed(
        series,
        ["2025-01", "2025-02"],
        cache=cache,
    )

    assert list(values) == [0.0, 0.0]
    assert observed == (True, False)


def test_segment_rows_uses_channel_map_without_rechecking_field_presence(monkeypatch) -> None:
    row = {
        "brand_key": "A",
        "metric_history": {"2026-01": {"raw_value": 10.0}},
        "dimension_data": {},
        "dimension_channel_data": {
            "class": {
                "A": {
                    "병원": {"2026-01": {"raw_value": 10.0}},
                },
            },
        },
    }

    def fail_redundant_presence_probe(*_args, **_kwargs):
        raise AssertionError("dimension channel presence must come from the mapped result")

    monkeypatch.setattr(cause, "_has_dimension_channel_field", fail_redundant_presence_probe)

    segments = cause._segment_rows_for_level(
        rows=[row],
        level="Class",
        periods=["2026-01"],
        source="UBIST",
        channel="병원",
        target_name=None,
        top_n=None,
    )

    assert segments[0]["name"] == "A"
    assert segments[0]["value_series"] == [10.0]


def test_is_class_level_reuses_pure_level_classification() -> None:
    cause._is_class_level.cache_clear()

    assert cause._is_class_level("Class") is True
    assert cause._is_class_level("Class") is True
    assert cause._is_class_level.cache_info().hits == 1
    assert cause._is_class_level.cache_info().misses == 1


def test_safe_float_avoids_conversion_for_native_finite_numbers(monkeypatch) -> None:
    calls = 0
    original_float = builtins.float
    float_type = type(12.5)

    def count_float(value: float | int | str = 0.0) -> float:
        nonlocal calls
        calls += 1
        return original_float(value)

    monkeypatch.setattr(builtins, "float", count_float)

    assert isinstance(12.5, float_type)
    assert cause.safe_float(12.5) == 12.5
    assert calls == 0


def test_optional_period_value_reads_dict_fallback_inline(monkeypatch) -> None:
    calls = 0
    original = cause._optional_row_value

    def count_optional_row_value(row: dict[str, Any]) -> float | None:
        nonlocal calls
        calls += 1
        return original(row)

    monkeypatch.setattr(cause, "_optional_row_value", count_optional_row_value)

    assert cause._optional_value_from_period_item({"raw_value": 12.5}) == 12.5
    assert calls == 0


def test_period_value_reads_dict_fallback_inline(monkeypatch) -> None:
    calls = 0
    original = cause._optional_row_value

    def count_optional_row_value(row: dict[str, Any]) -> float | None:
        nonlocal calls
        calls += 1
        return original(row)

    monkeypatch.setattr(cause, "_optional_row_value", count_optional_row_value)

    assert cause._value_from_period_item({"raw_value": 12.5}) == 12.5
    assert calls == 0


def test_total_series_reuses_observed_period_cache(monkeypatch) -> None:
    periods = ["2025-01", "2025-02"]
    history = {
        periods[0]: {"raw_value": 10.0},
        periods[1]: {"raw_value": 20.0},
    }
    row = {"metric_history": history}
    observed_cache: dict[object, object] = {}
    cause._series_values_with_observed(history, periods, cache=observed_cache)

    calls = 0

    def count_value(item: object) -> float:
        nonlocal calls
        calls += 1
        return 0.0

    monkeypatch.setattr(cause, "_value_from_period_item", count_value)

    assert cause._total_series_for_rows(
        [row],
        periods,
        series_observed_cache=observed_cache,
    ) == [10.0, 20.0]
    assert calls == 0


def test_segment_rows_reuses_one_observed_series_pass_for_group_and_total(monkeypatch) -> None:
    row = {
        "brand_name": "Brand A",
        "brand_key": "brand-a",
        "by_dimension": {"class": "Class A"},
        "dimension_data": {
            "class": {"Class A": {"2025-01": {"raw_value": 10.0}}},
        },
        "dimension_channel_data": {},
        "dimension_specialty_data": {},
        "channel_data": {},
        "metric_history": {"2025-01": {"raw_value": 10.0}},
    }
    calls = 0
    original = cause._series_values_with_observed

    def count_observed_series(*args: Any, **kwargs: Any) -> tuple[Any, tuple[bool, ...]]:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(cause, "_series_values_with_observed", count_observed_series)

    segments = cause._segment_rows_for_level(
        rows=[row],
        level="Class",
        periods=["2025-01"],
        source="UBIST",
        channel="전체",
        target_name=None,
        top_n=None,
    )

    assert segments[0]["value_series"] == [10.0]
    assert segments[0]["series_pct"] == [100.0]
    assert segments[0]["recent_share_pct"] == 100.0
    assert calls == 1


def test_channel_rows_seed_series_cache_for_total_reduction(monkeypatch) -> None:
    periods = ["2025-01", "2025-02"]
    row = {
        "brand_name": "Brand A",
        "brand_key": "brand-a",
        "channel_data": json.dumps(
            {
                "상급종합병원": {
                    "2025-01": {"raw_value": 10.0},
                    "2025-02": {"raw_value": 20.0},
                }
            },
            ensure_ascii=False,
        ),
    }
    calls = 0
    original = cause._value_from_period_item

    def count_value(item: object) -> float:
        nonlocal calls
        calls += 1
        return original(item)

    monkeypatch.setattr(cause, "_value_from_period_item", count_value)

    channel_rows = cause._rows_for_channel([row], "UBIST", "상급종병", periods)
    calls_after_channel_rows = calls
    totals = cause._total_series_for_rows(channel_rows, periods)

    assert totals == [10.0, 20.0]
    assert calls_after_channel_rows == 2
    assert calls == calls_after_channel_rows


def test_latest_top_trends_ranks_each_year_once(monkeypatch) -> None:
    years = [2023, 2024, 2025]
    normalized_by_year = {
        year: [
            {"brand": "Target", "value": 30.0, "is_others": False},
            {"brand": "Other", "value": 20.0, "is_others": False},
            {"brand": "Third", "value": 10.0, "is_others": False},
        ]
        for year in years
    }
    sort_calls = 0
    original_sorted = builtins.sorted

    def count_sorted(*args: Any, **kwargs: Any) -> list[Any]:
        nonlocal sort_calls
        sort_calls += 1
        return original_sorted(*args, **kwargs)

    monkeypatch.setattr(builtins, "sorted", count_sorted)

    trends = cause._latest_top_trends(
        years=years,
        normalized_by_year=normalized_by_year,
        label_key="brand",
        target_name="Target",
        top_n=1,
    )

    assert [trend["brand"] for trend in trends] == ["Target", "Other", "기타"]
    assert sort_calls == len(years)


def test_latest_top_trends_parses_values_once_per_year(monkeypatch) -> None:
    years = [2023, 2024, 2025]
    normalized_by_year = {
        year: [
            {"brand": "Target", "value": 30.0, "ms_pct": 60.0, "is_others": False},
            {"brand": "Other", "value": 20.0, "ms_pct": 30.0, "is_others": False},
            {"brand": "Third", "value": 10.0, "ms_pct": 10.0, "is_others": False},
        ]
        for year in years
    }
    calls = 0
    original_safe_float = cause.safe_float

    def count_safe_float(value: object) -> float | None:
        nonlocal calls
        calls += 1
        return original_safe_float(value)

    monkeypatch.setattr(cause, "safe_float", count_safe_float)

    cause._latest_top_trends(
        years=years,
        normalized_by_year=normalized_by_year,
        label_key="brand",
        target_name="Target",
        top_n=1,
    )

    assert calls == len(years) * 13


def test_analysis_level_builds_reuse_shared_series_caches(monkeypatch) -> None:
    rows = [
        {
            "brand_name": "Brand A",
            "brand_key": "brand-a",
            "metric_history": {"2025-01": {"raw_value": 10.0}},
            "by_dimension": {"class": "Class A"},
            "dimension_data": {},
            "dimension_channel_data": {},
            "dimension_specialty_data": {},
            "channel_data": {},
            "overlay_data": {},
        }
    ]
    value_cache: cause._SeriesValueCache = {}
    observed_cache: cause._SeriesObservedCache = {}
    calls = 0
    original = cause._optional_value_from_period_item

    def count_optional_value(item: object) -> float | None:
        nonlocal calls
        calls += 1
        return original(item)

    monkeypatch.setattr(cause, "_optional_value_from_period_item", count_optional_value)
    kwargs = {
        "rows": rows,
        "source": "UBIST",
        "market": {"analyze_class": 1},
        "view_source_id": "synthetic",
        "target_name": None,
        "fallback_level_top5": {},
        "channels_override": ["전체"],
        "resolved_levels": {"Class"},
        "resolved_periods": ["2025-01"],
        "series_value_cache": value_cache,
        "series_observed_cache": observed_cache,
    }

    first = cause._build_analysis_levels_from_mart(**kwargs)
    first_calls = calls
    second = cause._build_analysis_levels_from_mart(**kwargs)

    assert first == second
    assert first_calls > 0
    assert calls == first_calls


def test_build_response_does_not_compute_unused_others_display_rows() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(cause.build_response)))
    display_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_display_brand_rows"
    ]

    assert len(display_calls) == 1


def test_level_top5_reuses_overall_channel_rows_across_levels(monkeypatch) -> None:
    rows = [{"brand_key": "A", "metric_history": {"2025-01": {"raw_value": 10.0}}}]
    analysis_levels = {
        "levels": ["Class", "Molecule"],
        "periods_monthly": ["2025-01"],
        "data": {
            "Class": {"by_channel": {"전체": []}},
            "Molecule": {"by_channel": {"전체": []}},
        },
    }
    calls = 0

    def count_rows(
        actual_rows: list[dict[str, Any]],
        _source: str,
        _channel: str,
        _periods: list[str],
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return actual_rows

    monkeypatch.setattr(cause, "_rows_for_channel", count_rows)

    payload = cause._level_top5_trend(
        analysis_levels,
        rows,
        "UBIST",
        "A",
    )

    assert calls == 1
    assert set(payload["by_level"]) == {"Class", "Molecule"}


def test_multi_value_fallback_updates_targets_in_one_series_pass(monkeypatch) -> None:
    row = {
        "brand_name": "Brand A",
        "brand_key": "brand-a",
        "by_dimension": {"strength_pack": "10mg | 20mg"},
        "metric_history": {"2025-01": {"raw_value": 10.0}},
        "dimension_data": {},
    }
    calls = 0
    original = cause._series_values_with_observed

    def count_observed_series(*args: Any, **kwargs: Any) -> tuple[Any, tuple[bool, ...]]:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(cause, "_series_values_with_observed", count_observed_series)

    segments = cause._segment_rows_for_level(
        rows=[row],
        level="용량",
        periods=["2025-01"],
        source="UBIST",
        channel="전체",
        target_name=None,
        top_n=None,
    )

    assert [segment["value_series"] for segment in segments] == [[10.0], [10.0]]
    assert calls == 1


def test_segment_level_invariants_are_resolved_once_per_call(monkeypatch) -> None:
    calls = 0
    original = cause._is_class_level

    def count_class_level(level: str) -> bool:
        nonlocal calls
        calls += 1
        return original(level)

    monkeypatch.setattr(cause, "_is_class_level", count_class_level)
    rows = [
        {
            "brand_name": f"Brand {index}",
            "brand_key": f"brand-{index}",
            "by_dimension": {"class": "Class A"},
            "metric_history": {"2025-01": {"raw_value": 10.0}},
            "dimension_data": {},
        }
        for index in range(3)
    ]

    cause._segment_rows_for_level(
        rows=rows,
        level="Class",
        periods=["2025-01"],
        source="UBIST",
        channel="전체",
        target_name=None,
        top_n=None,
    )

    assert calls == 1


def test_available_specialty_fields_scans_rows_once_as_a_presence_index() -> None:
    rows = [
        {"dimension_specialty_data": {}},
        {"dimension_specialty_data": {"molecule": {"Molecule A": {}}}},
    ]

    assert cause._available_specialty_dimension_fields(rows) == {"molecule"}


def test_segment_rows_preserves_period_alignment_for_array_accumulation() -> None:
    row = {
        "brand_name": "Brand A",
        "brand_key": "brand-a",
        "by_dimension": {"class": "Class A"},
        "metric_history": {
            "2025-01": {"raw_value": 10.0},
            "2025-02": {"raw_value": 25.0},
        },
    }

    segments = cause._segment_rows_for_level(
        rows=[row], level="Class", periods=["2025-01", "2025-02"],
        source="UBIST", channel="전체", target_name=None, top_n=None,
    )

    assert segments[0]["value_series"] == [10.0, 25.0]
    assert segments[0]["series_pct"] == [100.0, 100.0]


def test_channel_bucket_reuses_same_raw_channel() -> None:
    cause._channel_bucket.cache_clear()

    assert cause._channel_bucket("상급종합병원", "UBIST") == "상급종병"
    assert cause._channel_bucket("상급종합병원", "UBIST") == "상급종병"

    info = cause._channel_bucket.cache_info()
    assert info.misses == 1
    assert info.hits == 1


def test_channel_match_reuses_same_raw_channel() -> None:
    cause._channel_matches.cache_clear()

    assert cause._channel_matches("상급종합병원", "UBIST", "상급종병") is True
    assert cause._channel_matches("상급종합병원", "UBIST", "상급종병") is True

    info = cause._channel_matches.cache_info()
    assert info.misses == 1
    assert info.hits == 1


def test_dimension_segment_index_resolves_class_level_once(monkeypatch) -> None:
    calls = 0
    original = cause._is_class_level

    def count_class_level(level: str) -> bool:
        nonlocal calls
        calls += 1
        return original(level)

    monkeypatch.setattr(cause, "_is_class_level", count_class_level)
    rows = [
        {
            "brand_name": f"Brand {index}",
            "by_dimension": {"class": "Class A"},
            "metric_history": {"2025-01": {"raw_value": 10.0}},
            "dimension_data": {},
        }
        for index in range(3)
    ]

    cause._rows_for_dimension_segments(rows, "Class", ["2025-01"])

    assert calls == 1


def test_response_composer_does_not_walk_the_complete_payload_twice() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(cache_to_response.compose_cached_json)))
    deep_format_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "deep_format_numbers"
    ]

    assert deep_format_calls == []
