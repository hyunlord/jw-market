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


MOCK_EVENTS = [
    {
        "id": "event-001",
        "category": "product_launch",
        "category_label": "신제품 출시",
        "date": "2025-12-15",
        "period_map": {"UBIST": "2025-12", "IQVIA": "4Q2025"},
        "impact_score": 4.5,
        "title": "시장 내 주요 경쟁 제품 출시",
        "summary": "주요 경쟁 제품 출시로 시장 내 포지셔닝 변화 가능성이 관찰됩니다.",
        "body_full": "본 이벤트는 심층분석 화면 검증을 위한 고정 mock 이벤트입니다.",
        "source": "mock",
    },
    {
        "id": "event-002",
        "category": "guideline",
        "category_label": "진료지침",
        "date": "2025-09-01",
        "period_map": {"UBIST": "2025-09", "IQVIA": "3Q2025"},
        "impact_score": 3.8,
        "title": "치료 가이드라인 개정",
        "summary": "치료 옵션 우선순위 조정이 시장 수요에 영향을 줄 수 있습니다.",
        "body_full": "본 이벤트는 심층분석 화면 검증을 위한 고정 mock 이벤트입니다.",
        "source": "mock",
    },
]


MOCK_AI_ANALYSIS = {
    "generated_at": "2026-05-22T12:00:00Z",
    "phenomenon": {
        "title": "시장 현황",
        "body": "최근 시장 데이터에서 source와 measure별 이력 추세를 확인할 수 있습니다.",
        "bullets": ["시장 규모와 점유율은 cache에 적재된 실제 history 값을 기준으로 표시됩니다."],
    },
    "cause": {
        "title": "원인 분석",
        "body": "세부 원인은 원인분석 카드의 ranking, matrix, contribution 지표와 함께 해석합니다.",
        "bullets": ["경쟁 intensity와 상위 brand concentration을 함께 검토합니다."],
    },
    "prediction": {
        "title": "미래 예측",
        "body": "예측 모델은 본 phase에서 보류되었으며 history만 제공합니다.",
        "bullets": ["forecast_values는 의도적으로 빈 list입니다."],
    },
    "recommendation": {
        "title": "전략 제안",
        "body": "실제 예측 모델 적용 전까지는 source별 history와 원인분석 지표를 우선 사용합니다.",
        "bullets": ["후속 phase에서 forecast/simulation 모델을 연결합니다."],
    },
}


def sorted_history_values(history: dict[str, Any]) -> tuple[list[str], list[float | None]]:
    periods = sorted((history or {}).keys())
    values = []
    for period in periods:
        item = history.get(period)
        if isinstance(item, dict):
            values.append(safe_float(item.get("raw_value")))
        else:
            values.append(safe_float(item))
    return [str(period) for period in periods], values


def combo_payload(row: dict[str, Any]) -> dict[str, Any]:
    history = decode_json(row.get("metric_history"))
    recent = metric_recent(history)
    periods, values = sorted_history_values(history)
    period_unit = "monthly" if row.get("source") == "ubist" else "quarterly"
    return {
        "period_unit": period_unit,
        "unit_label": row.get("unit_label"),
        "history_periods": periods,
        "forecast_periods": [],
        "target_brand": row.get("brand_name"),
        "brands": [
            {
                "brand": row.get("brand_name"),
                "company": row.get("company_name"),
                "is_target": bool(row.get("is_target")),
                "is_jw": bool(row.get("is_jw")),
                "rank": recent.get("rank"),
                "history_values": values,
                "forecast_values": [],
            }
        ],
        "baseline": {
            "value_recent": safe_float(recent.get("raw_value")),
            "ms_recent_pct": safe_float(recent.get("ms")),
        },
    }


def simulation_payload(row: dict[str, Any]) -> dict[str, Any]:
    history = decode_json(row.get("metric_history"))
    recent = metric_recent(history)
    periods, values = sorted_history_values(history)
    period_unit = "monthly" if row.get("source") == "ubist" else "quarterly"
    return {
        "period_unit": period_unit,
        "unit_label": row.get("unit_label"),
        "source_granularity": period_unit,
        "available_brands": [row.get("brand_name")],
        "by_brand": {
            row.get("brand_name"): {
                "target_period": periods[-1] if periods else None,
                "history_periods": periods,
                "forecast_periods": [],
                "history_values": values,
                "model": {"name": "mock", "variant": "history_only"},
                "scenarios": {},
                "confidence": {"score": None, "label": "pending forecast"},
                "warnings": ["forecast not implemented yet - only history is available"],
                "baseline": {
                    "value_recent": safe_float(recent.get("raw_value")),
                    "ms_recent_pct": safe_float(recent.get("ms")),
                },
            }
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
                "events": MOCK_EVENTS,
                "ai_analysis": MOCK_AI_ANALYSIS,
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
