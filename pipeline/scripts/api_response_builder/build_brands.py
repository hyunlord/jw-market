from __future__ import annotations

from typing import Any

from pipeline.scripts.api.db import connect

from .utils import BRAND_MARTS


def _brand_queries(view_type: str | None, source: str | None) -> tuple[str, list[Any]]:
    selected_views = [view_type] if view_type else list(BRAND_MARTS)
    parts: list[str] = []
    params: list[Any] = []
    for view in selected_views:
        cfg = BRAND_MARTS[view]
        mart = cfg["brand_mart"]
        market_col = cfg["market_id_col"]
        market_expr = f"{market_col} AS market_id"
        atc_expr = "atc4_code" if view == "general" else "NULL AS atc4_code"
        is_jw_expr = "FALSE AS is_jw" if view == "general" else "is_jw"
        where = ""
        if source:
            where = "WHERE source = %s"
            params.append(source)
        parts.append(
            f"""
            SELECT
              brand_key,
              brand_name,
              source,
              measure,
              {is_jw_expr},
              JSON_UNQUOTE(JSON_EXTRACT(by_dimension, '$.company')) AS company,
              JSON_UNQUOTE(JSON_EXTRACT(by_dimension, '$.catalog_status')) AS catalog_status,
              {atc_expr},
              {market_expr},
              '{view}' AS view_type,
              '{mart}' AS source_mart
            FROM {mart}
            {where}
            """
        )
    return "\nUNION ALL\n".join(parts), params


def build_brands_response(view_type: str | None = None, source: str | None = None) -> dict[str, Any]:
    """Build the precomputed response for ``GET /api/brands``.

    The v0.9 mock returned only JW demo brands. The six-mart cache response is
    intentionally broader: it exposes every cached brand with the views,
    sources, and measures where that brand is available.
    """

    sql, params = _brand_queries(view_type, source)
    brands: dict[str, dict[str, Any]] = {}
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur.fetchall():
                key = row["brand_key"]
                item = brands.setdefault(
                    key,
                    {
                        "brand_key": key,
                        "brand_name": row["brand_name"],
                        "is_jw": False,
                        "company": row.get("company"),
                        "catalog_status": row.get("catalog_status"),
                        "available_views": set(),
                        "available_sources": set(),
                        "available_measures": set(),
                        "atc4_codes": set(),
                        "market_ids": set(),
                        "source_marts": set(),
                    },
                )
                item["is_jw"] = bool(item["is_jw"] or row.get("is_jw"))
                item["company"] = item["company"] or row.get("company")
                if row.get("catalog_status") == "matched":
                    item["catalog_status"] = "matched"
                item["available_views"].add(row["view_type"])
                item["available_sources"].add(row["source"])
                item["available_measures"].add(row["measure"])
                if row.get("atc4_code"):
                    item["atc4_codes"].add(row["atc4_code"])
                if row.get("market_id"):
                    item["market_ids"].add(row["market_id"])
                item["source_marts"].add(row["source_mart"])

    brand_list = []
    for item in brands.values():
        brand_list.append(
            {
                **item,
                "available_views": sorted(item["available_views"]),
                "available_sources": sorted(item["available_sources"]),
                "available_measures": sorted(item["available_measures"]),
                "atc4_codes": sorted(item["atc4_codes"]),
                "market_ids": sorted(item["market_ids"]),
                "source_marts": sorted(item["source_marts"]),
            }
        )
    brand_list.sort(key=lambda item: (not item["is_jw"], item["brand_name"] or item["brand_key"]))

    return {
        "brands": brand_list,
        "total_count": len(brand_list),
        "filters_applied": {
            "view": view_type or "all",
            "source": source or "all",
        },
    }
