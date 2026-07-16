from __future__ import annotations

from collections import UserDict
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


def test_trim_period_rows_reuses_predecoded_dimension_series(monkeypatch) -> None:
    # Given canonical dimension JSON paired with the decoded objects used by downstream aggregation.
    period_series = {
        "seller": {
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
        field: json.dumps(value, ensure_ascii=False, sort_keys=True)
        for field, value in decoded_by_field.items()
    }
    row.update({f"__{field}": value for field, value in decoded_by_field.items()})
    loads_calls: list[str] = []
    original_loads = period_window_module.json.loads

    def spy_loads(value: str):
        loads_calls.append(value)
        return original_loads(value)

    monkeypatch.setattr(period_window_module.json, "loads", spy_loads)

    # When the row is projected to one year.
    result = trim_period_rows([row], PeriodRange("2025-01", "2025-12"))[0]

    # Then the encoded fields are projected without reparsing and private values remain untouched.
    expected = {
        "seller": {
            "JW": {
                "2025-01": {"raw_value": 1.0},
            }
        }
    }
    assert loads_calls == []
    for field, decoded in decoded_by_field.items():
        assert result[field] == json.dumps(expected, ensure_ascii=False, sort_keys=True)
        assert result[f"__{field}"] is decoded


def test_trim_period_payload_skips_mapping_protocol_for_json_scalars(monkeypatch) -> None:
    checks: list[type[object]] = []

    class MappingProbeMeta(type):
        def __instancecheck__(cls, instance: object) -> bool:
            checks.append(type(instance))
            return False

    class MappingProbe(metaclass=MappingProbeMeta):
        pass

    monkeypatch.setattr(period_window_module, "Mapping", MappingProbe)

    result = trim_period_payload(1.5, PeriodRange("2025-01", "2025-12"))

    assert result == 1.5
    assert checks == []


def test_trim_period_payload_preserves_mapping_and_list_subclass_support() -> None:
    class PeriodPoints(list[UserDict[str, object]]):
        pass

    payload = UserDict(
        {
            "history": UserDict({"2025-01": 1.0, "2026-01": 2.0}),
            "points": PeriodPoints(
                [
                    UserDict({"period": "2025-01", "value": 1.0}),
                    UserDict({"period": "2026-01", "value": 2.0}),
                ]
            ),
        }
    )

    result = trim_period_payload(payload, PeriodRange("2025-01", "2025-12"))

    assert result == {
        "history": {"2025-01": 1.0},
        "points": [{"period": "2025-01", "value": 1.0}],
    }
