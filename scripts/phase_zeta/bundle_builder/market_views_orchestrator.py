from __future__ import annotations

from .competitor_resolver import resolve_market_top5_competitors
from .market_view_builder import build_market_view

VIEW_ORDER = {
    "market_landscape": "ML",
    "competitive_dynamics": "CD",
}


def _view_sort_key(view: dict) -> tuple:
    source_order = {"UBIST": 0, "IQVIA": 1}
    view_order = {"ML": 0, "CD": 1}
    measure_order = {"sales": 0, "volume": 1, "unit": 2, "dosage_unit": 3, "counting_unit": 4}
    short, source, measure = view["view_id"].split(".", 2)
    return (view_order.get(short, 99), source_order.get(source, 99), measure_order.get(measure, 99), measure)


def build_market_views(
    brand_context: dict,
    snapshot_at: str,
    config,
    db_conn,
) -> list[dict]:
    ml_id = brand_context.get("ml_id")
    cd_id = brand_context.get("cd_id")
    available_sources = set(brand_context.get("available_sources") or [])
    competitors_top5_cache = {}
    for source in available_sources:
        if source not in competitors_top5_cache:
            competitors_top5_cache[source] = resolve_market_top5_competitors(
                brand_context["name"], ml_id, cd_id, source, db_conn
            )

    views = []
    for matrix in config.market.views_matrix:
        for source_cfg in matrix.sources:
            source = source_cfg.source.upper()
            if source not in available_sources:
                continue
            for measure in source_cfg.measures:
                item = build_market_view(
                    brand_context["name"],
                    ml_id,
                    cd_id,
                    matrix.view,
                    source,
                    measure,
                    snapshot_at,
                    config.market,
                    db_conn,
                    competitors_top5_cache,
                )
                if item:
                    views.append(item)
    views.sort(key=_view_sort_key)
    return views
