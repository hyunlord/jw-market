from __future__ import annotations

import pytest

from bundle_builder.market_kpi_calculator import calculate_ml_kpi_extras
from bundle_builder.mart_metric_reader import MlMetricRows


def _brand_row(
    brand_name: str,
    raw_value: float,
    *,
    rank: int,
    ms: float = 0.0,
    cagr_5y: float | None = 0.12,
    momentum_score: float | None = 3.5,
) -> dict:
    extended = {}
    if cagr_5y is not None:
        extended["cagr_5y"] = cagr_5y
    if momentum_score is not None:
        extended["momentum_score"] = momentum_score
    return {
        "brand_name": brand_name,
        "brand_key": brand_name,
        "source": "ubist",
        "measure": "sales",
        "metric_history": {
            "2021-04": {"raw_value": raw_value / 2, "ms": ms, "rank": rank},
            "2026-04": {"raw_value": raw_value, "ms": ms, "rank": rank},
        },
        "extended_metric_history": {"2026-04": extended},
        "is_jw": brand_name == "리바로젯",
    }


def test_calculate_ml_kpi_extras_recomputes_share_from_market_total() -> None:
    rows = MlMetricRows(
        brand_row=_brand_row("리바로젯", 532_130_000.0, rank=2, ms=99.0),
        market_row={
            "market_size_series": {"2021-04": 5_000_000_000.0, "2026-04": 10_000_000_000.0},
            "brand_ranking_stacked": {"from": "mart"},
            "ei_ms_matrix": {"data": [{"brand": "리바로젯"}]},
        },
        sibling_rows=(
            _brand_row("리바로젯", 532_130_000.0, rank=2, ms=99.0),
            _brand_row("경쟁약", 9_467_870_000.0, rank=1, ms=1.0),
        ),
    )

    result = calculate_ml_kpi_extras(rows)

    assert result["target_share_pct"] == pytest.approx(5.3213)
    assert result["brand_share_pct"] == pytest.approx(5.3213)
    assert result["ms_pct"] == pytest.approx(5.3213)
    assert result["target_rank"] == 2
    assert result["brand_ranking_stacked"] == {"from": "mart"}
    assert result["ei_ms_matrix"] == {"data": [{"brand": "리바로젯"}]}


def test_calculate_ml_kpi_extras_falls_back_to_sibling_sum_when_market_total_missing() -> None:
    rows = MlMetricRows(
        brand_row=_brand_row("리바로젯", 20.0, rank=1, ms=0.0),
        market_row={"market_size_series": {"2026-04": 0}},
        sibling_rows=(
            _brand_row("리바로젯", 20.0, rank=1, ms=0.0),
            _brand_row("경쟁약", 80.0, rank=2, ms=0.0),
        ),
    )

    result = calculate_ml_kpi_extras(rows)

    assert result["target_share_pct"] == pytest.approx(20.0)
    assert result["market_size_recent"] == pytest.approx(100.0)
    assert result["market_avg_ms_pct"] == pytest.approx(50.0)
    assert result["direct_competition_count"] == 2


def test_calculate_ml_kpi_extras_uses_catalog_member_count_when_larger() -> None:
    rows = MlMetricRows(
        brand_row=_brand_row("리바로젯", 20.0, rank=1, ms=0.0),
        market_row={"market_size_series": {"2026-04": 100.0}},
        sibling_rows=(
            _brand_row("리바로젯", 20.0, rank=1, ms=0.0),
            _brand_row("경쟁약", 80.0, rank=2, ms=0.0),
        ),
        catalog_member_count=5,
    )

    result = calculate_ml_kpi_extras(rows)

    assert result["direct_competition_count"] == 5


def test_calculate_ml_kpi_extras_preserves_missing_ei_values() -> None:
    rows = MlMetricRows(
        brand_row=_brand_row("리바로젯", 20.0, rank=1, cagr_5y=None, momentum_score=None),
        market_row={"market_size_series": {"2026-04": 100.0}},
        sibling_rows=(_brand_row("리바로젯", 20.0, rank=1, cagr_5y=None, momentum_score=None),),
    )

    result = calculate_ml_kpi_extras(rows)

    assert result["ei"] is None
    assert result["target_ei"] is None
    assert result["brand_cagr_5y_pct"] is None
    assert result["momentum_score"] is None
