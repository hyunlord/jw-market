#!/usr/bin/env python3
"""Build spec-aligned cache_deep_analysis from Phase 1 strategic ML marts."""

from __future__ import annotations

from collections import defaultdict
import re
from typing import Any

from cache_build_common import (
    api_source,
    decode_json,
    dump_payload,
    fetch_all,
    load_catalog,
    mariadb_connect,
    metric_recent,
    ml_to_strategy,
    parser,
    payload_size,
    period_key,
    safe_float,
    source_list,
)


ALL_COMBOS = [
    ("UBIST", "sales"),
    ("UBIST", "volume"),
    ("IQVIA", "sales"),
    ("IQVIA", "unit"),
    ("IQVIA", "dosage_unit"),
    ("IQVIA", "counting_unit"),
]


SOURCE_TO_INTERNAL = {"UBIST": "ubist", "IQVIA": "iqvia_nsa"}
UNIT_LABELS = {
    "sales": "KRW",
    "volume": "Rx",
    "unit": "unit",
    "dosage_unit": "dosage unit",
    "counting_unit": "counting unit",
}
FORECAST_METHOD = "deterministic_history_only_v0.9.1"
FORECAST_DISCLOSURE = (
    "각 brand의 과거 history만 사용한 deterministic seasonal/trend blend입니다. "
    "하드코딩 값이나 통계 backtest 모델이 아니며 v0.9.1 운영 표시용입니다."
)


def sorted_history_values(history: dict[str, Any]) -> tuple[list[str], list[float | None]]:
    periods = sorted((history or {}).keys(), key=period_key)
    values = []
    for period in periods:
        item = history.get(period)
        if isinstance(item, dict):
            values.append(safe_float(item.get("raw_value")))
        else:
            values.append(safe_float(item))
    return [str(period) for period in periods], values


def _recent_value(row: dict[str, Any]) -> float:
    return safe_float(metric_recent(decode_json(row.get("metric_history"))).get("raw_value")) or 0.0


def _next_month(period: str) -> str:
    year, month = map(int, period.split("-"))
    month += 1
    if month > 12:
        year += 1
        month = 1
    return f"{year:04d}-{month:02d}"


def _next_quarter(period: str) -> str:
    match = re.match(r"^(\d{4})-?Q([1-4])$", str(period))
    if not match:
        return "2026-Q1"
    year = int(match.group(1))
    quarter = int(match.group(2)) + 1
    if quarter > 4:
        year += 1
        quarter = 1
    return f"{year:04d}-Q{quarter}"


def forecast_periods_from_history(periods: list[str], source: str) -> list[str]:
    count = 120 if source == "UBIST" else 40
    if not periods:
        start = "2026-05" if source == "UBIST" else "2026-Q1"
    else:
        start = _next_month(periods[-1]) if source == "UBIST" else _next_quarter(periods[-1])
    output = []
    current = start
    for _ in range(count):
        output.append(current)
        current = _next_month(current) if source == "UBIST" else _next_quarter(current)
    return output


def available_combos_for_market(market: dict[str, Any]) -> list[str]:
    combos: list[str] = []
    sources = source_list(market.get("data_source"))
    if "UBIST" in sources:
        combos.extend(["UBIST.sales", "UBIST.volume"])
    if "IQVIA" in sources:
        combos.extend(["IQVIA.sales", "IQVIA.unit", "IQVIA.dosage_unit", "IQVIA.counting_unit"])
    return combos


def top6_rows(rows: list[dict[str, Any]], target_brand: str) -> list[dict[str, Any]]:
    target = next((row for row in rows if row.get("brand_name") == target_brand), None)
    competitors = [row for row in rows if row.get("brand_name") != target_brand]
    competitors.sort(key=_recent_value, reverse=True)
    selected = ([target] if target else []) + competitors[:5]
    return [row for row in selected if row is not None]


def deterministic_forecast_values(values: list[float | None], source: str, steps: int) -> tuple[list[float], dict[str, Any]]:
    """Create a deterministic, dependency-free forecast from existing history.

    This is intentionally simple and auditable: the next values blend a recent
    average level, same-season carry-forward, and trailing trend. It avoids
    model dependencies while making the forecast tab operational with real
    non-empty forecast_values.
    """
    clean = [safe_float(value) for value in values]
    clean = [0.0 if value is None else value for value in clean]
    if not clean or steps <= 0:
        return [], {
            "name": FORECAST_METHOD,
            "variant": "seasonal_trend_blend",
            "selection_reason": FORECAST_DISCLOSURE,
            "is_statistical_model": False,
            "backtest_available": False,
            "disclaimer": FORECAST_DISCLOSURE,
            "confidence_score": 0,
            "fit_quality": {"backtest_available": False},
        }

    season = 12 if source == "UBIST" else 4
    window = min(season, len(clean))
    recent = clean[-window:] if window else clean[-1:]
    last = clean[-1]

    deltas = [clean[idx] - clean[idx - 1] for idx in range(max(1, len(clean) - window), len(clean))]
    avg_delta = sum(deltas) / len(deltas) if deltas else 0.0
    recent_mean = sum(recent) / len(recent) if recent else last

    forecasts: list[float] = []
    for step in range(steps):
        seasonal = clean[-season + (step % season)] if len(clean) >= season else recent_mean
        trend = last + avg_delta * (step + 1)
        blended = (0.5 * seasonal) + (0.3 * trend) + (0.2 * recent_mean)
        forecasts.append(round(max(0.0, blended), 4))

    nonzero_history = sum(1 for value in clean if value > 0)
    confidence = min(85, max(35, int(nonzero_history / max(1, len(clean)) * 70) + 15))
    return forecasts, {
        "name": FORECAST_METHOD,
        "variant": "seasonal_trend_blend",
        "selection_reason": FORECAST_DISCLOSURE,
        "is_statistical_model": False,
        "backtest_available": False,
        "disclaimer": FORECAST_DISCLOSURE,
        "confidence_score": confidence,
        "fit_quality": {"backtest_available": False},
    }


def combo_payload(row: dict[str, Any], *, market_rows: list[dict[str, Any]], target_brand: str, combo_source: str) -> dict[str, Any]:
    history = decode_json(row.get("metric_history"))
    recent = metric_recent(history)
    periods, values = sorted_history_values(history)
    period_unit = "월" if row.get("source") == "ubist" else "분기"
    selected = top6_rows(market_rows, target_brand)
    forecast_periods = forecast_periods_from_history(periods, combo_source)
    return {
        "period_unit": period_unit,
        "unit_label": row.get("unit_label"),
        "history_periods": periods,
        "forecast_periods": forecast_periods,
        "target_brand": row.get("brand_name"),
        "brands": [
            _forecast_brand_entry(brand_row, target_brand=target_brand, source=combo_source, forecast_steps=len(forecast_periods))
            for brand_row in selected
        ] or [
            _forecast_brand_entry(row, target_brand=str(row.get("brand_name")), source=combo_source, forecast_steps=len(forecast_periods))
        ],
        "baseline": {
            "value_recent": safe_float(recent.get("raw_value")),
            "ms_recent_pct": safe_float(recent.get("ms")),
        },
    }


def _forecast_brand_entry(
    brand_row: dict[str, Any],
    *,
    target_brand: str,
    source: str,
    forecast_steps: int,
) -> dict[str, Any]:
    periods, values = sorted_history_values(decode_json(brand_row.get("metric_history")))
    forecast_values, model = deterministic_forecast_values(values, source, forecast_steps)
    return {
        "brand": brand_row.get("brand_name"),
        "company": brand_row.get("company_name"),
        "is_target": brand_row.get("brand_name") == target_brand,
        "is_jw": bool(brand_row.get("is_jw")),
        "rank": metric_recent(decode_json(brand_row.get("metric_history"))).get("rank"),
        "history_values": values,
        "forecast_values": forecast_values,
        "forecast_method": model["name"],
        "forecast_model": model,
        "forecast_disclaimer": model.get("disclaimer"),
        "confidence_score": model["confidence_score"],
    }


def empty_combo_payload(source: str, measure: str, brand: str, base_row: dict[str, Any]) -> dict[str, Any]:
    forecast_periods = forecast_periods_from_history([], source)
    return {
        "period_unit": "월" if source == "UBIST" else "분기",
        "unit_label": UNIT_LABELS.get(measure),
        "history_periods": [],
        "forecast_periods": forecast_periods,
        "target_brand": brand,
        "brands": [
            {
                "brand": brand,
                "company": base_row.get("company_name"),
                "is_target": bool(base_row.get("is_target")),
                "is_jw": bool(base_row.get("is_jw")),
                "rank": None,
                "history_values": [],
                "forecast_values": [],
            }
        ],
        "baseline": {"value_recent": None, "ms_recent_pct": None},
    }


def choose_base(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(rows, key=lambda r: (not bool(r.get("is_jw")), str(r.get("ml_id")), str(r.get("source")), str(r.get("measure"))))[0]


def main() -> None:
    args = parser(__doc__).parse_args()
    ml_market = load_catalog("ml_market").set_index("ml_id", drop=False)
    rows = fetch_all("SELECT * FROM mart_strategic_ml_brand_metric")
    by_brand: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_market_combo: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_brand[row["brand_name"]].append(row)
        by_market_combo[(row["ml_id"], row["source"], row["measure"])].append(row)

    columns = ["brand", "market_id", "response_json", "payload_size"]
    placeholders = ", ".join(["%s"] * len(columns))
    names = ", ".join(f"`{column}`" for column in columns)
    sql = f"REPLACE INTO `cache_deep_analysis` ({names}) VALUES ({placeholders})"
    conn = mariadb_connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM `cache_deep_analysis`")
    batch: list[tuple[Any, ...]] = []
    inserted = 0

    def flush_batch() -> None:
        nonlocal batch
        if not batch:
            return
        cur.executemany(sql, batch)
        batch = []

    for brand, brand_rows in sorted(by_brand.items()):
        base = choose_base(brand_rows)
        ml_id = base["ml_id"]
        market_id = ml_to_strategy(ml_id)
        market = ml_market.loc[ml_id].to_dict() if ml_id in ml_market.index else {}
        available_combos = available_combos_for_market(market)
        by_combo = {}
        rows_by_combo = {}
        for row in sorted(brand_rows, key=lambda r: (str(r["source"]), str(r["measure"]), str(r["ml_id"]))):
            combo = f"{api_source(row['source'])}.{row['measure']}"
            rows_by_combo.setdefault(combo, row)
        for source, measure in ALL_COMBOS:
            combo = f"{source}.{measure}"
            if combo not in available_combos:
                continue
            row = rows_by_combo.get(combo)
            internal_source = SOURCE_TO_INTERNAL[source]
            market_rows = by_market_combo.get((ml_id, internal_source, measure), [])
            if row is None:
                by_combo[combo] = empty_combo_payload(source, measure, brand, base)
            else:
                by_combo[combo] = combo_payload(row, market_rows=market_rows, target_brand=brand, combo_source=source)

        payload = {
            "brand": brand,
            "market_id": market_id,
            "market_name": market.get("name"),
            "available_combos": available_combos,
            "data": {
                "forecast": {
                    "method": FORECAST_METHOD,
                    "disclaimer": FORECAST_DISCLOSURE,
                    "is_statistical_model": False,
                    "backtest_available": False,
                    "by_combo": by_combo,
                },
                "simulation": {"by_combo": {}},
                "events": [],
                "ai_analysis": {},
            },
            "market_meta": {
                "available_combos": available_combos,
                "source_count": len({api_source(r["source"]) for r in brand_rows}),
                "measure_count": len({r["measure"] for r in brand_rows}),
                "market_count": len({r["ml_id"] for r in brand_rows}),
                "is_jw": bool(base.get("is_jw")),
                "is_target": bool(base.get("is_target")),
            },
        }
        out = {
            "brand": brand,
            "market_id": market_id,
            "response_json": dump_payload(payload),
            "payload_size": payload_size(payload),
        }
        batch.append(tuple(out[column] for column in columns))
        inserted += 1
        if len(batch) >= 20:
            flush_batch()
        if args.verbose and inserted % 500 == 0:
            print(f"inserted cache_deep_analysis rows={inserted}", flush=True)
    flush_batch()
    cur.close()
    conn.close()
    if args.verbose:
        print(f"cache_deep_analysis rows={inserted}")


if __name__ == "__main__":
    main()
