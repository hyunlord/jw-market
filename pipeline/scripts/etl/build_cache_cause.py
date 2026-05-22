#!/usr/bin/env python3
"""Build spec-aligned cache_cause from Phase 1 strategic marts."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from cache_build_common import (
    MEASURES_BY_SOURCE,
    api_source,
    decode_json,
    dump_payload,
    fetch_all,
    load_catalog,
    metric_recent,
    ml_to_strategy,
    mariadb_connect,
    parser,
    payload_size,
    safe_float,
    series_cagr,
    series_latest_number,
    source_list,
)


def latest_market_series_payload(series: dict[str, Any]) -> dict[str, Any]:
    return {
        "periods_unit": "월간",
        "periods_count": len(series or {}),
        "market_size_series": series or {},
    }


def top3_share(rows: list[dict[str, Any]]) -> float | None:
    shares = []
    for row in rows:
        recent = metric_recent(decode_json(row.get("metric_history")))
        shares.append(safe_float(recent.get("ms")))
    if not shares:
        return None
    return round(sum(sorted(shares, reverse=True)[:3]), 2)


def choose_target(rows: list[dict[str, Any]], fallback: dict[str, Any]) -> dict[str, Any]:
    for row in rows:
        if bool(row.get("is_target")):
            return row
    for row in rows:
        if bool(row.get("is_jw")):
            return row
    return fallback


def build_response(
    *,
    brand_row: dict[str, Any],
    market_row: dict[str, Any],
    sibling_rows: list[dict[str, Any]],
    view_type: str,
    market_id: str,
    source: str,
    measure: str,
    view_source_id: str,
    market_name: str | None,
    market_sources: list[str],
) -> dict[str, Any]:
    metric_history = decode_json(brand_row.get("metric_history"))
    extended = decode_json(brand_row.get("extended_metric_history"))
    recent = metric_recent(metric_history)
    ext_recent = metric_recent(extended)
    market_series = decode_json(market_row.get("market_size_series"))
    hhi_series = decode_json(market_row.get("hhi_series_5y") or market_row.get("hhi_series"))
    hhi_recent = series_latest_number(hhi_series)
    target = choose_target(sibling_rows, brand_row)
    target_recent = metric_recent(decode_json(target.get("metric_history")))
    target_ext = metric_recent(decode_json(target.get("extended_metric_history")))

    return {
        "brand": brand_row["brand_name"],
        "brand_key": brand_row["brand_key"],
        "market_id": market_id,
        "view": view_type,
        "source": source,
        "measure": measure,
        "unit_label": brand_row.get("unit_label"),
        "data": {
            "kpi": {
                "market_size_recent": series_latest_number(market_series),
                "market_cagr_5y_pct": series_cagr(market_series),
                "top3_share_pct": top3_share(sibling_rows),
                "hhi_recent": hhi_recent,
                "direct_competition_count": len({r.get("brand_key") for r in sibling_rows}),
                "target_brand": target.get("brand_name"),
                "target_company": target.get("company_name") or ("JW중외제약" if target.get("is_jw") else None),
                "target_ei": safe_float(target_ext.get("ei")),
                "target_momentum": safe_float(target_ext.get("momentum") or target_recent.get("mom")),
                "target_rank": target_recent.get("rank"),
                "target_share_pct": safe_float(target_recent.get("ms")),
                "brand_value_recent": safe_float(recent.get("raw_value")),
                "brand_share_pct": safe_float(recent.get("ms")),
            },
            "sources_data": {
                **latest_market_series_payload(market_series),
                "periods_unit": "월간" if brand_row["source"] == "ubist" else "분기",
            },
            "ei_ms_matrix": decode_json(market_row.get("ei_ms_matrix")),
            "growth_contribution": decode_json(market_row.get("growth_contribution")),
            "level_top5_trend": decode_json(market_row.get("level_top5_trend")),
            "target_customer_competition": decode_json(market_row.get("target_customer_competition")),
            "brand_ranking": decode_json(market_row.get("brand_ranking_stacked")),
            "company_ranking": decode_json(market_row.get("company_ranking_stacked")),
            "company_concentration_trend": decode_json(market_row.get("company_concentration_trend")),
        },
        "market_meta": {
            "market_name": market_name,
            "view_source_id": view_source_id,
            "atc_count": None,
            "nhi_type": None,
            "sources": market_sources,
            "source_label": source,
            "is_dual_source": len(market_sources) == 2,
            "measures": list(MEASURES_BY_SOURCE.get(brand_row["source"], ())),
            "is_jw": bool(brand_row.get("is_jw")),
            "is_target": bool(brand_row.get("is_target")),
        },
    }


def make_sibling_map(rows: list[dict[str, Any]], market_key: str) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row[market_key], row["source"], row["measure"])].append(row)
    return grouped


def main() -> None:
    args = parser(__doc__).parse_args()
    ml_market = load_catalog("ml_market").set_index("ml_id", drop=False)
    cd_market = load_catalog("cd_market").rename(columns={"cd_id": "cd_market_id"}).set_index("cd_market_id", drop=False)

    ml_market_rows = {
        (r["ml_id"], r["source"], r["measure"]): r for r in fetch_all("SELECT * FROM mart_strategic_ml_market_metric")
    }
    cd_market_rows = {
        (r["cd_market_id"], r["source"], r["measure"]): r for r in fetch_all("SELECT * FROM mart_strategic_cd_market_metric")
    }
    ml_brand_rows = fetch_all("SELECT * FROM mart_strategic_ml_brand_metric")
    cd_brand_rows = fetch_all("SELECT * FROM mart_strategic_cd_brand_metric")
    ml_siblings = make_sibling_map(ml_brand_rows, "ml_id")
    cd_siblings = make_sibling_map(cd_brand_rows, "cd_market_id")

    columns = ["brand", "view_type", "source", "measure", "market_id", "response_json", "payload_size"]
    placeholders = ", ".join(["%s"] * len(columns))
    names = ", ".join(f"`{c}`" for c in columns)
    sql = f"REPLACE INTO `cache_cause` ({names}) VALUES ({placeholders})"
    inserted = 0
    conn = mariadb_connect()
    cur = conn.cursor()
    for row in ml_brand_rows:
        market = ml_market.loc[row["ml_id"]].to_dict() if row["ml_id"] in ml_market.index else {}
        market_id = ml_to_strategy(row["ml_id"])
        source = api_source(row["source"])
        response = build_response(
            brand_row=row,
            market_row=ml_market_rows.get((row["ml_id"], row["source"], row["measure"]), {}),
            sibling_rows=ml_siblings[(row["ml_id"], row["source"], row["measure"])],
            view_type="market_landscape",
            market_id=market_id,
            source=source,
            measure=row["measure"],
            view_source_id=row["ml_id"],
            market_name=market.get("name"),
            market_sources=source_list(market.get("data_source")),
        )
        out = {
            "brand": row["brand_name"],
            "view_type": "market_landscape",
            "source": source,
            "measure": row["measure"],
            "market_id": market_id,
            "response_json": dump_payload(response),
            "payload_size": payload_size(response),
        }
        cur.execute(sql, tuple(out[col] for col in columns))
        inserted += 1
        if args.verbose and inserted % 1000 == 0:
            print(f"inserted cache_cause rows={inserted}", flush=True)

    for row in cd_brand_rows:
        cd = cd_market.loc[row["cd_market_id"]].to_dict() if row["cd_market_id"] in cd_market.index else {}
        ml_id = cd.get("ml_id") or row.get("ml_id")
        ml = ml_market.loc[ml_id].to_dict() if ml_id in ml_market.index else {}
        market_id = ml_to_strategy(ml_id)
        source = api_source(row["source"])
        response = build_response(
            brand_row=row,
            market_row=cd_market_rows.get((row["cd_market_id"], row["source"], row["measure"]), {}),
            sibling_rows=cd_siblings[(row["cd_market_id"], row["source"], row["measure"])],
            view_type="competitive_dynamics",
            market_id=market_id,
            source=source,
            measure=row["measure"],
            view_source_id=row["cd_market_id"],
            market_name=cd.get("name") or ml.get("name"),
            market_sources=source_list(cd.get("data_source") or ml.get("data_source")),
        )
        out = {
            "brand": row["brand_name"],
            "view_type": "competitive_dynamics",
            "source": source,
            "measure": row["measure"],
            "market_id": market_id,
            "response_json": dump_payload(response),
            "payload_size": payload_size(response),
        }
        cur.execute(sql, tuple(out[col] for col in columns))
        inserted += 1
        if args.verbose and inserted % 1000 == 0:
            print(f"inserted cache_cause rows={inserted}", flush=True)
    cur.close()
    conn.close()
    if args.verbose:
        print(f"cache_cause rows={inserted} ml_rows={len(ml_brand_rows)} cd_rows={len(cd_brand_rows)}")


if __name__ == "__main__":
    main()
