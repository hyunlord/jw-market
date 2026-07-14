from __future__ import annotations

import json

from pipeline.scripts.api.dynamic_market.period_window import (
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
