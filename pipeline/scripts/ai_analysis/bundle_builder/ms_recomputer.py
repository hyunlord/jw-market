from __future__ import annotations

from .market_kpi_calculator import calculate_ml_kpi_extras
from .mart_metric_reader import fetch_metric_rows


def _first_present(*values):
    return next((value for value in values if value is not None), None)


def recompute_ms_pct(
    brand_raw_value: float,
    market_total_raw_value: float,
) -> float | None:
    if not market_total_raw_value or market_total_raw_value <= 0:
        return None
    return (float(brand_raw_value) / float(market_total_raw_value)) * 100.0


def get_kpi_extras_from_mart(
    brand_name: str,
    market_id: str,
    view: str,
    source: str,
    measure: str,
    db_conn,
) -> dict:
    rows = fetch_metric_rows(brand_name, market_id, view, source, measure, db_conn)
    kpi = calculate_ml_kpi_extras(rows) if rows else {}
    return {
        "ei": _first_present(kpi.get("ei"), kpi.get("target_ei")),
        "ei_basis": kpi.get("ei_basis"),
        "ei_period_years": kpi.get("ei_period_years"),
        "ei_note": kpi.get("ei_note"),
        "brand_cagr_5y_pct": _first_present(kpi.get("brand_cagr_5y_pct"), kpi.get("brand_cagr_pct")),
        "market_cagr_5y_pct": _first_present(kpi.get("market_cagr_5y_pct"), kpi.get("market_cagr_pct")),
        "momentum_score": _first_present(kpi.get("momentum_score"), kpi.get("target_momentum")),
        "target_rank": kpi.get("target_rank"),
        "total_brands_in_market": kpi.get("direct_competition_count"),
        "market_avg_ms_pct": kpi.get("market_avg_ms_pct"),
    }
