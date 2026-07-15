from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline" / "scripts" / "etl"))

from pipeline.scripts.etl import build_cache_cause


def test_optional_row_value_uses_inline_fallback_order(monkeypatch) -> None:
    row = {"raw_value": None, "value": "12.5", "sales": 99.0}
    calls = 0

    def count_helper(*_values: object) -> float | None:
        nonlocal calls
        calls += 1
        return 12.5

    monkeypatch.setattr(build_cache_cause, "_first_optional_float", count_helper)

    assert build_cache_cause._optional_row_value(row) == 12.5
    assert calls == 0


def test_add_series_parses_each_period_once_for_shared_targets(monkeypatch) -> None:
    periods = ["2026-01", "2026-02"]
    series = {
        "2026-01": {"raw_value": 10.0},
        "2026-02": {"raw_value": 20.0},
    }
    first = {period: [0.0] for period in periods}
    second = {period: [0.0] for period in periods}
    calls = 0
    original = build_cache_cause._value_from_period_item

    def count_value(item: object) -> float:
        nonlocal calls
        calls += 1
        return original(item)

    monkeypatch.setattr(build_cache_cause, "_value_from_period_item", count_value)

    cache: dict[object, object] = {}
    build_cache_cause._add_series(first, series, periods, series_value_cache=cache)
    build_cache_cause._add_series(second, series, periods, series_value_cache=cache)

    assert first == second == {
        "2026-01": [10.0],
        "2026-02": [20.0],
    }
    assert calls == len(periods)


def test_dimension_channel_series_is_reduced_once_per_row(monkeypatch) -> None:
    row = {
        "dimension_channel_data": {
            "class": {
                "A": {
                    "병원": {
                        "2026-01": {"raw_value": 10.0},
                        "2026-02": {"raw_value": 20.0},
                    }
                }
            }
        }
    }
    calls = 0
    original = build_cache_cause._value_from_period_item

    def count_value(item: object) -> float:
        nonlocal calls
        calls += 1
        return original(item)

    monkeypatch.setattr(build_cache_cause, "_value_from_period_item", count_value)

    first = build_cache_cause._dimension_channel_series_map(row, "class", "UBIST", "병원")
    second = build_cache_cause._dimension_channel_series_map(row, "class", "UBIST", "병원")

    assert first == second
    assert calls == 2


def test_overall_level_options_reuse_channel_rows_across_levels(monkeypatch) -> None:
    data = {
        "Class": {
            "by_channel": {
                "전체": [{"name": "A", "value_series": [10.0]}],
            }
        },
        "Molecule": {
            "by_channel": {
                "전체": [{"name": "B", "value_series": [10.0]}],
            }
        },
    }
    calls = 0

    def count_rows(
        rows: list[dict[str, object]],
        source: str,
        channel: str,
        periods: list[str],
        **_kwargs: object,
    ) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        return rows

    monkeypatch.setattr(build_cache_cause, "_rows_for_channel", count_rows)

    build_cache_cause._with_overall_level_options(
        data=data,
        rows=[],
        source="UBIST",
        channels=["전체"],
        periods=["2026-01"],
    )

    assert calls == 1
