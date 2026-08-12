import pytest

from bundle_builder import ms_recomputer
from bundle_builder.ms_recomputer import recompute_ms_pct


def test_recompute_ms_pct():
    assert recompute_ms_pct(100, 1000) == pytest.approx(10.0)
    assert recompute_ms_pct(11687229691.75, 131340000000.0) == pytest.approx(8.9, abs=0.1)


def test_recompute_ms_pct_zero_market():
    assert recompute_ms_pct(100, 0) is None
    assert recompute_ms_pct(100, None) is None


def test_get_kpi_extras_from_mart_uses_metric_rows(monkeypatch) -> None:
    metric_rows = object()
    calculated = {
        "ei": 1.25,
        "target_rank": 2,
        "direct_competition_count": 14,
        "target_share_pct": 8.5,
    }
    function = getattr(ms_recomputer, "get_kpi_extras_from_mart", None)

    assert callable(function)
    monkeypatch.setattr(ms_recomputer, "fetch_metric_rows", lambda *_args: metric_rows)
    monkeypatch.setattr(ms_recomputer, "calculate_ml_kpi_extras", lambda rows: calculated if rows is metric_rows else {})

    assert function("리바로젯", "ml_006", "market_landscape", "UBIST", "sales", object()) == {
        "ei": 1.25,
        "ei_basis": None,
        "ei_period_years": None,
        "ei_note": None,
        "brand_cagr_5y_pct": None,
        "market_cagr_5y_pct": None,
        "momentum_score": None,
        "target_rank": 2,
        "total_brands_in_market": 14,
        "market_avg_ms_pct": None,
    }


def test_get_kpi_extras_from_mart_preserves_zero_values(monkeypatch) -> None:
    monkeypatch.setattr(ms_recomputer, "fetch_metric_rows", lambda *_args: object())
    monkeypatch.setattr(
        ms_recomputer,
        "calculate_ml_kpi_extras",
        lambda _rows: {
            "ei": 0.0,
            "target_ei": 9.0,
            "brand_cagr_5y_pct": 0.0,
            "brand_cagr_pct": 9.0,
            "market_cagr_5y_pct": 0.0,
            "market_cagr_pct": 9.0,
            "momentum_score": 0.0,
            "target_momentum": 9.0,
        },
    )

    result = ms_recomputer.get_kpi_extras_from_mart(
        "리바로젯", "ml_006", "market_landscape", "UBIST", "sales", object()
    )

    assert result["ei"] == 0.0
    assert result["brand_cagr_5y_pct"] == 0.0
    assert result["market_cagr_5y_pct"] == 0.0
    assert result["momentum_score"] == 0.0
