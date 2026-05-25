#!/usr/bin/env python3
"""Phase 28 validation for deep-analysis fake-data removal.

The gate keeps the deep-analysis tab honest:

* AI analysis may be empty until a real analysis producer writes it.
* Events may be present only when they are real, not the legacy mock list.
* Forecast may remain the Phase 24 deterministic history-only output, with
  explicit disclosure that it is not a statistical model.
* Simulation must stay empty until a real simulation producer exists.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    from phase23_consistency_pipeline import BASE_URL, get_json, load_brands
except ModuleNotFoundError:  # pragma: no cover - package import path under pytest
    from pipeline.scripts.validation.phase23_consistency_pipeline import BASE_URL, get_json, load_brands


PROJECT_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_HTML = PROJECT_ROOT / "docs" / "reference" / "jw_market_hardcoded_mockup_v3_4.html"
FORECAST_METHOD = "deterministic_history_only_v0.9.1"

AI_PLACEHOLDER_KEYS = {"generated_at", "phenomenon", "cause", "prediction", "recommendation"}
MOCK_EVENT_MARKERS = ("mock", "고정 mock", "화면 검증", "주요 경쟁 제품 출시")
SIMULATION_GENERATED_KEYS = {
    "target_period",
    "history_periods",
    "forecast_periods",
    "history_values",
    "model",
    "horizon_ci_levels",
    "scenarios",
    "stress",
    "confidence",
    "market_comparison",
    "momentum",
    "warnings",
    "baseline",
}
STATIC_FRONTEND_MARKERS = (
    "피타바스타틴 제네릭 5개사 동시 출시",
    "스타틴 시장 QoQ +3.2% 성장",
    "가드메트 M/S 8.3%, 4분기 연속 성장",
    "의원 채널 점유율 +2.7%p QoQ",
    "2026.05.19 08:00 KST",
)


@dataclass
class ValidationIssue:
    kind: str
    brand: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def deep_payload(brand: str, *, base_url: str) -> dict[str, Any] | None:
    encoded = urllib.parse.quote(brand)
    payload = get_json(f"/api/deep-analysis/{encoded}", base_url=base_url)
    if not payload or not payload.get("data"):
        return None
    return payload["data"]


def _contains_marker(value: Any, markers: tuple[str, ...]) -> str | None:
    text = json.dumps(value, ensure_ascii=False).lower()
    for marker in markers:
        if marker.lower() in text:
            return marker
    return None


def validate_ai_analysis(brand: str, data: dict[str, Any]) -> list[ValidationIssue]:
    ai = data.get("ai_analysis") or {}
    if not ai:
        return []
    if isinstance(ai, dict) and AI_PLACEHOLDER_KEYS.intersection(ai):
        return [
            ValidationIssue(
                "ai_analysis_placeholder_payload",
                brand,
                {"keys": sorted(ai.keys())},
            )
        ]
    marker = _contains_marker(ai, ("시장 현황", "원인 분석", "미래 예측", "전략 제안"))
    if marker:
        return [ValidationIssue("ai_analysis_static_text", brand, {"marker": marker})]
    return []


def validate_events(brand: str, data: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    raw_events = data.get("events") or []
    if isinstance(raw_events, dict):
        events = list(raw_events.get("cut_a") or []) + list(raw_events.get("cut_b") or [])
    elif isinstance(raw_events, list):
        events = raw_events
    else:
        return [ValidationIssue("events_invalid_shape", brand, {"type": type(raw_events).__name__})]
    for event in events:
        if str(event.get("source", "")).strip().lower() == "mock":
            issues.append(ValidationIssue("mock_event_source", brand, {"event_id": event.get("id")}))
        marker = _contains_marker(event, MOCK_EVENT_MARKERS)
        if marker:
            issues.append(
                ValidationIssue(
                    "mock_event_text",
                    brand,
                    {"event_id": event.get("id"), "marker": marker},
                )
            )
    return issues


def validate_forecast(brand: str, data: dict[str, Any]) -> list[ValidationIssue]:
    forecast = data.get("forecast") or {}
    if not forecast:
        return []
    issues: list[ValidationIssue] = []
    if forecast.get("method") != FORECAST_METHOD:
        issues.append(
            ValidationIssue(
                "forecast_method_changed",
                brand,
                {"actual": forecast.get("method"), "expected": FORECAST_METHOD},
            )
        )
    if forecast.get("is_statistical_model") is not False:
        issues.append(ValidationIssue("forecast_statistical_flag_wrong", brand, {"actual": forecast.get("is_statistical_model")}))
    if forecast.get("backtest_available") is not False:
        issues.append(ValidationIssue("forecast_backtest_flag_wrong", brand, {"actual": forecast.get("backtest_available")}))
    if not forecast.get("disclaimer"):
        issues.append(ValidationIssue("forecast_disclaimer_missing", brand))
    return issues


def validate_simulation(brand: str, data: dict[str, Any]) -> list[ValidationIssue]:
    simulation = data.get("simulation") or {}
    if not simulation:
        return []
    by_combo = simulation.get("by_combo") or {}
    if not by_combo:
        return []
    issues: list[ValidationIssue] = []
    for combo, payload in by_combo.items():
        if isinstance(payload, dict) and payload.get("poc") is True and payload.get("backtest"):
            continue
        issues.append(
            ValidationIssue(
                "simulation_unverified_payload_present",
                brand,
                {"combo": combo, "keys": sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__},
            )
        )
    return issues


def validate_no_anomaly(brand: str, data: dict[str, Any]) -> list[ValidationIssue]:
    marker = _contains_marker(data, ("anomaly_signals", "최근 이상 변동", "자동 탐지"))
    if marker:
        return [ValidationIssue("anomaly_output_present", brand, {"marker": marker})]
    return []


def validate_frontend_static_placeholders(path: Path = FRONTEND_HTML) -> list[ValidationIssue]:
    if not path.exists():
        return [ValidationIssue("frontend_html_missing", detail={"path": str(path)})]
    html = path.read_text(encoding="utf-8")
    issues: list[ValidationIssue] = []
    for marker in STATIC_FRONTEND_MARKERS:
        if marker in html:
            issues.append(ValidationIssue("frontend_static_deep_placeholder", detail={"marker": marker}))
    return issues


def run(*, base_url: str = BASE_URL, fail_on_brand_count: int = 25) -> dict[str, Any]:
    brands = load_brands(base_url)
    issues: list[ValidationIssue] = []
    checked = 0

    if len(brands) != fail_on_brand_count:
        issues.append(
            ValidationIssue(
                "brand_count_mismatch",
                detail={"expected": fail_on_brand_count, "actual": len(brands), "brands": brands},
            )
        )

    for brand in brands:
        data = deep_payload(brand, base_url=base_url)
        if not data:
            issues.append(ValidationIssue("deep_analysis_missing", brand))
            continue
        checked += 1
        issues.extend(validate_ai_analysis(brand, data))
        issues.extend(validate_events(brand, data))
        issues.extend(validate_forecast(brand, data))
        issues.extend(validate_simulation(brand, data))
        issues.extend(validate_no_anomaly(brand, data))

    issues.extend(validate_frontend_static_placeholders())
    return {
        "phase": "28",
        "validator": "deep_analysis_no_hardcoding",
        "brands": len(brands),
        "checked_payloads": checked,
        "issues_count": len(issues),
        "issues": [asdict(issue) for issue in issues],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--json-out")
    parser.add_argument("--fail-on-brand-count", type=int, default=25)
    args = parser.parse_args()

    report = run(base_url=args.base_url, fail_on_brand_count=args.fail_on_brand_count)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)

    print("=== Phase 28 Deep Analysis No-Hardcoding Validation ===")
    print(f"brands={report['brands']}")
    print(f"checked_payloads={report['checked_payloads']}")
    print(f"issues={report['issues_count']}")
    for issue in report["issues"][:50]:
        print(json.dumps(issue, ensure_ascii=False, sort_keys=True))
    if report["issues_count"] > 50:
        print(f"... {report['issues_count'] - 50} more")
    return 1 if report["issues"] else 0


if __name__ == "__main__":
    sys.exit(main())
