#!/usr/bin/env python3
"""Build spec-aligned cache_deep_analysis from Phase 1 strategic ML marts."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from cache_build_common import (
    api_source,
    decode_json,
    dump_payload,
    fetch_all,
    load_catalog,
    metric_recent,
    ml_to_strategy,
    parser,
    payload_size,
    replace_rows,
    safe_float,
)


def simple_forecast(history: dict[str, Any]) -> list[dict[str, Any]]:
    recent = metric_recent(history)
    value = safe_float(recent.get("raw_value"))
    return [{"period": f"T+{i}", "value": round(value, 2), "method": "flat_baseline"} for i in range(1, 4)]


def combo_payload(row: dict[str, Any]) -> dict[str, Any]:
    history = decode_json(row.get("metric_history"))
    recent = metric_recent(history)
    value = safe_float(recent.get("raw_value"))
    return {
        "history": history,
        "forecast": simple_forecast(history),
        "unit_label": row.get("unit_label"),
        "baseline": {"value_recent": value, "ms_recent_pct": safe_float(recent.get("ms"))},
    }


def simulation_payload(row: dict[str, Any]) -> dict[str, Any]:
    recent = metric_recent(decode_json(row.get("metric_history")))
    value = safe_float(recent.get("raw_value"))
    return {
        "baseline": {"value_recent": value, "ms_recent_pct": safe_float(recent.get("ms"))},
        "scenarios": {
            "minus_10pct": round(value * 0.9, 2),
            "base": round(value, 2),
            "plus_10pct": round(value * 1.1, 2),
        },
    }


def choose_base(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(rows, key=lambda r: (not bool(r.get("is_jw")), str(r.get("ml_id")), str(r.get("source")), str(r.get("measure"))))[0]


def main() -> None:
    args = parser(__doc__).parse_args()
    ml_market = load_catalog("ml_market").set_index("ml_id", drop=False)
    rows = fetch_all("SELECT * FROM mart_strategic_ml_brand_metric")
    by_brand: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_brand[row["brand_name"]].append(row)

    output_rows: list[dict[str, Any]] = []
    for brand, brand_rows in sorted(by_brand.items()):
        base = choose_base(brand_rows)
        ml_id = base["ml_id"]
        market_id = ml_to_strategy(ml_id)
        market = ml_market.loc[ml_id].to_dict() if ml_id in ml_market.index else {}
        by_combo = {}
        sim_by_combo = {}
        seen_combo = set()
        for row in sorted(brand_rows, key=lambda r: (str(r["source"]), str(r["measure"]), str(r["ml_id"]))):
            combo = f"{api_source(row['source'])}.{row['measure']}"
            if combo in seen_combo:
                continue
            seen_combo.add(combo)
            by_combo[combo] = combo_payload(row)
            sim_by_combo[combo] = simulation_payload(row)

        payload = {
            "brand": brand,
            "market_id": market_id,
            "market_name": market.get("name"),
            "data": {
                "forecast": {"by_combo": by_combo},
                "simulation": {"by_combo": sim_by_combo},
                "events": [],
                "ai_analysis": {
                    "phenomenon": {"summary": "Precomputed market history is available by source and measure."},
                    "cause": {"summary": "Driver analysis is provided in the cause cache variants."},
                    "prediction": {"summary": "Flat baseline forecast is included for UI readiness."},
                    "recommendation": {"summary": "Use source-specific cause views for action planning."},
                },
            },
            "market_meta": {
                "available_combos": sorted(by_combo.keys()),
                "source_count": len({api_source(r["source"]) for r in brand_rows}),
                "measure_count": len({r["measure"] for r in brand_rows}),
                "market_count": len({r["ml_id"] for r in brand_rows}),
                "is_jw": bool(base.get("is_jw")),
                "is_target": bool(base.get("is_target")),
            },
        }
        output_rows.append(
            {
                "brand": brand,
                "market_id": market_id,
                "response_json": dump_payload(payload),
                "payload_size": payload_size(payload),
            }
        )

    replace_rows("cache_deep_analysis", ["brand", "market_id", "response_json", "payload_size"], output_rows)
    if args.verbose:
        print(f"cache_deep_analysis rows={len(output_rows)}")


if __name__ == "__main__":
    main()
