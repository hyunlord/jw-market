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


def _period_year(period: str) -> int | None:
    try:
        return int(str(period)[:4])
    except (TypeError, ValueError):
        return None


def _row_value(row: dict[str, Any]) -> float:
    return safe_float(row.get("raw_value") or row.get("value")) or 0.0


def _row_share(row: dict[str, Any]) -> float:
    return safe_float(row.get("ms") or row.get("ms_pct") or row.get("share_pct")) or 0.0


def _normalize_rank_row(row: dict[str, Any], *, label_key: str, target_name: str | None) -> dict[str, Any]:
    name = row.get(label_key) or row.get("brand") or row.get("brand_key") or row.get("company") or row.get("name")
    is_target = bool(target_name and name == target_name)
    return {
        label_key: name,
        "brand": name if label_key == "brand" else row.get("brand"),
        "company": row.get("company") or row.get("company_name"),
        "is_target": is_target,
        "is_jw": bool(row.get("is_jw")) or is_target,
        "is_others": False,
        "value": _row_value(row),
        "rank": row.get("rank"),
        "ms_pct": _row_share(row),
    }


def _stacked_ranking(
    period_map: dict[str, Any],
    *,
    label_key: str,
    target_name: str | None,
    catalog_members: list[dict[str, Any]] | None = None,
    top_n: int = 5,
) -> dict[str, Any]:
    by_year: dict[int, tuple[str, list[dict[str, Any]]]] = {}
    for period, rows in sorted((period_map or {}).items()):
        year = _period_year(period)
        if year is None or not isinstance(rows, list):
            continue
        by_year[year] = (str(period), rows)

    years = sorted(by_year.keys())[-5:]
    yearly = []
    for year in years:
        _, rows = by_year[year]
        normalized = [_normalize_rank_row(row, label_key=label_key, target_name=target_name) for row in rows]
        existing = {row.get(label_key) for row in normalized}
        if catalog_members:
            for member in catalog_members:
                name = member.get("name")
                if name and name not in existing:
                    normalized.append(
                        {
                            label_key: name,
                            "brand": name if label_key == "brand" else None,
                            "company": member.get("company"),
                            "is_target": bool(target_name and name == target_name),
                            "is_jw": bool(member.get("is_jw")),
                            "is_others": False,
                            "value": 0.0,
                            "rank": None,
                            "ms_pct": 0.0,
                        }
                    )
                    existing.add(name)

        target = next((row for row in normalized if row["is_target"]), None)
        target_id = row_identity(target, label_key)
        competitors = []
        for candidate in sorted(normalized, key=lambda item: item["value"], reverse=True):
            if row_identity(candidate, label_key) != target_id:
                competitors.append(candidate)
        selected = ([target] if target else []) + competitors[:top_n]
        selected_ids = {row_identity(row, label_key) for row in selected}
        others = [row for row in normalized if row_identity(row, label_key) not in selected_ids]
        if others:
            selected.append(
                {
                    label_key: "기타",
                    "brand": "기타" if label_key == "brand" else None,
                    "company": "기타" if label_key == "company" else None,
                    "is_target": False,
                    "is_jw": False,
                    "is_others": True,
                    "value": sum(row["value"] for row in others),
                    "rank": None,
                    "ms_pct": sum(row["ms_pct"] for row in others),
                }
            )
        for index, row in enumerate(selected, start=1):
            row["rank"] = row["rank"] or index
        yearly.append({"year": year, "rankings": selected})
    return {"years": years, "yearly": yearly}


def row_identity(row: dict[str, Any] | None, label_key: str) -> str | None:
    if not row:
        return None
    return str(row.get(label_key) or row.get("brand") or row.get("company") or row.get("name"))


def _analysis_levels(level_top5: dict[str, Any], source: str) -> dict[str, Any]:
    levels = list((level_top5 or {}).keys())
    data = {}
    for level, period_map in (level_top5 or {}).items():
        latest_period = None
        latest = []
        if isinstance(period_map, dict):
            for period, rows in sorted(period_map.items(), reverse=True):
                if isinstance(rows, list) and rows:
                    latest_period = period
                    latest = rows
                    break
        total = sum(_row_value(row) for row in latest)
        segments = [
            {
                "name": row.get("label") or row.get("level") or row.get("name") or row.get(level),
                "rank": row.get("rank") or idx,
                "recent_share_pct": row.get("ms") or row.get("share_pct"),
                "series_pct": [(_row_share(row) if latest_period else 0.0)],
                "value_series": [_row_value(row)],
            }
            for idx, row in enumerate(latest, start=1)
        ]
        if total and not any(segment.get("recent_share_pct") for segment in segments):
            for segment in segments:
                segment["recent_share_pct"] = round((segment["value_series"][-1] / total) * 100, 4)
                segment["series_pct"] = [segment["recent_share_pct"]]
        data[level] = {"segments": segments, "by_channel": {"전체": segments}}
    return {
        "levels": levels,
        "channels": ["전체"] if levels else [],
        "period_unit": "monthly" if source == "UBIST" else "quarterly",
        "data": data,
    }


def _series_from_period_map(period_map: dict[str, Any]) -> tuple[list[float], list[float]]:
    values: list[float] = []
    shares: list[float] = []
    for _, item in sorted((period_map or {}).items()):
        if isinstance(item, dict):
            values.append(_row_value(item))
            shares.append(_row_share(item))
        else:
            values.append(safe_float(item) or 0.0)
            shares.append(0.0)
    if values and not any(shares):
        total = sum(values)
        shares = [round(value / total * 100, 4) if total else 0.0 for value in values]
    return values, shares


def _normalize_analysis_levels(raw: Any, fallback_level_top5: dict[str, Any], source: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or "levels" in raw:
        normalized = raw if isinstance(raw, dict) and "levels" in raw else _analysis_levels(fallback_level_top5, source)
    else:
        levels = list(raw.keys())
        data = {}
        for level, segment_map in raw.items():
            segments = []
            if isinstance(segment_map, dict):
                ranked = []
                for name, period_map in segment_map.items():
                    if not isinstance(period_map, dict):
                        continue
                    values, shares = _series_from_period_map(period_map)
                    recent_value = values[-1] if values else 0.0
                    recent_share = shares[-1] if shares else 0.0
                    ranked.append((recent_value, name, values, shares, recent_share))
                for idx, (_, name, values, shares, recent_share) in enumerate(sorted(ranked, reverse=True)[:8], start=1):
                    segments.append(
                        {
                            "name": name,
                            "rank": idx,
                            "recent_share_pct": recent_share,
                            "series_pct": shares,
                            "value_series": values,
                        }
                    )
            data[level] = {"segments": segments, "by_channel": {"전체": segments}}
        normalized = {
            "levels": levels,
            "channels": ["전체"] if levels else [],
            "period_unit": "monthly" if source == "UBIST" else "quarterly",
            "data": data,
        }

    for level in normalized.get("levels", []):
        level_data = normalized.setdefault("data", {}).setdefault(level, {})
        segments = level_data.get("segments") or []
        if not level_data.get("by_channel"):
            level_data["by_channel"] = {"전체": segments}
    if not normalized.get("channels") and normalized.get("levels"):
        normalized["channels"] = ["전체"]
    return normalized


def _growth_ms_matrix(ei_rows: Any) -> dict[str, Any]:
    rows = ei_rows if isinstance(ei_rows, list) else []
    output = []
    for row in rows:
        share = safe_float(row.get("ms") or row.get("share_pct"))
        contribution = safe_float(row.get("momentum_score") or row.get("growth_contribution") or row.get("contribution_pct"))
        output.append(
            {
                "brand": row.get("brand") or row.get("brand_key"),
                "company": row.get("company"),
                "is_target": bool(row.get("is_target")),
                "is_jw": bool(row.get("is_jw")),
                "share_pct": share,
                "contribution_pct": contribution,
                "growth_contribution": contribution,
                "value_recent": row.get("raw_value") or row.get("value"),
            }
        )
    shares = [row["share_pct"] for row in output if row["share_pct"] is not None]
    return {
        "data": output,
        "ms_avg_pct": round(sum(shares) / len(shares), 4) if shares else None,
        "share_avg_pct": round(sum(shares) / len(shares), 4) if shares else None,
    }


def _catalog_members_for_market(strategic_brand: Any, view_source_id: str) -> list[dict[str, Any]]:
    if strategic_brand is None or not view_source_id.startswith("ml_"):
        return []
    sub = strategic_brand[strategic_brand["ml_id"].astype(str) == view_source_id]
    members = []
    for _, row in sub.iterrows():
        name = str(row.get("canonical_name") or row.get("name") or "")
        if name:
            members.append({"name": name, "is_jw": bool(row.get("is_jw")), "company": row.get("판매사")})
    return members


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
    strategic_brand: Any = None,
) -> dict[str, Any]:
    metric_history = decode_json(brand_row.get("metric_history"))
    extended = decode_json(brand_row.get("extended_metric_history"))
    recent = metric_recent(metric_history)
    ext_recent = metric_recent(extended)
    market_series = decode_json(market_row.get("market_size_series"))
    hhi_series = decode_json(market_row.get("hhi_series_5y") or market_row.get("hhi_series"))
    hhi_recent = series_latest_number(hhi_series)
    source_api = source
    target = choose_target(sibling_rows, brand_row)
    target_recent = metric_recent(decode_json(target.get("metric_history")))
    target_ext = metric_recent(decode_json(target.get("extended_metric_history")))

    brand_ranking = decode_json(market_row.get("brand_ranking_stacked"))
    company_ranking = decode_json(market_row.get("company_ranking_stacked"))
    level_top5 = decode_json(market_row.get("level_top5_trend"))
    ei_ms = decode_json(market_row.get("ei_ms_matrix"))
    catalog_members = _catalog_members_for_market(strategic_brand, view_source_id)
    brand_ranking_stacked = _stacked_ranking(
        brand_ranking,
        label_key="brand",
        target_name=brand_row.get("brand_name"),
        catalog_members=catalog_members,
    )
    company_ranking_stacked = _stacked_ranking(company_ranking, label_key="company", target_name=target.get("company_name"))
    direct_competition_count = max(
        len({r.get("brand_key") for r in sibling_rows if r.get("brand_key")}),
        len({member["name"] for member in catalog_members if member.get("name")}),
    )

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
                "direct_competition_count": direct_competition_count,
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
                "hhi_series_5y": hhi_series,
                "hhi_recent": hhi_recent,
                "cagr_5y_pct": series_cagr(market_series),
            },
            "ei_ms_matrix": ei_ms,
            "growth_contribution_ms_matrix": decode_json(market_row.get("growth_contribution_ms_matrix")) or _growth_ms_matrix(ei_ms),
            "growth_contribution": decode_json(market_row.get("growth_contribution")),
            "level_top5_trend": level_top5,
            "target_customer_competition": decode_json(market_row.get("target_customer_competition")),
            "brand_ranking_stacked": brand_ranking_stacked,
            "company_ranking_stacked": company_ranking_stacked,
            "company_concentration_trend": decode_json(market_row.get("company_concentration_trend")),
            "analysis_levels": _normalize_analysis_levels(
                decode_json(market_row.get("analysis_levels")),
                level_top5,
                source_api,
            ),
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
    strategic_brand = load_catalog("strategic_brand")
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
            strategic_brand=strategic_brand,
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
            strategic_brand=strategic_brand,
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
