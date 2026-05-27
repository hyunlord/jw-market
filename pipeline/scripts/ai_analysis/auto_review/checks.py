from __future__ import annotations

from typing import Any

from bundle_builder.catalog_db_loader import source_public_to_db


def _market_table(market_id: str | None, view: str | None) -> tuple[str, str] | tuple[None, None]:
    if market_id and market_id.startswith("cd_"):
        return "mart_strategic_cd_brand_metric", "cd_market_id"
    if market_id and market_id.startswith("ml_"):
        return "mart_strategic_ml_brand_metric", "ml_id"
    if view == "competitive_dynamics":
        return "mart_strategic_cd_brand_metric", "cd_market_id"
    if view == "market_landscape":
        return "mart_strategic_ml_brand_metric", "ml_id"
    return None, None


def _competitor_exists(db_conn: Any, table: str, id_col: str, market_id: str, source: str, measure: str, brand_name: str) -> bool:
    with db_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT 1
            FROM {table}
            WHERE {id_col} = %s
              AND source = %s
              AND measure = %s
              AND brand_name = %s
            LIMIT 1
            """,
            (market_id, source_public_to_db(source), measure, brand_name),
        )
        return cur.fetchone() is not None


def check_competitor_in_view_market(bundle: dict[str, Any], db_conn: Any) -> dict[str, Any]:
    """Check every competitor_top5 row belongs to the same view market.

    This is intentionally read-only and mirrors the Stage 3-B' auto-review
    contract: selected JW brand excluded top 5 competitors must come from the
    exact view market, source, and measure that produced the view.
    """
    failures: list[dict[str, Any]] = []
    checked = 0

    for view in bundle.get("market_views", []) or []:
        market_id = (view.get("market_meta", {}) or {}).get("market_id_internal")
        source = view.get("source")
        measure = view.get("measure")
        table, id_col = _market_table(market_id, view.get("view"))
        if not market_id or not source or not measure or not table or not id_col:
            failures.append(
                {
                    "view_id": view.get("view_id"),
                    "market_id": market_id,
                    "source": source,
                    "measure": measure,
                    "brand_name": None,
                    "reason": "missing view market/source/measure metadata",
                }
            )
            continue

        for competitor in view.get("competitors_top5", []) or []:
            brand_name = competitor.get("brand_name")
            if not brand_name:
                continue
            checked += 1
            if not _competitor_exists(db_conn, table, id_col, market_id, source, measure, brand_name):
                failures.append(
                    {
                        "view_id": view.get("view_id"),
                        "market_id": market_id,
                        "source": source,
                        "measure": measure,
                        "brand_name": brand_name,
                        "reason": "competitor not found in same view market/source/measure",
                    }
                )

    return {
        "check": "competitor_in_view_market",
        "passed": not failures,
        "checked": checked,
        "failures": failures,
    }
