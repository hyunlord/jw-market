#!/usr/bin/env python3
"""Phase 30.4 validation for history-anchored CI scenarios."""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from pipeline.scripts.etl.cache_build_common import CANONICAL_25
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from pipeline.scripts.etl.cache_build_common import CANONICAL_25


BASE_URL = "http://127.0.0.1:8013"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_HTML = PROJECT_ROOT / "docs" / "reference" / "jw_market_hardcoded_mockup_v3_4.html"


@dataclass
class Issue:
    kind: str
    brand: str | None = None
    combo: str | None = None
    sim_brand: str | None = None
    detail: dict[str, Any] | None = None


def _deep_payload(brand: str) -> dict[str, Any] | None:
    encoded = urllib.parse.quote(brand)
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/deep-analysis/{encoded}", timeout=30) as response:
            return json.load(response)
    except Exception as exc:  # pragma: no cover - operational gate
        return {"_error": str(exc)}


def _close_enough(a: float, b: float) -> bool:
    return abs(a - b) <= max(abs(b) * 0.001, 1.0)


def _validate_frontend(issues: list[Issue]) -> None:
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    markers = [
        "[Phase 30.4] anchor mismatch",
        "const hasForecastAnchor =",
        "const offset = hasForecastAnchor ? Math.max(historyValues.length - 1, 0) : historyValues.length;",
        "const horizonIdx = horizonYears * stepsPerYear;",
    ]
    for marker in markers:
        if marker not in html:
            issues.append(Issue("frontend_anchor_marker_missing", detail={"marker": marker}))


def validate() -> dict[str, Any]:
    issues: list[Issue] = []
    checked = 0
    _validate_frontend(issues)

    for brand in sorted(CANONICAL_25):
        payload = _deep_payload(brand)
        if not payload or payload.get("_error"):
            issues.append(Issue("deep_analysis_api_error", brand, detail={"error": None if not payload else payload.get("_error")}))
            continue
        sim_by_combo = ((payload.get("data") or {}).get("simulation") or {}).get("by_combo") or {}
        for combo_key, combo_data in sim_by_combo.items():
            for sim_brand, sim_payload in (combo_data.get("by_brand") or {}).items():
                history_values = sim_payload.get("history_values") or []
                history_periods = sim_payload.get("history_periods") or []
                forecast_values = sim_payload.get("forecast_values") or []
                forecast_periods = sim_payload.get("forecast_periods") or []
                scenarios = sim_payload.get("scenarios") or {}
                base = (scenarios.get("base") or {}).get("values") or []
                upper = (scenarios.get("upper") or {}).get("values") or []
                lower = (scenarios.get("lower") or {}).get("values") or []
                if not history_values or not history_periods or not base:
                    continue
                checked += 1
                history_last = float(history_values[-1])
                if forecast_periods and forecast_periods[0] != history_periods[-1]:
                    issues.append(
                        Issue(
                            "forecast_period_not_anchored",
                            brand,
                            combo_key,
                            sim_brand,
                            {"forecast_first": forecast_periods[0], "history_last": history_periods[-1]},
                        )
                    )
                checks = {
                    "forecast_values[0]": forecast_values[0] if forecast_values else None,
                    "base[0]": base[0] if base else None,
                    "upper[0]": upper[0] if upper else None,
                    "lower[0]": lower[0] if lower else None,
                }
                for name, value in checks.items():
                    if value is None or not _close_enough(float(value), history_last):
                        issues.append(
                            Issue(
                                "value_not_anchored",
                                brand,
                                combo_key,
                                sim_brand,
                                {"field": name, "actual": value, "history_last": history_last},
                            )
                        )
                if upper and lower and not _close_enough(float(upper[0] - lower[0]), 0.0):
                    issues.append(Issue("ci_width_at_anchor_not_zero", brand, combo_key, sim_brand, {"width": upper[0] - lower[0]}))
                if len(upper) > 2 and len(lower) > 2:
                    width_t1 = float(upper[1] - lower[1])
                    width_last = float(upper[-1] - lower[-1])
                    if width_t1 <= 0:
                        issues.append(Issue("ci_width_t1_not_positive", brand, combo_key, sim_brand, {"width_t1": width_t1}))
                    if width_last <= width_t1:
                        issues.append(Issue("ci_width_not_accumulating", brand, combo_key, sim_brand, {"width_t1": width_t1, "width_last": width_last}))
                if (scenarios.get("upper") or {}).get("method") != "selected_model_ci_upper_95_natural":
                    issues.append(Issue("upper_method_not_ci_direct", brand, combo_key, sim_brand, {"method": (scenarios.get("upper") or {}).get("method")}))
                if (scenarios.get("lower") or {}).get("method") != "selected_model_ci_lower_95_natural":
                    issues.append(Issue("lower_method_not_ci_direct", brand, combo_key, sim_brand, {"method": (scenarios.get("lower") or {}).get("method")}))

    return {
        "phase": "30.4",
        "validator": "history_anchor_natural_ci",
        "brands": len(CANONICAL_25),
        "checked_sim_brand_payloads": checked,
        "issues_count": len(issues),
        "issues": [asdict(issue) for issue in issues[:50]],
    }


def main() -> int:
    report = validate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["issues_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
