#!/usr/bin/env python3
"""Phase 30.1 simulation visualization guardrail.

Checks the PL-requested simulation visualization corrections:

* no anomaly/stress payload or frontend UI
* scenarios lower/base/upper stay ordered across horizons
* event regressors remain disabled
* simulation chart follows the KPI horizon instead of a fixed one-year slice
"""

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
HORIZONS = {"1y": 11, "3y": 35, "5y": 59, "10y": 119}
IQVIA_HORIZONS = {"1y": 3, "3y": 11, "5y": 19, "10y": 39}


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


def _positive_floor(values: list[float]) -> float:
    positives = [float(value) for value in values if value and value > 0]
    return min(positives) * 0.3 if positives else 0.0


def _validate_frontend(issues: list[Issue]) -> None:
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    for marker in ("simulation-anomaly-box", "renderSimulationAnomaliesFromData", ".stress", "stress."):
        if marker in html:
            issues.append(Issue("frontend_forbidden_marker", detail={"marker": marker}))
    if "const historySteps = Math.min(Math.max(forecastSteps, stepsPerYear)" not in html:
        issues.append(Issue("frontend_history_not_horizon_synced"))


def _validate_sim_brand(brand: str, combo: str, sim_brand: str, payload: dict[str, Any], issues: list[Issue]) -> None:
    for key in ("anomaly_signals", "stress"):
        if key in payload:
            issues.append(Issue("simulation_forbidden_key_present", brand, combo, sim_brand, {"key": key}))

    event_regressor = ((payload.get("model") or {}).get("event_regressor") or {})
    if event_regressor.get("enabled") is not False:
        issues.append(Issue("event_regressor_enabled", brand, combo, sim_brand, {"event_regressor": event_regressor}))

    scenarios = payload.get("scenarios") or {}
    base = (scenarios.get("base") or {}).get("values") or []
    upper = (scenarios.get("upper") or {}).get("values") or []
    lower = (scenarios.get("lower") or {}).get("values") or []
    if not (base and upper and lower):
        issues.append(Issue("scenario_values_missing", brand, combo, sim_brand))
        return

    horizons = HORIZONS if combo.startswith("UBIST.") else IQVIA_HORIZONS
    floor = _positive_floor(payload.get("history_values") or [])
    for label, idx in horizons.items():
        if idx >= min(len(base), len(upper), len(lower)):
            issues.append(Issue("scenario_horizon_missing", brand, combo, sim_brand, {"horizon": label, "idx": idx}))
            continue
        b, u, l = float(base[idx]), float(upper[idx]), float(lower[idx])
        if b <= 0:
            if l != 0 or u < b:
                issues.append(Issue("scenario_order_invalid", brand, combo, sim_brand, {"horizon": label, "lower": l, "base": b, "upper": u}))
            continue
        if not (l < b < u):
            issues.append(Issue("scenario_order_invalid", brand, combo, sim_brand, {"horizon": label, "lower": l, "base": b, "upper": u}))
        effective_floor = min(floor, max(b * 0.7, 0.0)) if floor else 0.0
        if effective_floor and l < effective_floor:
            issues.append(
                Issue(
                    "scenario_lower_below_floor",
                    brand,
                    combo,
                    sim_brand,
                    {"horizon": label, "lower": l, "floor": effective_floor, "raw_history_floor": floor},
                )
            )
        if b > 0:
            delta_lower = (l - b) / b * 100
            delta_upper = (u - b) / b * 100
            max_abs = 65.0 if label in {"3y", "5y"} else 55.0
            if abs(delta_lower) > max_abs or abs(delta_upper) > max_abs:
                issues.append(
                    Issue(
                        "scenario_ci_too_wide",
                        brand,
                        combo,
                        sim_brand,
                        {"horizon": label, "lower_pct": delta_lower, "upper_pct": delta_upper, "max_abs": max_abs},
                    )
                )


def validate() -> dict[str, Any]:
    issues: list[Issue] = []
    payloads = _payloads()
    checked_payloads = 0
    _validate_frontend(issues)

    for brand in CANONICAL_25:
        payload = payloads.get(brand)
        if not payload:
            issues.append(Issue("deep_payload_missing", brand))
            continue
        simulation = ((payload.get("data") or {}).get("simulation") or {}).get("by_combo") or {}
        for combo, sim in simulation.items():
            for sim_brand, sim_payload in (sim.get("by_brand") or {}).items():
                checked_payloads += 1
                _validate_sim_brand(brand, combo, sim_brand, sim_payload, issues)

    return {
        "phase": "30.1",
        "validator": "simulation_visualization_guardrail",
        "brands": len(CANONICAL_25),
        "checked_sim_brand_payloads": checked_payloads,
        "issues_count": len(issues),
        "issues": [asdict(issue) for issue in issues],
    }


def main() -> int:
    report = validate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["issues_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
