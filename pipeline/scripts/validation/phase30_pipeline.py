#!/usr/bin/env python3
"""Phase 30 validation for v0.9.1 deep-analysis forecast/simulation payloads."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pymysql

try:
    from pipeline.scripts.etl.cache_build_common import CANONICAL_25
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from pipeline.scripts.etl.cache_build_common import CANONICAL_25


HORIZON_CI_LEVELS = {
    "1y": 0.95,
    "3y": 0.95,
    "5y": 0.95,
    "10y": 0.95,
    "method": "natural_accumulation_95_only",
    "note": "Phase 30.2: horizon 차등 제거, 모든 horizon 95% CI 자연 누적",
}
FORECAST_METHOD = "data_size_dispatch_v1_phase30_baseline"


@dataclass
class Issue:
    kind: str
    brand: str | None = None
    combo: str | None = None
    detail: dict[str, Any] | None = None


def _conn():
    return pymysql.connect(
        host="127.0.0.1",
        port=3308,
        user="root",
        password="",
        database="jw_mart",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _expected_steps(combo: str) -> int:
    return 120 if combo.startswith("UBIST.") else 40


def _payloads() -> dict[str, dict[str, Any]]:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT brand, response_json FROM cache_deep_analysis")
        return {row["brand"]: json.loads(row["response_json"]) for row in cur.fetchall()}
    finally:
        conn.close()


def _validate_events(brand: str, events: Any, issues: list[Issue]) -> None:
    if not isinstance(events, dict):
        issues.append(Issue("events_invalid_shape", brand, detail={"type": type(events).__name__}))
        return
    cut_a = events.get("cut_a") or []
    cut_b = events.get("cut_b") or []
    if len(cut_a) > 50:
        issues.append(Issue("cut_a_over_50", brand, detail={"count": len(cut_a)}))
    if brand != "플라주오피" and len(cut_a) < 5:
        issues.append(Issue("cut_a_under_5", brand, detail={"count": len(cut_a)}))
    for event in cut_b:
        if event.get("derivation") != "llm_direct" or int(event.get("score") or 0) < 80:
            issues.append(Issue("cut_b_contract_violation", brand, detail={"event": event}))


def _validate_sim_brand(brand: str, combo: str, sim_brand: str, payload: dict[str, Any], expected_steps: int, issues: list[Issue]) -> None:
    required = [
        "target_period",
        "history_periods",
        "forecast_periods",
        "history_values",
        "model",
        "horizon_ci_levels",
        "scenarios",
        "confidence",
        "market_comparison",
        "momentum",
        "warnings",
        "baseline",
    ]
    missing = [key for key in required if key not in payload]
    if missing:
        issues.append(Issue("simulation_brand_missing_keys", brand, combo, {"sim_brand": sim_brand, "missing": missing}))
    if len(payload.get("forecast_periods") or []) != expected_steps:
        issues.append(Issue("forecast_period_length_wrong", brand, combo, {"sim_brand": sim_brand, "actual": len(payload.get("forecast_periods") or []), "expected": expected_steps}))
    model = payload.get("model") or {}
    if model.get("selection_policy") != "data_size_dispatch_v1":
        issues.append(Issue("model_dispatch_policy_wrong", brand, combo, {"sim_brand": sim_brand, "model": model}))
    event_regressor = model.get("event_regressor") or {}
    if event_regressor.get("enabled") is not False:
        issues.append(Issue("event_regressor_enabled", brand, combo, {"sim_brand": sim_brand, "event_regressor": event_regressor}))
    if payload.get("horizon_ci_levels") != HORIZON_CI_LEVELS:
        issues.append(Issue("horizon_ci_levels_wrong", brand, combo, {"sim_brand": sim_brand, "actual": payload.get("horizon_ci_levels")}))
    scenarios = payload.get("scenarios") or {}
    for scenario_key in ("base", "upper", "lower"):
        values = (scenarios.get(scenario_key) or {}).get("values") or []
        if len(values) != expected_steps:
            issues.append(Issue("scenario_length_wrong", brand, combo, {"sim_brand": sim_brand, "scenario": scenario_key, "actual": len(values), "expected": expected_steps}))
    confidence = payload.get("confidence") or {}
    if confidence.get("method") != "ci_width_normalized" or not (0 <= int(confidence.get("score") or -1) <= 100):
        issues.append(Issue("confidence_invalid", brand, combo, {"sim_brand": sim_brand, "confidence": confidence}))
    if (payload.get("market_comparison") or {}).get("method") != "brand_cagr_minus_market_cagr_same_source":
        issues.append(Issue("market_comparison_invalid", brand, combo, {"sim_brand": sim_brand, "market_comparison": payload.get("market_comparison")}))
    if (payload.get("momentum") or {}).get("method") != "forecast_slope_avg":
        issues.append(Issue("momentum_invalid", brand, combo, {"sim_brand": sim_brand, "momentum": payload.get("momentum")}))
    for forbidden_key in ("anomaly_signals", "stress"):
        if forbidden_key in payload:
            issues.append(Issue("simulation_forbidden_key_present", brand, combo, {"sim_brand": sim_brand, "key": forbidden_key}))


def validate() -> dict[str, Any]:
    issues: list[Issue] = []
    payloads = _payloads()
    checked_combos = 0
    checked_sim_brands = 0

    for brand in CANONICAL_25:
        payload = payloads.get(brand)
        if not payload:
            issues.append(Issue("deep_payload_missing", brand))
            continue
        data = payload.get("data") or {}
        if "ai_analysis" in data:
            issues.append(Issue("ai_analysis_still_embedded_in_cache", brand, detail={"ai_analysis": data.get("ai_analysis")}))
        available = payload.get("available_combos") or []
        forecast = data.get("forecast") or {}
        simulation = data.get("simulation") or {}
        if forecast.get("method") != FORECAST_METHOD:
            issues.append(Issue("forecast_method_wrong", brand, detail={"method": forecast.get("method")}))
        if forecast.get("event_regressor_enabled") is not False:
            issues.append(Issue("forecast_event_regressor_flag_wrong", brand, detail={"actual": forecast.get("event_regressor_enabled")}))
        forecast_by_combo = forecast.get("by_combo") or {}
        sim_by_combo = simulation.get("by_combo") or {}
        if set(forecast_by_combo) != set(available):
            issues.append(Issue("forecast_combo_mismatch", brand, detail={"available": available, "forecast": sorted(forecast_by_combo)}))
        if set(sim_by_combo) != set(available):
            issues.append(Issue("simulation_combo_mismatch", brand, detail={"available": available, "simulation": sorted(sim_by_combo)}))
        _validate_events(brand, data.get("events"), issues)
        for combo in available:
            expected_steps = _expected_steps(combo)
            fc = forecast_by_combo.get(combo) or {}
            if len(fc.get("forecast_periods") or []) != expected_steps:
                issues.append(Issue("forecast_combo_period_length_wrong", brand, combo, {"actual": len(fc.get("forecast_periods") or []), "expected": expected_steps}))
            if len(fc.get("brands") or []) == 0 or len(fc.get("brands") or []) > 6:
                issues.append(Issue("forecast_brand_count_wrong", brand, combo, {"count": len(fc.get("brands") or [])}))
            sim = sim_by_combo.get(combo) or {}
            if sim.get("phase30_baseline") is not True:
                issues.append(Issue("simulation_not_phase30_baseline", brand, combo, {"keys": sorted(sim.keys()) if isinstance(sim, dict) else type(sim).__name__}))
                continue
            if not sim.get("by_brand"):
                issues.append(Issue("simulation_by_brand_empty", brand, combo))
            checked_combos += 1
            for sim_brand, sim_brand_payload in (sim.get("by_brand") or {}).items():
                checked_sim_brands += 1
                _validate_sim_brand(brand, combo, sim_brand, sim_brand_payload, expected_steps, issues)

    return {
        "phase": "30",
        "validator": "forecast_simulation_v0_9_1",
        "brands": len(CANONICAL_25),
        "checked_combos": checked_combos,
        "checked_sim_brands": checked_sim_brands,
        "issues_count": len(issues),
        "issues": [asdict(issue) for issue in issues],
    }


def main() -> int:
    report = validate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["issues_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
