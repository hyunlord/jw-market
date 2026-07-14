from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response

from pipeline.scripts.api import db
from pipeline.scripts.api.brand_source_options import brand_source_options
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.deep_analysis_context import (
    public_source_labels,
    resolve_deep_analysis_context,
)
from pipeline.scripts.utils.brand_name_normalize import compact_brand_name


router = APIRouter()


def _default_brands() -> list[dict]:
    row = db.fetch_one(
        """
        SELECT response_json
        FROM cache_brands
        WHERE query_key = 'default'
        LIMIT 1
        """
    )
    if not row:
        raise HTTPException(status_code=404, detail={"error": "cache_not_found", "cache": "cache_brands"})
    payload = compose_cached_json(row["response_json"])
    if not isinstance(payload, list):
        raise HTTPException(status_code=500, detail={"error": "invalid_cache_payload", "cache": "cache_brands"})
    return payload


def _search_brand_candidates(query: str) -> list[dict[str, Any]]:
    needle = compact_brand_name(query)
    if not needle:
        return []
    rows = db.fetch_all(
        """
        SELECT brand_key, MAX(brand_name) AS brand_name, raw_value_history, source
        FROM (
          SELECT brand_key, brand_name, atc4_code, raw_value_history, source
          FROM mart_general_brand_metric
          WHERE measure = 'sales' AND brand_key = %s
          UNION ALL
          SELECT brand_key, brand_name, atc4_code, raw_value_history, source
          FROM mart_general_brand_metric
          WHERE measure = 'sales' AND brand_name = %s AND brand_key <> %s
        ) AS exact_matches
        GROUP BY brand_key, atc4_code, raw_value_history, source
        """,
        (needle, needle, needle),
    )
    candidates: dict[str, dict[str, Any]] = {}
    seen_rows: set[tuple[str, str, str, str]] = set()
    for row in rows:
        brand_key = str(row.get("brand_key") or "").strip()
        if not brand_key:
            continue
        identity = (
            brand_key,
            str(row.get("brand_name") or ""),
            str(row.get("raw_value_history") or ""),
            str(row.get("source") or ""),
        )
        if identity in seen_rows:
            continue
        seen_rows.add(identity)
        item = candidates.setdefault(
            brand_key,
            {
                "brand_key": brand_key,
                "brand_name": str(row.get("brand_name") or brand_key).strip(),
                "source_values": {},
            },
        )
        source = str(row.get("source") or "")
        item["source_values"][source] = item["source_values"].get(source, 0.0) + _latest_value(
            row.get("raw_value_history")
        )
    for item in candidates.values():
        item["market_size"] = max(item.pop("source_values").values(), default=0.0)
    return list(candidates.values())


def _latest_value(value: object) -> float:
    try:
        payload = json.loads(value) if isinstance(value, str) else value
        if not isinstance(payload, dict) or not payload:
            return 0.0
        period = max(str(key) for key in payload)
        return float(payload.get(period) or 0.0)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0.0


def _context_options_for_brand(brand: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    return brand_source_options(brand, resolver=resolve_deep_analysis_context)


def _search_results(query: str, limit: int) -> tuple[list[dict[str, Any]], int]:
    candidates = sorted(
        _search_brand_candidates(query),
        key=lambda item: (-float(item.get("market_size") or 0.0), str(item.get("brand_name") or "")),
    )
    results: list[dict[str, Any]] = []
    jw_targets = {str(item.get("brand") or "") for item in _default_brands()}
    for candidate in candidates[:limit]:
        brand = str(candidate.get("brand_name") or candidate.get("brand_key") or "")
        contexts, general_sources, strategic_sources = _context_options_for_brand(brand)
        sources = public_source_labels((*general_sources, *strategic_sources))
        result: dict[str, Any] = {
            "brand": brand,
            "sources": sources,
            "strategic_sources": strategic_sources,
            "general_sources": general_sources,
            "contexts": contexts,
            "is_jw_target": brand in jw_targets,
        }
        if not contexts:
            result["context_reason"] = "analysis_context_not_available"
        results.append(result)
    return results, len(candidates)


@router.get("/api/brands")
def list_brands(
    response: Response,
    q: str | None = Query(None, description="brand 이름 완전 일치 검색"),
    query: str | None = Query(None, description="BFF 호환 brand 검색어"),
    market_id: str | None = Query(None, description="strategy_NNN market id"),
    limit: int = Query(20, ge=1, le=50, description="검색 결과 상한(최대 50)"),
) -> list[dict]:
    if q and query and compact_brand_name(q).casefold() != compact_brand_name(query).casefold():
        raise HTTPException(status_code=422, detail={"error": "conflicting_search_query"})
    search_query = q or query
    if search_query:
        results, total = _search_results(search_query, limit)
        response.headers["X-Has-More"] = str(total > limit).lower()
        response.headers["X-Total-Matches"] = str(total)
        response.headers["X-Result-Limit"] = str(limit)
        return results

    brands = _default_brands()
    if market_id:
        brands = [brand for brand in brands if brand.get("market_id") == market_id]
    return brands
