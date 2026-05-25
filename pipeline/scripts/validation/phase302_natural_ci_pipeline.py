#!/usr/bin/env python3
"""Phase 30.2 validation for native 95% CI forecast/simulation payloads."""

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


PROJECT_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_HTML = PROJECT_ROOT / "docs" / "reference" / "jw_market_hardcoded_mockup_v3_4.html"
HORIZON_CI_LEVELS = {
    "1y": 0.95,
    "3y": 0.95,
    "5y": 0.95,
    "10y": 0.95,
    "method": "selected_model_natural_with_funnel_floor",
    "note": "Phase 30.7: native 95% CI with horizon-scaled funnel floor",
}


@dataclass
class Issue:
    kind: str
    brand: str | None = None
    combo: str | None = None
    sim_brand: str | None = None
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


def _payloads() -> dict[str, dict[str, Any]]:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT brand, response_json FROM cache_deep_analysis")
        return {row["brand"]: json.loads(row["response_json"]) for row in cur.fetchall()}
    finally:
        conn.close()


def _validate_frontend(issues: list[Issue]) -> None:
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    expected_markers = [
        "const forecastSteps = Math.min(horizonIdx + 1, (simBrand.forecast_periods || []).length);",
        "const historySteps = Math.min(Math.max(forecastSteps, stepsPerYear)",
        "if (_deepState.data) renderSimulationCardsFromData(_deepState.data);",
        "simulation 카드 + 차트 동시 갱신",
    ]
    for marker in expected_markers:
        if marker not in html:
            issues.append(Issue("frontend_horizon_sync_marker_missing", detail={"marker": marker}))
    for forbidden in ("simulation-anomaly-box", "renderSimulationAnomaliesFromData", ".stress", "stress."):
        if forbidden in html:
            issues.append(Issue("frontend_forbidden_marker", detail={"marker": forbidden}))


def _validate_sim_brand(brand: str, combo: str, sim_brand: str, payload: dict[str, Any], issues: list[Issue]) -> None:
    if payload.get("horizon_ci_levels") != HORIZON_CI_LEVELS:
        issues.append(Issue("horizon_ci_levels_not_phase302", brand, combo, sim_brand, {"actual": payload.get("horizon_ci_levels")}))
    scenarios = payload.get("scenarios") or {}
    upper = scenarios.get("upper") or {}
    lower = scenarios.get("lower") or {}
    if upper.get("method") != "selected_model_ci_upper_95_natural_with_funnel_floor":
        issues.append(Issue("upper_method_not_natural_95", brand, combo, sim_brand, {"method": upper.get("method")}))
    if lower.get("method") != "selected_model_ci_lower_95_natural_with_funnel_floor":
        issues.append(Issue("lower_method_not_natural_95", brand, combo, sim_brand, {"method": lower.get("method")}))
    for forbidden in ("anomaly_signals", "stress"):
        if forbidden in payload:
            issues.append(Issue("simulation_forbidden_key_present", brand, combo, sim_brand, {"key": forbidden}))
    event_regressor = ((payload.get("model") or {}).get("event_regressor") or {})
    if event_regressor.get("enabled") is not False:
        issues.append(Issue("event_regressor_enabled", brand, combo, sim_brand, {"event_regressor": event_regressor}))
    base_values = (scenarios.get("base") or {}).get("values") or []
    upper_values = upper.get("values") or []
    lower_values = lower.get("values") or []
    for idx in (0, min(11, len(base_values) - 1), len(base_values) - 1):
        if idx < 0:
            continue
        b = float(base_values[idx])
        u = float(upper_values[idx])
        l = float(lower_values[idx])
        if not (l <= b <= u):
            issues.append(Issue("scenario_order_invalid", brand, combo, sim_brand, {"idx": idx, "lower": l, "base": b, "upper": u}))


def validate() -> dict[str, Any]:
    issues: list[Issue] = []
    payloads = _payloads()
    checked = 0
    _validate_frontend(issues)
    for brand in CANONICAL_25:
        payload = payloads.get(brand)
        if not payload:
            issues.append(Issue("deep_payload_missing", brand))
            continue
        simulation = ((payload.get("data") or {}).get("simulation") or {}).get("by_combo") or {}
        for combo, sim in simulation.items():
            for sim_brand, sim_payload in (sim.get("by_brand") or {}).items():
                checked += 1
                _validate_sim_brand(brand, combo, sim_brand, sim_payload, issues)
    return {
        "phase": "30.2",
        "validator": "native_95_ci_natural_accumulation",
        "brands": len(CANONICAL_25),
        "checked_sim_brand_payloads": checked,
        "issues_count": len(issues),
        "issues": [asdict(issue) for issue in issues],
    }


def main() -> int:
    report = validate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["issues_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
