from __future__ import annotations

import ast
import inspect
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
