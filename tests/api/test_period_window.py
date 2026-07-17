from __future__ import annotations

import json

from pipeline.scripts.api.dynamic_market import period_window as period_window_module
from pipeline.scripts.api.dynamic_market.period_window import (
    _period_interval,
    trim_period_payload,
    trim_period_rows,
)
from pipeline.scripts.api.dynamic_market.types import PeriodRange


def test_trim_period_payload_filters_nested_month_quarter_and_year_series_without_zero_fill() -> None:
    payload = {
        "metric_history": {
            "2024-12": {"raw_value": 10.0},
            "2025-01": {"raw_value": 20.0},
            "2025-12": {"raw_value": 30.0},
            "2026-01": {"raw_value": 40.0},
        },
        "nested": {
            "quarterly": {"2024-Q4": 1.0, "2025-Q1": 2.0, "2026-Q1": 3.0},
            "annual": {"2024": 100.0, "2025": 200.0, "2026": 300.0},
        },
        "identity": {"market_id": "ml_006", "rank": 7},
    }

    result = trim_period_payload(payload, PeriodRange("2025-01", "2025-12"))

    assert list(result["metric_history"]) == ["2025-01", "2025-12"]
    assert result["nested"]["quarterly"] == {"2025-Q1": 2.0}
    assert result["nested"]["annual"] == {"2025": 200.0}
    assert result["identity"] == payload["identity"]


def test_trim_period_rows_filters_known_json_series_and_preserves_missing_periods() -> None:
    rows = [
        {
            "brand_key": "리바로",
            "metric_history": json.dumps(
                {"2025-01": {"raw_value": 1.0}, "2026-01": {"raw_value": 2.0}},
                ensure_ascii=False,
            ),
            "dimension_data": json.dumps(
                {
                    "class": {
                        "JW": {
                            "2025-01": {"raw_value": 1.0},
                            "2026-01": {"raw_value": 2.0},
                        }
                    }
                },
                ensure_ascii=False,
            ),
            "company_ranking_stacked": json.dumps(
                {"2025": [{"company": "JW"}], "2026": [{"company": "Other"}]},
                ensure_ascii=False,
            ),
        }
    ]

    result = trim_period_rows(rows, PeriodRange("2025-01", "2025-12"))

    assert json.loads(result[0]["metric_history"]) == {"2025-01": {"raw_value": 1.0}}
    assert json.loads(result[0]["dimension_data"])["class"]["JW"] == {
        "2025-01": {"raw_value": 1.0}
    }
    assert "2025-02" not in result[0]["metric_history"]
    assert json.loads(result[0]["company_ranking_stacked"]) == {"2025": [{"company": "JW"}]}


def test_trim_period_rows_reuses_predecoded_dimension_series(monkeypatch) -> None:
    period_series = {
        "class": {
            "JW": {
                "2025-01": {"raw_value": 1.0},
                "2026-01": {"raw_value": 2.0},
            }
        }
    }
    decoded_by_field = {
        "dimension_data": period_series,
        "dimension_channel_data": period_series,
        "dimension_specialty_data": period_series,
    }
    row = {
        field: json.dumps(decoded, ensure_ascii=False, sort_keys=True)
        for field, decoded in decoded_by_field.items()
    }
    row.update({f"__{field}": decoded for field, decoded in decoded_by_field.items()})
    encoded_values = {row[field] for field in decoded_by_field}
    original_loads = period_window_module.json.loads

    def reject_duplicate_decode(raw: str) -> object:
        if raw in encoded_values:
            raise AssertionError("predecoded dimension series must bypass json.loads")
        return original_loads(raw)

    monkeypatch.setattr(period_window_module.json, "loads", reject_duplicate_decode)

    result = trim_period_rows([row], PeriodRange("2025-01", "2025-12"))[0]

    expected = {"class": {"JW": {"2025-01": {"raw_value": 1.0}}}}
    for field, decoded in decoded_by_field.items():
        assert json.loads(result[field]) == expected
        assert result[f"__{field}"] is decoded


def test_trim_period_rows_can_defer_materializing_predecoded_dimension_series() -> None:
    period_series = {
        "class": {
            "JW": {
                "2025-01": {"raw_value": 1.0},
                "2026-01": {"raw_value": 2.0},
            }
        }
    }
    row = {
        "dimension_data": "{}",
        "dimension_channel_data": "{}",
        "dimension_specialty_data": "{}",
        "__dimension_data": period_series,
        "__dimension_channel_data": period_series,
        "__dimension_specialty_data": period_series,
    }

    result = trim_period_rows(
        [row],
        PeriodRange("2025-01", "2025-12"),
        materialize_predecoded_fields=False,
    )[0]

    for field in ("dimension_data", "dimension_channel_data", "dimension_specialty_data"):
        assert result[field] == "{}"
        assert result[f"__{field}"] is period_series


def test_unbounded_period_range_preserves_payload_byte_shape() -> None:
    raw = json.dumps({"2025-01": 1.0}, separators=(",", ":"))
    rows = [{"metric_history": raw}]

    result = trim_period_rows(rows, PeriodRange())

    assert result == rows
    assert result is not rows


def test_empty_period_window_does_not_invent_zero_points() -> None:
    rows = [
        {
            "metric_history": json.dumps({"2026-01": {"raw_value": 10.0}}),
            "hhi_series_5y": json.dumps([{"year": 2026, "hhi": 100.0}]),
        }
    ]

    result = trim_period_rows(rows, PeriodRange("2030-01", "2030-12"))

    assert json.loads(result[0]["metric_history"]) == {}
    assert json.loads(result[0]["hhi_series_5y"]) == []


def test_period_interval_parsing_reuses_cached_results() -> None:
    values = ("2025-01", "2025-Q1", "2025", "not-a-period")
    _period_interval.cache_clear()

    for _ in range(5):
        for value in values:
            _period_interval(value)

    cache = _period_interval.cache_info()
    assert cache.maxsize == 4096
    assert cache.misses == len(values)
    assert cache.hits == len(values) * 4


def test_period_window_boundaries_are_parsed_once_per_projection(monkeypatch) -> None:
    calls: list[str] = []
    original = period_window_module._period_interval

    def spy(value: str) -> tuple[int, int] | None:
        calls.append(value)
        return original(value)

    monkeypatch.setattr(period_window_module, "_period_interval", spy)

    result = trim_period_payload(
        {"2025-01": 1.0, "2025-02": 2.0, "2025-03": 3.0},
        PeriodRange("2025-02", "2025-03"),
    )

    assert result == {"2025-02": 2.0, "2025-03": 3.0}
    assert calls.count("2025-01") == 1
    assert calls.count("2025-02") == 2
    assert calls.count("2025-03") == 2
    assert len(calls) == 5


def test_non_period_mapping_stops_period_key_probe_after_first_miss(monkeypatch) -> None:
    calls: list[str] = []
    original = period_window_module._period_interval

    def spy(value: str) -> tuple[int, int] | None:
        calls.append(value)
        return original(value)

    monkeypatch.setattr(period_window_module, "_period_interval", spy)

    result = trim_period_payload(
        {
            "identity": {"market_id": "ml_006", "rank": 7},
            "series": {"2025-01": 1.0, "2026-01": 2.0},
            "metadata": {"source": "UBIST"},
        },
        PeriodRange("2025-01", "2025-12"),
    )

    assert result["series"] == {"2025-01": 1.0}
    assert "identity" in calls
    assert "series" not in calls
    assert "metadata" not in calls
    assert "rank" not in calls


def test_period_point_list_resolves_each_point_period_once(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    original = period_window_module._point_period

    def spy(value: dict[str, object]) -> str | None:
        calls.append(value)
        return original(value)

    monkeypatch.setattr(period_window_module, "_point_period", spy)

    result = trim_period_payload(
        [
            {"period": "2025-01", "raw_value": 1.0},
            {"period": "2026-01", "raw_value": 2.0},
        ],
        PeriodRange("2025-01", "2025-12"),
    )

    assert result == [{"period": "2025-01", "raw_value": 1.0}]
    assert len(calls) == 2
