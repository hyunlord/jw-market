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
    optional_float,
    safe_float,
    source_list,
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
    {
        "id": "event-003",
        "category": "policy",
        "category_label": "정책/급여",
        "date": "2025-06-01",
        "period_map": {"UBIST": "2025-06", "IQVIA": "2Q2025"},
        "impact_score": 3.4,
        "title": "급여 기준 검토",
        "summary": "급여 기준 변화 가능성이 처방 흐름에 영향을 줄 수 있습니다.",
        "body_full": "본 이벤트는 심층분석 화면 검증을 위한 고정 mock 이벤트입니다.",
        "source": "mock",
    },
    {
        "id": "event-004",
        "category": "supply",
        "category_label": "공급/수급",
        "date": "2025-03-15",
        "period_map": {"UBIST": "2025-03", "IQVIA": "1Q2025"},
        "impact_score": 2.9,
        "title": "공급 안정성 이슈",
        "summary": "일부 제품 수급 변동으로 단기 처방 대체 가능성이 있습니다.",
        "body_full": "본 이벤트는 심층분석 화면 검증을 위한 고정 mock 이벤트입니다.",
        "source": "mock",
    },
    {
        "id": "event-005",
        "category": "competition",
        "category_label": "경쟁 변화",
        "date": "2024-12-01",
        "period_map": {"UBIST": "2024-12", "IQVIA": "4Q2024"},
        "impact_score": 3.6,
        "title": "상위 경쟁군 프로모션 강화",
        "summary": "상위 경쟁군의 영업 활동 강화가 시장 점유율 변화와 함께 관찰됩니다.",
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
        "body": "최근 history의 추세와 계절성을 결합한 deterministic forecast를 제공합니다.",
        "bullets": ["forecast_values는 source granularity에 맞춰 월간/분기별로 생성됩니다."],
    },
    "recommendation": {
        "title": "전략 제안",
        "body": "forecast는 의사결정 보조용이며 원인분석 지표와 함께 검토합니다.",
        "bullets": ["향후 phase에서 통계 backtest 모델로 교체 가능한 구조입니다."],
    },
}


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
HORIZON_CI_LEVELS = {"1y": 0.95, "3y": 0.90, "5y": 0.80, "10y": 0.50}


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


def cagr_from_values(values: list[float | None], source: str) -> float | None:
    clean = [optional_float(value) for value in values]
    clean = [value for value in clean if value is not None]
    if len(clean) < 2 or clean[0] <= 0 or clean[-1] <= 0:
        return None
    years = max((len(clean) - 1) / (12 if source == "UBIST" else 4), 1)
    return ((clean[-1] / clean[0]) ** (1 / years) - 1) * 100


def build_market_history(rows: list[dict[str, Any]]) -> tuple[list[str], list[float]]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        history = decode_json(row.get("metric_history"))
        if not isinstance(history, dict):
            continue
        for period, item in history.items():
            if isinstance(item, dict):
                totals[str(period)] += safe_float(item.get("raw_value")) or 0.0
            else:
                totals[str(period)] += safe_float(item) or 0.0
    periods = sorted(totals.keys(), key=period_key)
    return periods, [totals[period] for period in periods]


def momentum_payload(values: list[float | None], source: str) -> dict[str, Any]:
    n = 12 if source == "UBIST" else 4
    clean = [safe_float(value) or 0.0 for value in values]
    basis = "12m" if source == "UBIST" else "4q"
    if len(clean) < n + 1:
        return {"value_pct_per_period": None, "label": "insufficient_data", "basis": basis, "n_periods": n, "method": "trailing_mean"}
    recent = clean[-n:]
    previous = clean[-2 * n : -n] if len(clean) >= 2 * n else clean[:n]
    previous_total = sum(previous)
    if previous_total <= 0:
        return {"value_pct_per_period": None, "label": "not_computable", "basis": basis, "n_periods": n, "method": "trailing_mean"}
    pct = ((sum(recent) - previous_total) / previous_total) * 100
    label = "rising" if pct > 5 else "declining" if pct < -5 else "stable"
    return {"value_pct_per_period": round(pct, 4), "label": label, "basis": basis, "n_periods": n, "method": "trailing_mean"}


def anomaly_payload(periods: list[str], values: list[float | None], source: str) -> dict[str, Any]:
    window = 12 if source == "UBIST" else 4
    threshold = 30.0
    clean = [safe_float(value) or 0.0 for value in values]
    items = []
    for index in range(window, len(clean)):
        previous = clean[index - window]
        if previous <= 0:
            continue
        yoy = ((clean[index] - previous) / previous) * 100
        if abs(yoy) >= threshold:
            items.append(
                {
                    "period": periods[index],
                    "value": clean[index],
                    "expected_value": previous,
                    "yoy_pct": round(yoy, 4),
                    "direction": "up" if yoy > 0 else "down",
                    "threshold_pass": True,
                }
            )
    return {"method": "yoy_threshold", "threshold_yoy_pct": threshold, "window": window, "fallback_top_n": 5, "items": items[-10:]}


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
        return [], {"name": "deterministic_trend", "confidence_score": 0}

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
        "name": "deterministic_trend",
        "variant": "seasonal_trend_blend",
        "selection_reason": "history-only deterministic forecast for v0.9.1 operational display",
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


def brand_simulation_entry(row: dict[str, Any], *, source: str, measure: str, market_periods: list[str], market_values: list[float]) -> dict[str, Any]:
    history = decode_json(row.get("metric_history"))
    recent = metric_recent(history)
    periods, values = sorted_history_values(history)
    brand_cagr = cagr_from_values(values, source)
    market_cagr = cagr_from_values(market_values, source)
    forecast_periods = forecast_periods_from_history(periods, source)
    forecast_values, forecast_model = deterministic_forecast_values(values, source, len(forecast_periods))
    upper_values = [round(value * 1.12, 4) for value in forecast_values]
    lower_values = [round(value * 0.88, 4) for value in forecast_values]
    return {
        "target_period": periods[-1] if periods else None,
        "history_periods": periods,
        "forecast_periods": forecast_periods,
        "history_values": values,
        "model": {
            "name": forecast_model["name"],
            "variant": forecast_model["variant"],
            "selection_reason": forecast_model["selection_reason"],
            "fit_quality": {"backtest_available": False},
        },
        "horizon_ci_levels": HORIZON_CI_LEVELS,
        "scenarios": {
            "base": {"label": "Base", "method": forecast_model["name"], "values": forecast_values, "final_value": forecast_values[-1] if forecast_values else None},
            "upper": {"label": "Upper", "method": "base_plus_12pct", "values": upper_values, "final_value": upper_values[-1] if upper_values else None},
            "lower": {"label": "Lower", "method": "base_minus_12pct", "values": lower_values, "final_value": lower_values[-1] if lower_values else None},
        },
        "stress": {"method": "history_anomaly", "note": "deterministic forecast; inspect anomaly signals before use"},
        "confidence": {"score": forecast_model["confidence_score"], "label": "deterministic", "method": forecast_model["name"]},
        "market_comparison": {
            "delta_pp": round(brand_cagr - market_cagr, 4) if brand_cagr is not None and market_cagr is not None else None,
            "brand_cagr_pct": round(brand_cagr, 4) if brand_cagr is not None else None,
            "market_cagr_pct": round(market_cagr, 4) if market_cagr is not None else None,
            "basis": "5y",
            "horizon": "5y",
            "method": "history_only",
        },
        "momentum": momentum_payload(values, source),
        "anomaly_signals": anomaly_payload(periods, values, source),
        "warnings": ["deterministic history-only forecast; no external causal model applied"],
        "baseline": {"value_recent": safe_float(recent.get("raw_value")), "ms_recent_pct": safe_float(recent.get("ms"))},
    }


def simulation_payload(
    row: dict[str, Any] | None,
    *,
    source: str | None = None,
    measure: str | None = None,
    brand: str | None = None,
    base_row: dict[str, Any] | None = None,
    market_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if row is None:
        base_row = base_row or {}
        period_unit = "월" if source == "UBIST" else "분기"
        brand_name = brand or base_row.get("brand_name")
        return {
            "period_unit": period_unit,
            "unit_label": UNIT_LABELS.get(measure or ""),
            "source_granularity": period_unit,
            "available_brands": [brand_name],
            "by_brand": {
                brand_name: {
                    "target_period": None,
                    "history_periods": [],
                    "forecast_periods": forecast_periods_from_history([], source or "UBIST"),
                    "history_values": [],
                    "model": {"name": "pending", "variant": "history_only"},
                    "horizon_ci_levels": HORIZON_CI_LEVELS,
                    "scenarios": {
                        "base": {"values": [], "final_value": None, "method": "pending"},
                        "upper": {"values": [], "final_value": None, "method": "pending"},
                        "lower": {"values": [], "final_value": None, "method": "pending"},
                    },
                    "confidence": {"score": None, "label": "forecast pending"},
                    "market_comparison": {"delta_pp": None, "brand_cagr_pct": None, "market_cagr_pct": None, "basis": "5y", "horizon": "5y", "method": "history_only"},
                    "momentum": {"value_pct_per_period": None, "label": "insufficient_data", "basis": "12m" if source == "UBIST" else "4q", "n_periods": 12 if source == "UBIST" else 4, "method": "trailing_mean"},
                    "anomaly_signals": {"method": "yoy_threshold", "threshold_yoy_pct": 30.0, "window": 12 if source == "UBIST" else 4, "fallback_top_n": 5, "items": []},
                    "warnings": ["forecast not implemented yet - only history is available"],
                }
            },
        }

    history = decode_json(row.get("metric_history"))
    periods, values = sorted_history_values(history)
    combo_source = source or api_source(row.get("source"))
    period_unit = "월" if row.get("source") == "ubist" else "분기"
    selected = top6_rows(market_rows or [row], brand or row.get("brand_name"))
    market_periods, market_values = build_market_history(market_rows or [row])
    return {
        "period_unit": period_unit,
        "unit_label": row.get("unit_label"),
        "source_granularity": period_unit,
        "available_brands": [
            {"brand": selected_row.get("brand_name"), "is_target": selected_row.get("brand_name") == (brand or row.get("brand_name")), "is_jw": bool(selected_row.get("is_jw")), "rank": metric_recent(decode_json(selected_row.get("metric_history"))).get("rank")}
            for selected_row in selected
        ],
        "by_brand": {
            selected_row.get("brand_name"): brand_simulation_entry(
                selected_row,
                source=combo_source,
                measure=measure or row.get("measure"),
                market_periods=market_periods,
                market_values=market_values,
            )
            for selected_row in selected
        },
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
        sim_by_combo = {}
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
                sim_by_combo[combo] = simulation_payload(None, source=source, measure=measure, brand=brand, base_row=base)
            else:
                by_combo[combo] = combo_payload(row, market_rows=market_rows, target_brand=brand, combo_source=source)
                sim_by_combo[combo] = simulation_payload(row, source=source, measure=measure, brand=brand, market_rows=market_rows)

        payload = {
            "brand": brand,
            "market_id": market_id,
            "market_name": market.get("name"),
            "available_combos": available_combos,
            "data": {
                "forecast": {"by_combo": by_combo},
                "simulation": {"by_combo": sim_by_combo},
                "events": MOCK_EVENTS,
                "ai_analysis": MOCK_AI_ANALYSIS,
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
