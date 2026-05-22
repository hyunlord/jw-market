#!/usr/bin/env python3
"""Build spec-aligned cache_market_status with separated UBIST/IQVIA KPI."""

from __future__ import annotations

from collections import defaultdict
import sys
from typing import Any

from cache_build_common import (
    API_TO_SOURCE,
    CANONICAL_25,
    display_ukrw,
    dump_payload,
    fetch_all,
    first_pair,
    load_catalog,
    metric_first,
    metric_recent,
    ml_to_strategy,
    numeric_mean,
    parser,
    payload_size,
    period_key,
    replace_rows,
    series_latest_number,
    source_list,
    decode_json,
    safe_float,
    series_cagr,
    PROJECT_ROOT,
)

sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.scripts.api.metadata import BRAND_METADATA
from pipeline.scripts.api.services import MARKET_STATUS_COMPANY_BY_BRAND


BRAND_META_BY_NAME = {meta.brand: meta for meta in BRAND_METADATA}


def _history_number(item: object) -> float | None:
    if isinstance(item, dict):
        value = item.get("raw_value")
    else:
        value = item
    if value is None:
        return None
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def movement_pct_from_history(metric_history: dict | None) -> float | None:
    """Return latest-vs-previous movement pct from a period keyed history."""
    data = metric_history or {}
    if len(data) < 2:
        return None
    keys = sorted(data.keys(), key=period_key)
    previous = _history_number(data.get(keys[-2]))
    recent = _history_number(data.get(keys[-1]))
    if previous is None or recent is None or previous == 0:
        return None
    return (recent - previous) / previous * 100


def source_card_payload(row: dict) -> dict:
    history = decode_json(row.get("metric_history"))
    recent = metric_recent(history)
    movement = movement_pct_from_history(history)
    return {
        "value_recent": safe_float(recent.get("raw_value")),
        "ms_recent_pct": safe_float(recent.get("ms")),
        "gr_mom_pct": movement if row.get("source") == "ubist" else safe_float(recent.get("mom")),
        "gr_qoq_pct": movement if row.get("source") == "iqvia_nsa" else safe_float(recent.get("qoq")),
        "gr_yoy_pct": safe_float(recent.get("yoy")),
        "gr_yoy_mat_pct": safe_float(recent.get("yoy_mat")),
        "gr_yoy_ym_pct": safe_float(recent.get("yoy_ym")),
        "ms_change_yoy_pct": safe_float(recent.get("ms_change_yoy")),
        "unit_label": row.get("unit_label"),
        "measure": row.get("measure"),
    }


def _valid_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def _period_first(metric_history: dict | None) -> str | None:
    period, _ = first_pair(metric_history)
    return str(period) if period is not None else None


def _source_label(sources: list[str]) -> str:
    if not sources:
        return ""
    return " + ".join(sources)


def _ordered_sources(sources: list[str]) -> list[str]:
    order = {"UBIST": 0, "IQVIA": 1}
    return sorted(dict.fromkeys(sources), key=lambda source: order.get(source, 99))


def _ratio_to_pct(value: Any) -> float | None:
    if value is None:
        return None
    return round(safe_float(value) * 100, 4)


def _market_definition_label(atc_codes: list[str]) -> str:
    return "1 ATC" if len(atc_codes) == 1 else f"{len(atc_codes)} ATCs"


def _catalog_company(catalog_row: dict) -> str | None:
    return _valid_text(catalog_row.get("판매사")) or _valid_text(catalog_row.get("제조사"))


def _direct_competition_count(strategic_brand: Any, cd_id: Any) -> int:
    cd = _valid_text(cd_id)
    if not cd or strategic_brand is None or "cd_id" not in strategic_brand:
        return 0
    return int((strategic_brand["cd_id"].astype(str) == cd).sum())


def _first_existing(*values: Any) -> Any:
    for value in values:
        if isinstance(value, list):
            if value:
                return value
        elif _valid_text(value) is not None:
            return value
    return None


def build_brand_card(
    brand_row: dict,
    market: dict,
    sales_rows: list[dict],
    market_rows: dict,
    strategic_brand: Any,
) -> dict:
    preferred = next((r for r in sales_rows if r["source"] == "ubist"), None) or (sales_rows[0] if sales_rows else {})
    metric_history = decode_json(preferred.get("metric_history"))
    extended = decode_json(preferred.get("extended_metric_history"))
    recent = metric_recent(metric_history)
    first = metric_first(metric_history)
    ext_recent = metric_recent(extended)
    market_metric = market_rows.get((preferred.get("ml_id"), preferred.get("source"), "sales"), {})
    market_series = decode_json(market_metric.get("market_size_series"))
    market_recent = series_latest_number(market_series)
    sources_data = {
        "UBIST" if row["source"] == "ubist" else "IQVIA": source_card_payload(row)
        for row in sales_rows
    }
    default_source = "UBIST" if "UBIST" in sources_data else (next(iter(sources_data.keys()), None))
    brand_name = brand_row["brand"]
    meta = BRAND_META_BY_NAME.get(brand_name)
    meta_sources = list(meta.sources) if meta else []
    sources = _ordered_sources(meta_sources or brand_row["sources"])
    atc_codes = list(meta.atc_codes) if meta else []
    brand_cagr = _ratio_to_pct(ext_recent.get("cagr_5y"))
    if brand_cagr is None:
        brand_cagr = 0.0
    market_cagr = series_cagr(market_series)
    excess_growth = round(brand_cagr - market_cagr, 4) if brand_cagr is not None and market_cagr is not None else None
    company = (
        MARKET_STATUS_COMPANY_BY_BRAND.get(brand_name)
        or _catalog_company(brand_row.get("catalog_row", {}))
        or "JW중외제약"
    )
    market_name = _first_existing(meta.market_name if meta else None, market.get("name"))
    market_name_short = _first_existing(meta.market_name_short if meta else None, market_name, brand_name)
    atc_desc = _first_existing(meta.atc_desc if meta else None, "")
    market_label_kor = _first_existing(meta.market_label_kor if meta else None, market_name_short)
    mkt_team = _valid_text(meta.mkt_team if meta else None)
    nhi_type = _valid_text(brand_row.get("catalog_row", {}).get("nhi_type")) or "NHI"
    direct_competition_count = _direct_competition_count(strategic_brand, brand_row.get("catalog_row", {}).get("cd_id"))

    return {
        "rank": int(meta.rank) if meta else safe_float(recent.get("rank")),
        "brand": brand_name,
        "brand_key": brand_row["brand_key"],
        "company": company,
        "market_id": brand_row["market_id"],
        "market_name": market_name,
        "market_name_short": market_name_short,
        "market_label_kor": market_label_kor,
        "mkt_team": mkt_team,
        "atc_codes": atc_codes,
        "atc_desc": atc_desc,
        "nhi_type": nhi_type,
        "is_jw": True,
        "is_target": brand_row["is_target"],
        "sources": sources,
        "front": {
            "value_recent": safe_float(recent.get("raw_value")),
            "ms_recent_pct": safe_float(recent.get("ms")),
            "gr_mom_pct": source_card_payload(preferred).get("gr_mom_pct") if preferred else None,
            "gr_qoq_pct": safe_float(recent.get("qoq")),
            "gr_yoy_pct": safe_float(recent.get("yoy")),
            "gr_yoy_mat_pct": safe_float(recent.get("yoy_mat")),
            "gr_yoy_ym_pct": safe_float(recent.get("yoy_ym")),
            "ms_change_yoy_pct": safe_float(recent.get("ms_change_yoy")),
            "sources_data": sources_data,
            "default_source": default_source,
        },
        "back": {
            "cagr_5y_pct": brand_cagr,
            "sales_first_period_krw": safe_float(first.get("raw_value")),
            "ms_first_period_pct": safe_float(first.get("ms")),
            "period_first": _period_first(metric_history),
        },
        "back_extended": {
            "market_size_recent": market_recent,
            "market_cagr_5y_pct": market_cagr,
            "brand_cagr_5y_pct": brand_cagr,
            "excess_growth_pct": excess_growth,
            "source_label": _source_label(sources),
            "is_dual_source": len(sources) > 1,
            "sources": sources,
            "market_definition_label": _market_definition_label(atc_codes),
            "market_definition_full": ", ".join(atc_codes),
            "atc_count": len(atc_codes),
            "direct_competition_count": direct_competition_count,
            "market_label_kor": market_label_kor,
        },
        "source_cards": [
            {
                "source": "UBIST" if row["source"] == "ubist" else "IQVIA",
                "measure": row["measure"],
                "unit_label": row["unit_label"],
                "value_recent": safe_float(metric_recent(decode_json(row["metric_history"])).get("raw_value")),
                "ms_recent_pct": safe_float(metric_recent(decode_json(row["metric_history"])).get("ms")),
            }
            for row in sales_rows
        ],
    }


def history_period_totals(rows: list[dict]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        for period, item in (decode_json(row.get("metric_history")) or {}).items():
            value = _history_number(item)
            if value is not None:
                totals[str(period)] += value
    return totals


def cagr_from_source_rows(source: str, rows: list[dict]) -> float:
    totals = history_period_totals(rows)
    if len(totals) < 2:
        return 0.0
    periods = sorted(totals.keys(), key=period_key)
    first = totals[periods[0]]
    last = totals[periods[-1]]
    if first <= 0 or last <= 0:
        return 0.0
    periods_per_year = 12.0 if source == "UBIST" else 4.0
    years = (len(periods) - 1) / periods_per_year
    if years <= 0:
        return 0.0
    return round(((last / first) ** (1 / years) - 1) * 100, 4)


def period_recent_from_rows(rows: list[dict]) -> str | None:
    periods = set()
    for row in rows:
        periods.update(str(period) for period in (decode_json(row.get("metric_history")) or {}).keys())
    if not periods:
        return None
    return sorted(periods, key=period_key)[-1]


def build_kpi(source: str, rows: list[dict]) -> dict:
    source_rows = [r for r in rows if r["source"] == API_TO_SOURCE[source] and r["measure"] == "sales"]
    latest_values = []
    ms_values = []
    movement_values = []
    for row in source_rows:
        history = decode_json(row["metric_history"])
        recent = metric_recent(history)
        latest_values.append(safe_float(recent.get("raw_value")))
        ms_values.append(safe_float(recent.get("ms")))
        movement = movement_pct_from_history(history)
        if movement is not None:
            movement_values.append(movement)
    total = sum(latest_values)
    rising = sum(1 for value in movement_values if value >= 0)
    declining = sum(1 for value in movement_values if value < 0)
    return {
        "total_sales_recent_krw": total,
        "avg_ms_per_brand_pct": numeric_mean(ms_values),
        "sales_up_count": rising,
        "sales_down_count": declining,
        "avg_cagr_5y_pct": cagr_from_source_rows(source, source_rows),
        "period_recent": period_recent_from_rows(source_rows),
        "brand_count": rising + declining,
    }


def main() -> None:
    args = parser(__doc__).parse_args()
    strategic_brand = load_catalog("strategic_brand")
    ml_market = load_catalog("ml_market").set_index("ml_id", drop=False)

    jw = strategic_brand[strategic_brand["is_jw"].astype(bool)].copy()
    actual = set(jw["canonical_name"].fillna(jw["name"]).astype(str))
    if actual != CANONICAL_25:
        raise SystemExit(f"canonical brand mismatch: missing={sorted(CANONICAL_25 - actual)}, extra={sorted(actual - CANONICAL_25)}")

    mart_rows = fetch_all(
        """
        SELECT *
        FROM mart_strategic_ml_brand_metric
        WHERE is_jw = 1
        """
    )
    sales_by_brand: dict[str, list[dict]] = defaultdict(list)
    for row in mart_rows:
        if row["measure"] == "sales":
            sales_by_brand[row["brand_name"]].append(row)

    market_rows = {
        (r["ml_id"], r["source"], r["measure"]): r
        for r in fetch_all("SELECT * FROM mart_strategic_ml_market_metric")
    }

    cards = []
    for _, row in jw.sort_values(["ml_id", "is_target", "brand_id"], ascending=[True, False, True]).iterrows():
        ml_id = str(row["ml_id"])
        market = ml_market.loc[ml_id].to_dict() if ml_id in ml_market.index else {}
        brand = str(row.get("canonical_name") or row.get("name"))
        cards.append(
            build_brand_card(
                {
                    "brand": brand,
                    "brand_key": str(row.get("brand_key") or brand),
                    "market_id": ml_to_strategy(ml_id),
                    "is_target": bool(row.get("is_target")),
                    "sources": source_list(market.get("data_source")),
                    "catalog_row": row.to_dict(),
                },
                market,
                sales_by_brand.get(brand, []),
                market_rows,
                strategic_brand,
            )
        )

    payload = {
        "kpi_summary": {
            "UBIST": build_kpi("UBIST", mart_rows),
            "IQVIA": build_kpi("IQVIA", mart_rows),
        },
        "brand_cards": cards,
    }
    replace_rows(
        "cache_market_status",
        ["query_key", "response_json", "payload_size"],
        [{"query_key": "default", "response_json": dump_payload(payload), "payload_size": payload_size(payload)}],
    )
    if args.verbose:
        print(f"cache_market_status default brand_cards={len(cards)} payload_size={payload_size(payload)}")


if __name__ == "__main__":
    main()
