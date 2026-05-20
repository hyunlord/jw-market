from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from pipeline.scripts.api import db
from pipeline.scripts.api.catalog import (
    DISPLAY_BRANDS,
    DisplayBrand,
    get_display_brand,
    validate_source_measure,
)
from pipeline.scripts.api.drivers import compute_drivers
from pipeline.scripts.api.utils import loads_json_maybe, now_iso, to_jsonable


FORM_BOUNDARY = re.compile(r"(?:$|\\s|정|캡슐|주|액|서방|시럽|현탁|구강|SR|CR|OD)", re.IGNORECASE)


@dataclass(frozen=True)
class BrandResolution:
    display: DisplayBrand
    brand_id: str
    brand_name: str
    period_yyyymm: str
    snapshot: dict[str, Any]


def latest_period() -> str:
    row = db.fetch_one("SELECT MAX(period_yyyymm) AS period FROM mart_core_brand_metric")
    if not row or not row["period"]:
        raise RuntimeError("mart_core_brand_metric has no rows")
    return str(row["period"])


def _brand_match(display_name: str, stored_name: str) -> bool:
    display = display_name.replace(" ", "").lower()
    stored = stored_name.replace(" ", "").lower()
    if stored == display:
        return True
    if not stored.startswith(display):
        return False
    remainder = stored[len(display) :]
    if not remainder:
        return True
    return bool(FORM_BOUNDARY.match(remainder))


def resolve_brand(brand_name: str) -> BrandResolution:
    display = get_display_brand(brand_name)
    if not display:
        raise HTTPException(status_code=404, detail=f"Brand not found: {brand_name}")

    rows = db.fetch_all(
        """
        SELECT brand_id, brand_name, period_yyyymm, market_share, rank_in_market,
               cagr_1y, cagr_3y, cagr_5y, ei_5y, momentum_score,
               growth_contribution, hhi, market_cagr_5y, raw_value
        FROM mart_core_brand_metric
        WHERE ml_id = %s
          AND is_jw = TRUE
          AND channel IS NULL
          AND specialty IS NULL
          AND period_yyyymm = (
            SELECT MAX(period_yyyymm)
            FROM mart_core_brand_metric
            WHERE ml_id = %s AND is_jw = TRUE AND channel IS NULL AND specialty IS NULL
          )
        ORDER BY raw_value DESC
        """,
        (display.ml_id, display.ml_id),
    )
    match_names = (display.brand_name, *display.layer3_aliases)
    candidates = [
        row
        for row in rows
        if any(_brand_match(match_name, str(row["brand_name"])) for match_name in match_names)
    ]
    if not candidates:
        candidates = [row for row in rows if str(row["brand_name"]).replace(" ", "").lower().startswith(display.brand_name.replace(" ", "").lower())]
    if not candidates:
        raise HTTPException(status_code=404, detail=f"Layer 3 brand row not found: {brand_name}")

    chosen = to_jsonable(candidates[0])
    return BrandResolution(
        display=display,
        brand_id=str(chosen["brand_id"]),
        brand_name=str(chosen["brand_name"]),
        period_yyyymm=str(chosen["period_yyyymm"]),
        snapshot=chosen,
    )


def latest_period_for_brand(brand_id: str) -> str:
    row = db.fetch_one(
        """
        SELECT MAX(period_yyyymm) AS period
        FROM mart_core_brand_metric
        WHERE brand_id = %s AND channel IS NULL AND specialty IS NULL
        """,
        (brand_id,),
    )
    if not row or not row["period"]:
        raise HTTPException(status_code=404, detail=f"No Layer 3 rows for brand_id={brand_id}")
    return str(row["period"])


def build_brands_response(include_snapshot: bool = False) -> dict[str, Any]:
    data: list[dict[str, Any]] = []
    for display in DISPLAY_BRANDS:
        item: dict[str, Any] = {
            "brand": display.brand_name,
            "market_id": display.market_id,
            "ml_id": display.ml_id,
            "market_name": None,
            "source_class": display.source_class,
            "sources": display.sources,
            "available_measures": display.available_measures,
            "cause_variants": display.cause_variants,
        }
        try:
            resolved = resolve_brand(display.brand_name)
            item["resolved_brand_id"] = resolved.brand_id
            item["resolved_brand_name"] = resolved.brand_name
            if include_snapshot:
                item["snapshot"] = resolved.snapshot
        except HTTPException:
            item["resolved_brand_id"] = None
            item["resolved_brand_name"] = None
            if include_snapshot:
                item["snapshot"] = None
        data.append(item)
    return {"data": data, "total": len(data), "generated_at": now_iso()}


def build_market_status_response(period: str | None = None, top_n: int = 10) -> dict[str, Any]:
    if period and period != "latest":
        rows = db.fetch_all(
            """
            SELECT ml_id, brand_id, brand_name, is_jw, market_share, rank_in_market,
                   cagr_5y, ei_5y, hhi, market_cagr_5y
            FROM mart_core_brand_metric
            WHERE period_yyyymm = %s AND channel IS NULL AND specialty IS NULL
            ORDER BY ml_id, rank_in_market
            """,
            (period,),
        )
    else:
        rows = db.fetch_all(
            """
            WITH latest AS (
              SELECT ml_id, MAX(period_yyyymm) AS period_yyyymm
              FROM mart_core_brand_metric
              WHERE channel IS NULL AND specialty IS NULL
              GROUP BY ml_id
            )
            SELECT m.ml_id, m.brand_id, m.brand_name, m.is_jw, m.period_yyyymm,
                   m.market_share, m.rank_in_market, m.cagr_5y, m.ei_5y,
                   m.hhi, m.market_cagr_5y
            FROM mart_core_brand_metric m
            JOIN latest l ON l.ml_id = m.ml_id AND l.period_yyyymm = m.period_yyyymm
            WHERE m.channel IS NULL AND m.specialty IS NULL
            ORDER BY m.ml_id, m.rank_in_market
            """
        )
        period = "latest"

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["ml_id"]), []).append(to_jsonable(row))

    markets: list[dict[str, Any]] = []
    for ml_id, group in sorted(grouped.items()):
        if not group:
            continue
        top = group[:top_n]
        markets.append(
            {
                "ml_id": ml_id,
                "ml_name": next((b.brand_name + " 시장" for b in DISPLAY_BRANDS if b.ml_id == ml_id), ml_id),
                "period_yyyymm": str(group[0].get("period_yyyymm") or period),
                "hhi": group[0].get("hhi"),
                "market_cagr_5y": group[0].get("market_cagr_5y"),
                "top_brands": [
                    {
                        "brand_id": item["brand_id"],
                        "brand_name": item["brand_name"],
                        "is_jw": bool(item["is_jw"]),
                        "market_share": item["market_share"],
                        "rank": item["rank_in_market"],
                        "cagr_5y": item["cagr_5y"],
                        "ei_5y": item["ei_5y"],
                    }
                    for item in top
                ],
                "total_brands": len(group),
            }
        )
    return {"period": str(period), "markets": markets, "total_markets": len(markets), "generated_at": now_iso()}


def _warnings_from_row(row: dict[str, Any]) -> list[str]:
    warnings = loads_json_maybe(row.get("warnings"))
    if isinstance(warnings, list):
        return [str(item) for item in warnings]
    return []


def _extended(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "metric_basis": "canonical_value",
        "cagr_1y": row.get("cagr_1y"),
        "cagr_3y": row.get("cagr_3y"),
        "cagr_5y": row.get("cagr_5y"),
        "ei_5y": row.get("ei_5y"),
        "momentum_score": row.get("momentum_score"),
        "growth_contribution": row.get("growth_contribution"),
    }


def _market_context(row: dict[str, Any]) -> dict[str, Any]:
    return {"hhi": row.get("hhi"), "market_cagr_5y": row.get("market_cagr_5y")}


def build_cause_response(
    brand_name: str,
    *,
    view: str,
    source: str | None,
    measure: str,
    period: str | None,
) -> dict[str, Any]:
    resolved = resolve_brand(brand_name)
    source = source or resolved.display.default_source
    is_valid, reason = validate_source_measure(resolved.display, source, measure)
    concrete_period = period or latest_period_for_brand(resolved.brand_id)

    if not is_valid:
        return {
            "brand": resolved.display.brand_name,
            "resolved_brand_id": resolved.brand_id,
            "resolved_brand_name": resolved.brand_name,
            "market_id": resolved.display.ml_id,
            "view": view,
            "source": source,
            "measure": measure,
            "unit_label": measure,
            "period_yyyymm": concrete_period,
            "summary": None,
            "monthly": [],
            "drivers": [],
            "market_context": {},
            "data": None,
            "reason": reason,
            "generated_at": now_iso(),
        }

    rows = db.fetch_all(
        """
        SELECT period_yyyymm, channel, specialty,
               market_share, mom, qoq, yoy, mat, growth_abs, rank_in_market,
               cagr_1y, cagr_3y, cagr_5y, ei_5y, momentum_score,
               growth_contribution, hhi, market_cagr_5y,
               JSON_EXTRACT(payload, '$.warnings') AS warnings
        FROM mart_core_brand_metric
        WHERE ml_id = %s
          AND brand_id = %s
          AND channel IS NULL AND specialty IS NULL
          AND period_yyyymm <= %s
        ORDER BY period_yyyymm
        """,
        (resolved.display.ml_id, resolved.brand_id, concrete_period),
    )
    rows = [to_jsonable(row) for row in rows]
    if not rows:
        raise HTTPException(status_code=404, detail=f"No cause rows for {brand_name}")
    summary_row = rows[-1]
    summary = {
        "market_share": summary_row.get("market_share"),
        "rank_in_market": summary_row.get("rank_in_market"),
        "mom": summary_row.get("mom"),
        "qoq": summary_row.get("qoq"),
        "yoy": summary_row.get("yoy"),
        "mat": summary_row.get("mat"),
        "growth_abs": summary_row.get("growth_abs"),
        "extended": _extended(summary_row),
        "market_context": _market_context(summary_row),
    }
    monthly = [
        {
            "period_yyyymm": row["period_yyyymm"],
            "market_share": row.get("market_share"),
            "mom": row.get("mom"),
            "qoq": row.get("qoq"),
            "yoy": row.get("yoy"),
            "mat": row.get("mat"),
            "growth_abs": row.get("growth_abs"),
            "rank_in_market": row.get("rank_in_market"),
            "extended": _extended(row),
            "market_context": _market_context(row),
            "warnings": _warnings_from_row(row),
        }
        for row in rows
    ]
    return {
        "brand": resolved.display.brand_name,
        "resolved_brand_id": resolved.brand_id,
        "resolved_brand_name": resolved.brand_name,
        "market_id": resolved.display.ml_id,
        "view": view,
        "source": source,
        "measure": measure,
        "unit_label": "KRW" if measure == "sales" else measure,
        "period_yyyymm": concrete_period,
        "summary": summary,
        "monthly": monthly,
        "drivers": compute_drivers(summary_row, view=view),
        "market_context": _market_context(summary_row),
        "data": {"metric_basis": "canonical_value"},
        "reason": None,
        "generated_at": now_iso(),
    }


def build_deep_analysis_response(brand_name: str, period: str | None = None) -> dict[str, Any]:
    resolved = resolve_brand(brand_name)
    concrete_period = period or latest_period_for_brand(resolved.brand_id)
    rows = db.fetch_all(
        """
        SELECT period_yyyymm, channel, specialty,
               market_share, rank_in_market,
               cagr_5y, ei_5y, momentum_score, growth_contribution,
               hhi, market_cagr_5y
        FROM mart_core_brand_metric
        WHERE ml_id = %s
          AND brand_id = %s
          AND channel IS NOT NULL
          AND specialty IS NOT NULL
          AND period_yyyymm = %s
        ORDER BY channel, specialty
        """,
        (resolved.display.ml_id, resolved.brand_id, concrete_period),
    )
    breakdown = [
        {
            "channel": str(row["channel"]),
            "specialty": str(row["specialty"]),
            "market_share": to_jsonable(row.get("market_share")),
            "rank": row.get("rank_in_market"),
            "cagr_5y": to_jsonable(row.get("cagr_5y")),
            "ei_5y": to_jsonable(row.get("ei_5y")),
            "momentum_score": to_jsonable(row.get("momentum_score")),
            "growth_contribution": to_jsonable(row.get("growth_contribution")),
            "hhi": to_jsonable(row.get("hhi")),
            "market_cagr_5y": to_jsonable(row.get("market_cagr_5y")),
        }
        for row in rows
    ]
    return {
        "brand": resolved.display.brand_name,
        "resolved_brand_id": resolved.brand_id,
        "resolved_brand_name": resolved.brand_name,
        "market_id": resolved.display.ml_id,
        "period_yyyymm": concrete_period,
        "breakdown": breakdown,
        "data": {"metric_basis": "canonical_value"},
        "generated_at": now_iso(),
    }
