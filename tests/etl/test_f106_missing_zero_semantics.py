from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline" / "scripts" / "etl"))

from pipeline.scripts.etl import build_cache_cause as cause


def test_rank_normalization_keeps_missing_distinct_from_real_zero() -> None:
    rows = [
        {"brand": "sold", "value": 100.0},
        {"brand": "zero", "value": 0.0},
        {"brand": "missing", "value": None},
    ]

    result = cause._rank_normalized_rows(rows, label_key="brand")
    by_brand = {row["brand"]: row for row in result}

    assert [row["brand"] for row in result] == ["sold", "zero", "missing"]
    assert by_brand["sold"]["ms_pct"] == 100.0
    assert by_brand["zero"]["value"] == 0.0
    assert by_brand["zero"]["ms_pct"] == 0.0
    assert by_brand["missing"]["value"] is None
    assert by_brand["missing"]["ms_pct"] is None
    assert by_brand["missing"]["rank"] is None
    assert by_brand["missing"]["data_quality"] == {
        "available": False,
        "reason": "no_data",
    }


def test_normalize_rank_row_does_not_invent_zero_for_missing_value_or_share() -> None:
    normalized = cause._normalize_rank_row(
        {"brand": "missing", "value": None, "ms_pct": None},
        label_key="brand",
        target_name=None,
    )

    assert normalized["value"] is None
    assert normalized["ms_pct"] is None


def test_analysis_levels_preserves_observed_zero_share() -> None:
    result = cause._analysis_levels(
        {
            "판매사": {
                "2026-05": [
                    {"name": "zero", "raw_value": 0.0, "ms": 0.0},
                ]
            }
        },
        "UBIST",
    )

    segment = result["data"]["판매사"]["by_channel"]["전체"][0]
    assert segment["value_series"] == [0.0]
    assert segment["recent_share_pct"] == 0.0


def test_legacy_analysis_level_normalization_preserves_missing_latest_value() -> None:
    result = cause._normalize_analysis_levels(
        {
            "판매사": {
                "known": {"2026-05": {"raw_value": 10.0}},
                "missing": {"2026-05": {"raw_value": None}},
                "zero": {"2026-05": {"raw_value": 0.0}},
            }
        },
        {},
        "UBIST",
    )
    by_name = {
        row["name"]: row
        for row in result["data"]["판매사"]["by_channel"]["전체"]
    }

    assert by_name["missing"]["value_series"] == [None]
    assert by_name["missing"]["recent_share_pct"] is None
    assert by_name["missing"]["data_quality"] == {"available": False, "reason": "no_data"}
    assert by_name["zero"]["value_series"] == [0.0]
    assert "data_quality" not in by_name["zero"]


def test_matrix_average_ignores_missing_but_preserves_real_zero() -> None:
    result = cause._matrix_payload(
        [
            {"brand": "known", "share_pct": 50.0},
            {"brand": "zero", "share_pct": 0.0},
            {"brand": "missing", "share_pct": None},
        ]
    )

    assert result["ms_avg_pct"] == 25.0
    assert result["share_avg_pct"] == 25.0
    assert cause._matrix_payload([{"brand": "missing", "share_pct": None}])["ms_avg_pct"] is None


def test_segment_sum_cannot_hide_an_incomplete_period() -> None:
    summed = cause._sum_segment_value_series(
        [
            {"value_series": [10.0, 20.0]},
            {"value_series": [None, 0.0]},
        ],
        ["2026-04", "2026-05"],
    )

    assert summed == [None, 20.0]
    assert cause._series_covers_options([10.0, 20.0], summed) is False


def test_annual_latest_points_preserve_missing_hhi() -> None:
    points = cause._annual_latest_points(
        {
            "2025-12": {"hhi": None},
            "2026-05": {"hhi": 0.0},
        },
        value_key="hhi",
    )

    assert points[0]["hhi"] is None
    assert points[0]["data_quality"] == {"available": False, "reason": "no_data"}
    assert points[1]["hhi"] == 0.0
    assert "data_quality" not in points[1]


def test_company_hhi_is_missing_when_any_component_share_is_missing() -> None:
    result = cause._company_hhi_from_ranking(
        {
            "2026-05": [
                {"company": "known", "ms_pct": 100.0},
                {"company": "missing", "ms_pct": None},
            ]
        }
    )

    assert result == {
        "periods": ["2026"],
        "hhi_values": [None],
        "data_quality": [{"available": False, "reason": "no_data"}],
    }


def test_display_rows_keep_missing_latest_values_out_of_market_denominator(monkeypatch) -> None:
    cause.EI_META_CACHE.clear()
    monkeypatch.setattr(cause, "calculate_ei_with_fallback", lambda *_args: {})
    rows = [
        {
            "brand_name": "known",
            "brand_key": "known",
            "metric_history": {"2026-01": {"raw_value": 100.0, "ms": 100.0}},
        },
        {
            "brand_name": "zero",
            "brand_key": "zero",
            "metric_history": {"2026-01": {"raw_value": 0.0, "ms": 0.0}},
        },
        {
            "brand_name": "missing",
            "brand_key": "missing",
            "metric_history": {"2026-01": {"raw_value": None, "ms": None}},
        },
    ]

    result = cause._display_brand_rows(
        rows,
        target_name="known",
        include_others=False,
        market_series=None,
    )
    by_brand = {row["brand"]: row for row in result}

    assert by_brand["known"]["share_pct"] == 100.0
    assert by_brand["zero"]["value_recent"] == 0.0
    assert by_brand["zero"]["share_pct"] == 0.0
    assert by_brand["missing"]["value_recent"] is None
    assert by_brand["missing"]["share_pct"] is None
    assert by_brand["missing"]["data_quality"] == {
        "available": False,
        "reason": "no_data",
    }


def test_company_waterfall_does_not_sum_missing_as_zero() -> None:
    result = cause._company_waterfall(
        [
            {
                "brand": "known",
                "company": "A",
                "growth_contribution": 10.0,
                "growth_contribution_pct": 100.0,
                "value_recent": 20.0,
            },
            {
                "brand": "missing",
                "company": "B",
                "growth_contribution": None,
                "growth_contribution_pct": None,
                "value_recent": None,
            },
        ],
        target_company=None,
    )
    by_company = {row["company"]: row for row in result["top_contributors"]}

    assert by_company["B"]["contribution"] is None
    assert by_company["B"]["contribution_pct"] is None
    assert by_company["B"]["value_recent"] is None
    assert by_company["B"]["data_quality"] == {
        "available": False,
        "reason": "no_data",
    }


def test_missing_growth_period_does_not_create_a_zero_based_contribution() -> None:
    rows = [
        {
            "brand_name": "complete",
            "company_name": "A",
            "metric_history": {
                "2025-01": {"raw_value": 10.0},
                "2026-01": {"raw_value": 20.0},
            },
        },
        {
            "brand_name": "missing-start",
            "company_name": "B",
            "metric_history": {"2026-01": {"raw_value": 5.0}},
        },
        {
            "brand_name": "real-zero",
            "company_name": "C",
            "metric_history": {
                "2025-01": {"raw_value": 0.0},
                "2026-01": {"raw_value": 0.0},
            },
        },
    ]

    result, market_start, market_end, market_growth = cause._top_contribution_rows(
        rows,
        target_name=None,
        periods=["2025-01", "2026-01"],
        top_n=5,
    )
    by_brand = {row["brand"]: row for row in result}

    assert (market_start, market_end, market_growth) == (None, None, None)
    assert by_brand["missing-start"]["contribution"] is None
    assert by_brand["missing-start"]["value_start"] is None
    assert by_brand["missing-start"]["data_quality"] == {
        "available": False,
        "reason": "no_data",
    }
    assert by_brand["real-zero"]["contribution"] == 0.0
    assert "data_quality" not in by_brand["real-zero"]


def test_level_trend_brand_payload_preserves_missing_recent_value(monkeypatch) -> None:
    monkeypatch.setattr(
        cause,
        "_display_brand_rows",
        lambda *_args, **_kwargs: [
            {
                "brand": "missing",
                "company": "A",
                "value_recent": None,
                "raw_value": None,
                "share_pct": None,
                "rank": None,
            }
        ],
    )

    result = cause._level_trend_brand_payloads(
        option_rows=[],
        periods=["2026-05"],
        target_name=None,
        total_series=[100.0],
    )

    assert result == []

    result = cause._level_trend_brand_payloads(
        option_rows=[{"brand_name": "missing", "metric_history": {}}],
        periods=["2026-05"],
        target_name=None,
        total_series=[100.0],
    )
    assert result[0]["value_recent"] is None
    assert result[0]["raw_value"] is None
    assert result[0]["ms_recent_pct"] is None
    assert result[0]["value_series_10pt"] == [None]
    assert result[0]["volume_series_10pt"] == [None]
    assert result[0]["ms_series_10pt"] == [None]
    assert result[0]["data_quality"] == {"available": False, "reason": "no_data"}


def test_annual_rank_rows_preserve_missing_catalog_identity() -> None:
    by_year, period_counts = cause._annual_rank_rows(
        {
            "2026-05": [
                {"brand": "known", "value": 10.0},
                {"brand": "missing", "value": None},
                {"brand": "real-zero", "value": 0.0},
            ]
        },
        label_key="brand",
        target_name=None,
    )
    by_brand = {row["brand"]: row for row in by_year[2026]}

    assert period_counts == {2026: 1}
    assert by_brand["missing"]["value"] is None
    assert by_brand["missing"]["ms_pct"] is None
    assert by_brand["missing"]["data_quality"] == {
        "available": False,
        "reason": "no_data",
    }
    assert by_brand["real-zero"]["value"] == 0.0
    assert by_brand["real-zero"]["ms_pct"] == 0.0


def test_full_row_annual_rank_cache_preserves_incomplete_year() -> None:
    rows = [
        {
            "brand_name": "known",
            "company_name": "A",
            "metric_history": {"2026-05": {"raw_value": 10.0}},
        },
        {
            "brand_name": "missing",
            "company_name": "B",
            "metric_history": {"2026-05": {"raw_value": None}},
        },
        {
            "brand_name": "real-zero",
            "company_name": "C",
            "metric_history": {"2026-05": {"raw_value": 0.0}},
        },
    ]

    by_year, period_counts = cause._annual_rank_rows_from_full_rows(
        rows,
        label_key="brand",
        target_name=None,
    )
    by_brand = {row["brand"]: row for row in by_year[2026]}

    assert period_counts == {2026: 1}
    assert by_brand["missing"]["value"] is None
    assert by_brand["missing"]["ms_pct"] is None
    assert by_brand["missing"]["data_quality"] == {
        "available": False,
        "reason": "no_data",
    }
    assert by_brand["real-zero"]["value"] == 0.0
    assert by_brand["real-zero"]["ms_pct"] == 0.0


def test_period_rank_does_not_rank_a_partial_brand_sum() -> None:
    ranks = cause._period_rank_series_by_brand(
        [
            {
                "brand_name": "complete",
                "metric_history": {"2026-05": {"raw_value": 10.0}},
            },
            {
                "brand_name": "partial",
                "metric_history": {"2026-05": {"raw_value": 20.0}},
            },
            {
                "brand_name": "partial",
                "metric_history": {"2026-05": {"raw_value": None}},
            },
            {
                "brand_name": "real-zero",
                "metric_history": {"2026-05": {"raw_value": 0.0}},
            },
        ],
        ["2026-05"],
    )

    assert ranks["complete"] == [1]
    assert ranks["partial"] == [None]
    assert ranks["real-zero"] == [None]
