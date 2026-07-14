from __future__ import annotations

from pipeline.scripts.etl import build_cache_cause as cause


PERIODS = ["2026-04", "2026-05"]


def _row(label: str, series: dict[str, dict[str, float]]) -> dict[str, object]:
    return {
        "brand_name": label,
        "brand_key": label,
        "by_dimension": {"molecule": label},
        "dimension_data": {"molecule": {label: series}},
    }


def _segments(*rows: dict[str, object]) -> list[dict[str, object]]:
    return cause._segment_rows_for_level(
        rows=list(rows),
        level="Molecule",
        periods=PERIODS,
        source="UBIST",
        channel="전체",
        target_name=None,
        top_n=None,
        use_latest_valid_share=True,
    )


def test_dimension_wide_missing_period_is_null_with_reason() -> None:
    segments = _segments(
        _row("A", {"2026-04": {"raw_value": 70.0}}),
        _row("B", {"2026-04": {"raw_value": 30.0}}),
    )

    assert [segment["name"] for segment in segments] == ["A", "B"]
    assert segments[0]["value_series"] == [70.0, None]
    assert segments[0]["series_pct"] == [70.0, None]
    assert segments[0]["recent_share_pct"] == 70.0
    assert segments[0]["data_quality"] == {
        "available": False,
        "reason": "dimension_period_missing",
        "missing_periods": ["2026-05"],
    }


def test_explicit_zero_is_observed_not_missing() -> None:
    segments = _segments(
        _row(
            "A",
            {
                "2026-04": {"raw_value": 70.0},
                "2026-05": {"raw_value": 0.0},
            },
        ),
        _row(
            "B",
            {
                "2026-04": {"raw_value": 30.0},
                "2026-05": {"raw_value": 0.0},
            },
        ),
    )

    assert segments[0]["value_series"] == [70.0, 0.0]
    assert segments[0]["series_pct"] == [70.0, 0.0]
    assert "data_quality" not in segments[0]


def test_sparse_label_does_not_mark_observed_dimension_period_missing() -> None:
    segments = _segments(
        _row(
            "A",
            {
                "2026-04": {"raw_value": 70.0},
                "2026-05": {"raw_value": 20.0},
            },
        ),
        _row("B", {"2026-04": {"raw_value": 30.0}}),
    )

    assert segments[0]["value_series"] == [70.0, 20.0]
    assert segments[1]["value_series"] == [30.0, 0.0]
    assert all("data_quality" not in segment for segment in segments)


def test_level_top5_does_not_turn_missing_dimension_period_back_into_zero() -> None:
    data_quality = {
        "available": False,
        "reason": "dimension_period_missing",
        "missing_periods": ["2026-05"],
    }
    analysis_levels = {
        "levels": ["Molecule"],
        "periods_monthly": PERIODS,
        "data": {
            "Molecule": {
                "by_channel": {
                    "전체": [
                        {
                            "name": "전체",
                            "rank": 0,
                            "value_series": [100.0, 120.0],
                            "is_overall": True,
                        },
                        {
                            "name": "A",
                            "rank": 1,
                            "recent_share_pct": 70.0,
                            "series_pct": [70.0, None],
                            "value_series": [70.0, None],
                            "data_quality": data_quality,
                        },
                    ]
                }
            }
        },
    }

    result = cause._level_top5_trend(
        analysis_levels,
        rows=[],
        source="UBIST",
        target_name=None,
    )
    segment = result["by_level"]["Molecule"]["values"][1]

    assert segment["total_value"] is None
    assert segment["total_volume"] is None
    assert segment["brands_in_value"] == []
    assert segment["data_quality"] == data_quality
