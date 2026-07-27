from __future__ import annotations

import json

from .cache_cause_key import cache_market_id
from .catalog_db_loader import load_market_from_catalog, source_public_to_db
from .competitor_resolver import get_competitor_history_for_view
from .market_kpi_calculator import calculate_ml_kpi_extras
from .mart_metric_reader import fetch_ml_metric_rows, ml_view_exists, use_cache_free_ml_kpi
from .mat_computer import compute_mat_12m_absolute, find_latest_actual_period
from .ms_recomputer import get_kpi_extras_from_cache_cause, recompute_ms_pct

VIEW_SHORT = {"market_landscape": "ML", "competitive_dynamics": "CD"}
PERIOD_UNIT = {"UBIST": "월간", "IQVIA": "분기"}


def _json_load(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


def _shift_month(yyyymm: str, delta: int) -> str:
    year, month = [int(part) for part in yyyymm.split("-")]
    month += delta
    while month <= 0:
        year -= 1
        month += 12
    while month > 12:
        year += 1
        month -= 12
    return f"{year:04d}-{month:02d}"


def _is_month_period(period: str | None) -> bool:
    if not period:
        return False
    parts = period.split("-")
    return len(parts) == 2 and parts[1].isdigit()


def _last_n_history(series: dict, latest: str | None, months: int) -> dict:
    if not series or not latest:
        return {}
    if not _is_month_period(latest):
        keys = [key for key in sorted(series) if key <= latest]
        selected = keys[-months:]
        return {key: series[key] for key in selected}
    start = _shift_month(latest, -(months - 1))
    return {key: series[key] for key in sorted(series) if start <= key <= latest}


def _mat_absolute(metric_history: dict, latest: str | None) -> dict:
    if not latest:
        return {"latest_period": None, "value": None, "raw_value_12m": None, "growth_yoy_pct": None, "missing_months": []}
    if _is_month_period(latest):
        return compute_mat_12m_absolute(metric_history, latest)
    keys = [key for key in sorted(metric_history) if key <= latest][-4:]
    total = 0.0
    missing = []
    for key in keys:
        point = metric_history.get(key) or {}
        raw_value = point.get("raw_value")
        if raw_value is None:
            missing.append(key)
        else:
            total += float(raw_value)
    latest_point = metric_history.get(latest) or {}
    return {
        "latest_period": latest,
        "value": total,
        "raw_value_12m": total,
        "growth_yoy_pct": latest_point.get("mat"),
        "missing_months": missing,
    }


def _market_tables(view: str) -> tuple[str, str, str]:
    if view == "competitive_dynamics":
        return "mart_strategic_cd_brand_metric", "mart_strategic_cd_market_metric", "cd_market_id"
    return "mart_strategic_ml_brand_metric", "mart_strategic_ml_market_metric", "ml_id"


def _cache_exists(
    brand_name: str,
    view: str,
    source: str,
    measure: str,
    db_conn,
    cache_market: str | None = None,
) -> bool:
    """Probe cache_cause on its full primary key.

    ``cache_market`` is the ``market_id`` column value, i.e. the strategy id, not
    the internal ml/cd id — see ``cache_cause_key``. Matching on four of five key
    columns previously let a brand's other market answer the probe.
    """

    if cache_market is None:
        raise ValueError("cache_market is required to probe cache_cause by primary key")
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM cache_cause
            WHERE brand = %s AND view_type = %s AND source = %s AND measure = %s
              AND market_id = %s
            LIMIT 1
            """,
            (brand_name, view, source.upper(), measure, cache_market),
        )
        return cur.fetchone() is not None


def _view_exists(
    brand_name: str,
    market_id: str,
    view: str,
    source: str,
    measure: str,
    config,
    db_conn,
    cache_market: str | None = None,
) -> bool:
    if view == "market_landscape" and use_cache_free_ml_kpi(config):
        return ml_view_exists(brand_name, market_id, source, measure, db_conn)
    return _cache_exists(brand_name, view, source, measure, db_conn, cache_market)


def _fetch_brand_metric(brand_name: str, market_id: str, view: str, source: str, measure: str, db_conn) -> dict | None:
    brand_table, _market_table, id_col = _market_tables(view)
    with db_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT brand_name, source, measure, unit_label, metric_history,
                   extended_metric_history, raw_value_history, computed_at
            FROM {brand_table}
            WHERE {id_col} = %s
              AND brand_name = %s
              AND source = %s
              AND measure = %s
            LIMIT 1
            """,
            (market_id, brand_name, source_public_to_db(source), measure),
        )
        return cur.fetchone()


def _fetch_market_metric(market_id: str, view: str, source: str, measure: str, db_conn) -> dict | None:
    _brand_table, market_table, id_col = _market_tables(view)
    name_col = "cd_market_name" if view == "competitive_dynamics" else "ml_name"
    with db_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {id_col} AS market_id, {name_col} AS market_name, source, measure,
                   unit_label, market_size_series, hhi_series_5y, brand_ranking_stacked,
                   target_customer_competition, computed_at
            FROM {market_table}
            WHERE {id_col} = %s AND source = %s AND measure = %s
            LIMIT 1
            """,
            (market_id, source_public_to_db(source), measure),
        )
        return cur.fetchone()


def _metric_history_points(metric_history: dict, market_size_history: dict, latest: str | None, months: int) -> dict:
    selected = _last_n_history(metric_history, latest, months)
    result = {}
    for period, point in selected.items():
        raw_value = point.get("raw_value") if isinstance(point, dict) else None
        market_total = market_size_history.get(period)
        result[period] = {
            "raw_value": raw_value,
            "ms_pct": recompute_ms_pct(raw_value, market_total) if raw_value is not None else None,
            "mom_pct": point.get("mom") if isinstance(point, dict) else None,
            "qoq_pct": point.get("qoq") if isinstance(point, dict) else None,
            "yoy_pct": point.get("yoy") if isinstance(point, dict) else None,
            "mat_yoy_pct": point.get("mat") if isinstance(point, dict) else None,
            "growth_abs": point.get("growth_abs") if isinstance(point, dict) else None,
            "rank": point.get("rank") if isinstance(point, dict) else None,
        }
    return result


def _latest_channel_top5(ranking_series: dict, target_brand: str, latest: str | None) -> list:
    rows = ranking_series.get(latest) if latest else []
    if not isinstance(rows, list):
        return []
    top = rows[:5]
    if target_brand not in {row.get("brand") for row in top}:
        target = next((row for row in rows if row.get("brand") == target_brand), None)
        if target:
            top = top[:4] + [target]
    return [
        {
            "rank": row.get("rank"),
            "brand": row.get("brand"),
            "is_target": row.get("brand") == target_brand,
            "raw_value": row.get("raw_value"),
            "ms_pct": row.get("ms"),
        }
        for row in top
    ]


def build_market_view(
    brand_name: str,
    ml_id: str,
    cd_id: str | None,
    view: str,
    source: str,
    measure: str,
    snapshot_at: str,
    config,
    db_conn,
    competitors_top5_cache: dict,
) -> dict | None:
    market_id = cd_id if view == "competitive_dynamics" else ml_id
    if not market_id:
        return None
    # The exact market this view is about, expressed in the cache's own key
    # space. mart reads below already use `market_id`; the cache reads now use
    # the same identity instead of taking whatever row came first.
    cache_market = cache_market_id(view, ml_id, cd_id)
    if not _view_exists(
        brand_name, market_id, view, source, measure, config, db_conn, cache_market
    ):
        return None

    brand_row = _fetch_brand_metric(brand_name, market_id, view, source, measure, db_conn)
    market_row = _fetch_market_metric(market_id, view, source, measure, db_conn)
    if not brand_row or not market_row:
        return None

    metric_history = _json_load(brand_row["metric_history"])
    market_size_series = _json_load(market_row["market_size_series"])
    hhi_series = _json_load(market_row["hhi_series_5y"])
    latest_period = find_latest_actual_period(metric_history)
    market_size_history = _last_n_history(market_size_series, latest_period, config.lookback_months)
    hhi_history = _last_n_history(hhi_series, latest_period, config.lookback_months)
    target_history = _metric_history_points(metric_history, market_size_series, latest_period, config.lookback_months)

    mat = _mat_absolute(metric_history, latest_period) if config.include_mat_12m_absolute else {
        "latest_period": latest_period,
        "value": None,
        "raw_value_12m": None,
        "growth_yoy_pct": None,
        "missing_months": [],
    }
    market = load_market_from_catalog(ml_id, db_conn)
    top5 = competitors_top5_cache.get((view, source.upper(), measure)) or {}
    competitors = []
    for item in top5.get("top_competitors", []):
        competitor_metric = get_competitor_history_for_view(
            item["brand_name"],
            ml_id,
            cd_id,
            view,
            source,
            measure,
            snapshot_at,
            config,
            db_conn,
        )
        competitors.append(
            {
                "rank_in_market": item.get("rank_in_market"),
                "brand_name": item["brand_name"],
                "is_jw": bool(item.get("is_jw") or competitor_metric.get("is_jw")),
                "history": competitor_metric.get("history") or {},
                "kpi_extras": competitor_metric.get("kpi_extras") or {},
            }
        )

    ranking_series = _json_load(market_row.get("brand_ranking_stacked"))
    kpi_extras = get_kpi_extras_from_cache_cause(
        brand_name, view, source, measure, db_conn, cache_market
    )
    if view == "market_landscape" and use_cache_free_ml_kpi(config):
        ml_rows = fetch_ml_metric_rows(brand_name, ml_id, source, measure, db_conn)
        if ml_rows:
            kpi_extras = calculate_ml_kpi_extras(ml_rows)
    return {
        "view_id": f"{VIEW_SHORT[view]}.{source.upper()}.{measure}",
        "view": view,
        "source": source.upper(),
        "measure": measure,
        "market_meta": {
            "market_id_internal": market_id,
            "market_id_spec": f"strategy_{(ml_id or market_id).split('_')[-1]}",
            "market_name": market_row.get("market_name") or market.get("ml_name"),
            "market_label_kor": market.get("market_label_kor") or market.get("ml_name"),
            "atc4_code": market.get("atc4_code"),
            "unit_label": brand_row.get("unit_label") or market_row.get("unit_label"),
            "period_unit": PERIOD_UNIT.get(source.upper(), "월간"),
        },
        "market_size": {"history": market_size_history, "hhi_5y": hhi_history},
        "target_brand_metric": {
            "history": target_history,
            "mat_12m_absolute": mat,
            "kpi_extras": kpi_extras,
        },
        "competitors_top5": competitors,
        "channel_breakdown": {
            "channel": config.channel_filter,
            "top5_in_channel": _latest_channel_top5(ranking_series, brand_name, latest_period),
        },
    }
