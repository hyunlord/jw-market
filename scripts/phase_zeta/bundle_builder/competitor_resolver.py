from __future__ import annotations

import json

from .catalog_db_loader import source_public_to_db
from .mat_computer import compute_mat_12m_absolute, find_latest_actual_period
from .ms_recomputer import get_kpi_extras_from_cache_cause, get_ms_from_cache_cause, recompute_ms_pct


def _json_load(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


def _latest_metric_point(metric_history: dict) -> tuple[str | None, dict]:
    latest = find_latest_actual_period(metric_history)
    if not latest:
        return None, {}
    return latest, metric_history.get(latest) or {}


def _market_size_for(
    db_conn,
    market_id: str,
    source: str,
    measure: str,
    period: str | None,
    view: str = "market_landscape",
) -> float | None:
    if not period:
        return None
    table, id_col = (
        ("mart_strategic_cd_market_metric", "cd_market_id")
        if view == "competitive_dynamics"
        else ("mart_strategic_ml_market_metric", "ml_id")
    )
    with db_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT market_size_series
            FROM {table}
            WHERE {id_col} = %s AND source = %s AND measure = %s
            LIMIT 1
            """,
            (market_id, source_public_to_db(source), measure),
        )
        row = cur.fetchone()
    series = _json_load(row.get("market_size_series") if row else None)
    value = series.get(period)
    return float(value) if value is not None else None


def resolve_market_top5_competitors(
    brand_name: str,
    ml_id: str,
    cd_id: str | None,
    source: str,
    db_conn,
) -> dict:
    db_source = source_public_to_db(source)
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT brand_name, is_jw, metric_history
            FROM mart_strategic_ml_brand_metric
            WHERE ml_id = %s
              AND source = %s
              AND measure = 'sales'
            """,
            (ml_id, db_source),
        )
        rows = cur.fetchall()

    candidates = []
    for row in rows:
        if row["brand_name"] == brand_name:
            continue
        history = _json_load(row["metric_history"])
        period, point = _latest_metric_point(history)
        raw_value = point.get("raw_value")
        if raw_value is None:
            continue
        market_total = _market_size_for(db_conn, ml_id, source, "sales", period)
        candidates.append(
            {
                "rank_in_market": int(point.get("rank") or 0),
                "brand_name": row["brand_name"],
                "is_jw": bool(row.get("is_jw")),
                "latest_period": period,
                "raw_value": float(raw_value),
                "ms_pct": recompute_ms_pct(float(raw_value), market_total),
            }
        )

    candidates.sort(key=lambda item: (-item["raw_value"], item["brand_name"]))
    top = candidates[:5]
    for idx, item in enumerate(top, start=1):
        if not item.get("rank_in_market"):
            item["rank_in_market"] = idx
    return {
        "source": source.upper(),
        "market_id_for_ranking": ml_id,
        "ranking_basis": "sales",
        "top_competitors": top,
    }


def resolve_view_top5_competitors(
    brand_name: str,
    ml_id: str,
    cd_id: str | None,
    view: str,
    source: str,
    measure: str,
    db_conn,
) -> dict:
    table, id_col = _metric_table(view)
    market_id = cd_id if view == "competitive_dynamics" else ml_id
    if not market_id:
        return {
            "source": source.upper(),
            "market_id_for_ranking": None,
            "ranking_basis": measure,
            "top_competitors": [],
        }

    db_source = source_public_to_db(source)
    with db_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT brand_name, is_jw, metric_history
            FROM {table}
            WHERE {id_col} = %s
              AND source = %s
              AND measure = %s
            """,
            (market_id, db_source, measure),
        )
        rows = cur.fetchall()

    candidates = []
    for row in rows:
        if row["brand_name"] == brand_name:
            continue
        history = _json_load(row["metric_history"])
        period, point = _latest_metric_point(history)
        raw_value = point.get("raw_value")
        if raw_value is None:
            continue
        market_total = _market_size_for(db_conn, market_id, source, measure, period, view)
        candidates.append(
            {
                "rank_in_market": int(point.get("rank") or 0),
                "brand_name": row["brand_name"],
                "is_jw": bool(row.get("is_jw")),
                "latest_period": period,
                "raw_value": float(raw_value),
                "ms_pct": recompute_ms_pct(float(raw_value), market_total),
            }
        )

    candidates.sort(key=lambda item: (-item["raw_value"], item["brand_name"]))
    top = candidates[:5]
    for idx, item in enumerate(top, start=1):
        if not item.get("rank_in_market"):
            item["rank_in_market"] = idx
    return {
        "source": source.upper(),
        "market_id_for_ranking": market_id,
        "ranking_basis": measure,
        "top_competitors": top,
    }


def _metric_table(view: str) -> tuple[str, str]:
    if view == "competitive_dynamics":
        return "mart_strategic_cd_brand_metric", "cd_market_id"
    return "mart_strategic_ml_brand_metric", "ml_id"


def get_competitor_history_for_view(
    competitor_brand_name: str,
    ml_id: str,
    cd_id: str | None,
    view: str,
    source: str,
    measure: str,
    snapshot_at: str,
    config,
    db_conn,
) -> dict:
    table, id_col = _metric_table(view)
    market_id = cd_id if view == "competitive_dynamics" else ml_id
    if not market_id:
        return {"history": {}, "kpi_extras": {}}
    with db_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT metric_history, extended_metric_history, is_jw
            FROM {table}
            WHERE {id_col} = %s
              AND brand_name = %s
              AND source = %s
              AND measure = %s
            LIMIT 1
            """,
            (market_id, competitor_brand_name, source_public_to_db(source), measure),
        )
        row = cur.fetchone()
    if not row:
        return {"history": {}, "kpi_extras": {}, "is_jw": False}

    metric_history = _json_load(row["metric_history"])
    latest_period, point = _latest_metric_point(metric_history)
    history = {}
    if latest_period and point:
        raw_value = point.get("raw_value")
        market_total = _market_size_for(db_conn, market_id, source, measure, latest_period, view)
        history[latest_period] = {
            "raw_value": raw_value,
            "ms_pct": recompute_ms_pct(raw_value, market_total) if raw_value is not None else None,
            "mom_pct": point.get("mom"),
            "qoq_pct": point.get("qoq"),
            "yoy_pct": point.get("yoy"),
            "rank": point.get("rank"),
        }
    extras = get_kpi_extras_from_cache_cause(competitor_brand_name, view, source, measure, db_conn)
    return {"history": history, "kpi_extras": extras, "is_jw": bool(row.get("is_jw"))}
