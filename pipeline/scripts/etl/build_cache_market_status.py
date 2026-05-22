#!/usr/bin/env python3
"""Build spec-aligned cache_market_status with separated UBIST/IQVIA KPI."""

from __future__ import annotations

from collections import defaultdict

from cache_build_common import (
    API_TO_SOURCE,
    CANONICAL_25,
    display_ukrw,
    dump_payload,
    fetch_all,
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
)


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


def build_brand_card(brand_row: dict, market: dict, sales_rows: list[dict], market_rows: dict) -> dict:
    preferred = next((r for r in sales_rows if r["source"] == "ubist"), None) or (sales_rows[0] if sales_rows else {})
    metric_history = decode_json(preferred.get("metric_history"))
    extended = decode_json(preferred.get("extended_metric_history"))
    recent = metric_recent(metric_history)
    first = metric_first(metric_history)
    ext_recent = metric_recent(extended)
    market_metric = market_rows.get((preferred.get("ml_id"), preferred.get("source"), "sales"), {})
    market_series = decode_json(market_metric.get("market_size_series"))
    market_recent = series_latest_number(market_series)

    return {
        "brand": brand_row["brand"],
        "brand_key": brand_row["brand_key"],
        "market_id": brand_row["market_id"],
        "market_name": market.get("name"),
        "is_jw": True,
        "is_target": brand_row["is_target"],
        "sources": brand_row["sources"],
        "front": {
            "value_recent": safe_float(recent.get("raw_value")),
            "ms_recent_pct": safe_float(recent.get("ms")),
            "gr_qoq_pct": safe_float(recent.get("qoq")),
            "gr_yoy_pct": safe_float(recent.get("yoy")),
            "ms_change_yoy_pct": safe_float(recent.get("ms_change_yoy")),
        },
        "back": {
            "cagr_5y_pct": safe_float(ext_recent.get("cagr_5y")),
            "sales_first_period_krw": safe_float(first.get("raw_value")),
            "ms_first_period_pct": safe_float(first.get("ms")),
        },
        "back_extended": {
            "market_size_recent": market_recent,
            "market_cagr_5y_pct": series_cagr(market_series),
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


def build_kpi(source: str, rows: list[dict]) -> dict:
    source_rows = [r for r in rows if r["source"] == API_TO_SOURCE[source] and r["measure"] == "sales"]
    latest_values = []
    ms_values = []
    movement_values = []
    cagr_values = []
    for row in source_rows:
        history = decode_json(row["metric_history"])
        recent = metric_recent(history)
        ext_recent = metric_recent(decode_json(row["extended_metric_history"]))
        latest_values.append(safe_float(recent.get("raw_value")))
        ms_values.append(safe_float(recent.get("ms")))
        movement = movement_pct_from_history(history)
        if movement is not None:
            movement_values.append(movement)
        cagr_values.append(safe_float(ext_recent.get("cagr_5y")))
    total = sum(latest_values)
    rising = sum(1 for value in movement_values if value >= 0)
    declining = sum(1 for value in movement_values if value < 0)
    return {
        "total_revenue": total,
        "total_revenue_display": display_ukrw(total),
        "avg_market_share_pct": numeric_mean(ms_values),
        "rising_brand_count": rising,
        "declining_brand_count": declining,
        "cagr_5y_pct": numeric_mean(cagr_values),
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
                },
                market,
                sales_by_brand.get(brand, []),
                market_rows,
            )
        )

    payload = {
        "kpi": {
            "ubist": build_kpi("UBIST", mart_rows),
            "iqvia": build_kpi("IQVIA", mart_rows),
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
