#!/usr/bin/env python3
"""Read-only audit for response_store cache redundancy and size.

Phase 16-G-4-Side-CacheSizeAudit.

This script only reads response_store. It does not mutate DB rows or code.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pipeline.scripts.api.db import connect


MARKET_CHART_KEYS = [
    "market_size_series",
    "hhi_series_5y",
    "brand_ranking_stacked",
    "company_ranking_stacked",
    "company_concentration_trend",
    "ei_ms_matrix",
    "growth_contribution_ms_matrix",
    "growth_contribution",
    "analysis_levels",
    "level_top5_trend",
    "target_customer_competition",
]

DIRECT_DATA_DUP_KEYS = [
    "kpi",
    "ei_ms_matrix",
    "growth_contribution",
    "growth_contribution_ms_matrix",
    "target_customer_competition",
    "company_concentration_trend",
    "level_top5_trend",
    "brand_ranking_stacked",
    "company_ranking_stacked",
    "analysis_levels",
]

SOURCES_DATA_DUP_KEYS = [
    "metric_history",
    "extended_metric_history",
    "raw_value_history",
    "channel_data",
    "specialty_data",
    "market_size_series",
    "hhi_series_5y",
]

BRAND_SPECIFIC_KEYS = [
    "metric_history",
    "extended_metric_history",
    "raw_value_history",
    "channel_data",
    "specialty_data",
    "by_dimension",
    "overlay_data",
    "cd_overlay",
    "kpi",
]


def json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def parse_response(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def pct(part: float, total: float) -> float:
    return round(part / total * 100, 2) if total else 0.0


def summarize(values: list[float]) -> dict[str, float]:
    if not values:
        return {"count": 0, "avg": 0.0, "min": 0.0, "max": 0.0}
    return {
        "count": len(values),
        "avg": round(statistics.mean(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }


def analyze_response(response: dict[str, Any]) -> dict[str, Any]:
    total = json_size(response)
    data = response.get("data") or {}
    sources_data = data.get("sources_data") or {}

    direct_dup = sum(json_size(data.get(k)) for k in DIRECT_DATA_DUP_KEYS if k in response and k in data)
    sources_dup = sum(json_size(sources_data.get(k)) for k in SOURCES_DATA_DUP_KEYS if k in response and k in sources_data)
    market_embed = sum(json_size(response.get(k)) for k in MARKET_CHART_KEYS if k in response)
    brand_specific = sum(json_size(response.get(k)) for k in BRAND_SPECIFIC_KEYS if k in response)
    data_size = json_size(data)

    return {
        "total_bytes": total,
        "direct_data_duplicate_bytes": direct_dup,
        "sources_data_duplicate_bytes": sources_dup,
        "total_duplicate_bytes": direct_dup + sources_dup,
        "duplicate_ratio_pct": pct(direct_dup + sources_dup, total),
        "market_chart_embed_bytes": market_embed,
        "market_chart_embed_ratio_pct": pct(market_embed, total),
        "brand_specific_bytes": brand_specific,
        "brand_specific_ratio_pct": pct(brand_specific, total),
        "data_object_bytes": data_size,
        "data_object_ratio_pct": pct(data_size, total),
    }


def analyze_deep_analysis_response(response: dict[str, Any]) -> dict[str, Any]:
    base_total = json_size(response)
    data = response.get("data") or {}
    cause = data.get("cause") or {}
    cause_stats = analyze_response(cause) if isinstance(cause, dict) and cause else {}
    cause_bytes = json_size(cause) if cause else 0
    return {
        "total_bytes": base_total,
        "embedded_cause_bytes": cause_bytes,
        "embedded_cause_ratio_pct": pct(cause_bytes, base_total),
        "embedded_cause_duplicate_bytes": cause_stats.get("total_duplicate_bytes", 0),
        "embedded_cause_market_chart_bytes": cause_stats.get("market_chart_embed_bytes", 0),
        "embedded_cause_duplicate_ratio_pct": pct(cause_stats.get("total_duplicate_bytes", 0), base_total),
        "embedded_cause_market_chart_ratio_pct": pct(cause_stats.get("market_chart_embed_bytes", 0), base_total),
    }


def fetch_endpoint_view_size(cur: Any) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT endpoint, COALESCE(view_type, 'null') AS view_type,
               COUNT(*) AS row_count,
               SUM(size_bytes) AS total_bytes,
               AVG(size_bytes) AS avg_bytes,
               MIN(size_bytes) AS min_bytes,
               MAX(size_bytes) AS max_bytes
        FROM response_store
        GROUP BY endpoint, view_type
        ORDER BY total_bytes DESC
        """
    )
    rows = list(cur.fetchall())
    for row in rows:
        row["avg_bytes"] = float(row["avg_bytes"] or 0)
    return rows


def sample_response_stats(cur: Any, endpoint: str, view_type: str | None, limit: int) -> dict[str, Any]:
    params: list[Any] = [endpoint]
    sql = "SELECT response_json FROM response_store WHERE endpoint = %s"
    if view_type:
        sql += " AND view_type = %s"
        params.append(view_type)
    sql += " ORDER BY size_bytes DESC LIMIT %s"
    params.append(limit)

    cur.execute(sql, params)
    analyzer = analyze_deep_analysis_response if endpoint == "deep-analysis" else analyze_response
    stats = [analyzer(parse_response(row["response_json"])) for row in cur.fetchall()]
    return {
        "samples": stats,
        "summary": {
            "total_kb": summarize([s["total_bytes"] / 1024 for s in stats]),
            "duplicate_kb": summarize([s.get("total_duplicate_bytes", 0) / 1024 for s in stats]),
            "duplicate_ratio_pct": summarize([s.get("duplicate_ratio_pct", 0) for s in stats]),
            "market_embed_kb": summarize([s.get("market_chart_embed_bytes", 0) / 1024 for s in stats]),
            "market_embed_ratio_pct": summarize([s.get("market_chart_embed_ratio_pct", 0) for s in stats]),
            "brand_specific_kb": summarize([s.get("brand_specific_bytes", 0) / 1024 for s in stats]),
            "embedded_cause_kb": summarize([s.get("embedded_cause_bytes", 0) / 1024 for s in stats]),
            "embedded_cause_ratio_pct": summarize([s.get("embedded_cause_ratio_pct", 0) for s in stats]),
            "embedded_cause_duplicate_kb": summarize([s.get("embedded_cause_duplicate_bytes", 0) / 1024 for s in stats]),
            "embedded_cause_market_chart_kb": summarize([s.get("embedded_cause_market_chart_bytes", 0) / 1024 for s in stats]),
        },
    }


def top_market_duplication(cur: Any, limit: int) -> dict[str, Any]:
    cur.execute(
        """
        SELECT view_type, market_id, COUNT(*) AS brand_count
        FROM response_store
        WHERE endpoint = 'cause'
        GROUP BY view_type, market_id
        ORDER BY brand_count DESC
        LIMIT %s
        """,
        (limit,),
    )
    groups = list(cur.fetchall())

    details = []
    total_duplicate_bytes = 0
    for group in groups:
        cur.execute(
            """
            SELECT response_json
            FROM response_store
            WHERE endpoint = 'cause' AND view_type = %s AND market_id = %s
            ORDER BY size_bytes DESC
            LIMIT 1
            """,
            (group["view_type"], group["market_id"]),
        )
        sample = cur.fetchone()
        if not sample:
            continue
        response = parse_response(sample["response_json"])
        market_size = sum(json_size(response.get(k)) for k in MARKET_CHART_KEYS if k in response)
        duplicate_bytes = max(int(group["brand_count"]) - 1, 0) * market_size
        total_duplicate_bytes += duplicate_bytes
        details.append(
            {
                "view_type": group["view_type"],
                "market_id": group["market_id"],
                "brand_count": int(group["brand_count"]),
                "market_chart_bytes_per_brand": market_size,
                "duplicate_bytes": duplicate_bytes,
            }
        )

    return {
        "limit": limit,
        "total_duplicate_bytes": total_duplicate_bytes,
        "details": details,
    }


def current_counts(cur: Any) -> dict[str, Any]:
    cur.execute("SELECT COUNT(*) AS row_count, SUM(size_bytes) AS total_bytes FROM response_store")
    total = cur.fetchone()
    cur.execute(
        """
        SELECT endpoint, COUNT(*) AS row_count, SUM(size_bytes) AS total_bytes
        FROM response_store
        GROUP BY endpoint
        ORDER BY endpoint
        """
    )
    return {"total": total, "by_endpoint": list(cur.fetchall())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--top-market-limit", type=int, default=20)
    args = parser.parse_args()

    with connect() as conn:
        with conn.cursor() as cur:
            result = {
                "static_duplicate_keys": {
                    "direct_data_duplicates": DIRECT_DATA_DUP_KEYS,
                    "sources_data_duplicates": SOURCES_DATA_DUP_KEYS,
                    "market_chart_keys": MARKET_CHART_KEYS,
                },
                "response_store_counts": current_counts(cur),
                "endpoint_view_size": fetch_endpoint_view_size(cur),
                "cause_sample_general": sample_response_stats(cur, "cause", "general", args.sample_limit),
                "cause_sample_strategic_ml": sample_response_stats(cur, "cause", "strategic_ml", args.sample_limit),
                "cause_sample_strategic_cd": sample_response_stats(cur, "cause", "strategic_cd", args.sample_limit),
                "deep_analysis_sample": sample_response_stats(cur, "deep-analysis", None, args.sample_limit),
                "top_market_duplication": top_market_duplication(cur, args.top_market_limit),
            }

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
