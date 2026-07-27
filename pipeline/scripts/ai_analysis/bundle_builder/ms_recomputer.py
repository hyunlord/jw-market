from __future__ import annotations

import json
from typing import Optional


def _json_load(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


def recompute_ms_pct(
    brand_raw_value: float,
    market_total_raw_value: float,
) -> float | None:
    if not market_total_raw_value or market_total_raw_value <= 0:
        return None
    return (float(brand_raw_value) / float(market_total_raw_value)) * 100.0


def _cache_row(
    brand_name: str,
    view: str,
    source: str,
    measure: str,
    db_conn,
    market_id: str | None = None,
) -> dict | None:
    """Read one cache_cause row by its full primary key.

    ``market_id`` completes the PK. Without it the query matched on four of the
    five key columns and ``LIMIT 1`` returned whichever market the storage engine
    offered first, so a brand in two markets could be answered with the other
    market's KPIs. A miss must stay a miss: never fall back to a different
    market's row.
    """

    if market_id is None:
        raise ValueError("market_id is required to read cache_cause by primary key")
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT response_json
            FROM cache_cause
            WHERE brand = %s
              AND view_type = %s
              AND source = %s
              AND measure = %s
              AND market_id = %s
            LIMIT 1
            """,
            (brand_name, view, source.upper(), measure, market_id),
        )
        row = cur.fetchone()
    if not row:
        return None
    return _json_load(row.get("response_json"))


def get_ms_from_cache_cause(
    brand_name: str,
    view: str,
    source: str,
    measure: str,
    db_conn,
    market_id: str | None = None,
) -> Optional[float]:
    obj = _cache_row(brand_name, view, source, measure, db_conn, market_id)
    if not obj:
        return None
    kpi = ((obj.get("data") or {}).get("kpi") or {})
    value = kpi.get("ms_pct")
    if value is None:
        value = kpi.get("target_share_pct") or kpi.get("brand_share_pct")
    return float(value) if value is not None else None


def get_kpi_extras_from_cache_cause(
    brand_name: str,
    view: str,
    source: str,
    measure: str,
    db_conn,
    market_id: str | None = None,
) -> dict:
    obj = _cache_row(brand_name, view, source, measure, db_conn, market_id)
    kpi = ((obj or {}).get("data") or {}).get("kpi") or {}
    return {
        "ei": kpi.get("ei") or kpi.get("target_ei"),
        "ei_basis": kpi.get("ei_basis"),
        "ei_period_years": kpi.get("ei_period_years"),
        "ei_note": kpi.get("ei_note"),
        "brand_cagr_5y_pct": kpi.get("brand_cagr_5y_pct") or kpi.get("brand_cagr_pct"),
        "market_cagr_5y_pct": kpi.get("market_cagr_5y_pct") or kpi.get("market_cagr_pct"),
        "momentum_score": kpi.get("momentum_score") or kpi.get("target_momentum"),
        "target_rank": kpi.get("target_rank"),
        "total_brands_in_market": kpi.get("direct_competition_count"),
        "market_avg_ms_pct": kpi.get("market_avg_ms_pct"),
    }
