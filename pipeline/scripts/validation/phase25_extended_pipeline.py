#!/usr/bin/env python3
"""Phase 25 validation for D.3 levels, forecast disclosure, and anomaly removal."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


BASE_URL = "http://127.0.0.1:8013"
VIEWS = ("market_landscape", "competitive_dynamics")
SOURCE_MEASURES = {
    "UBIST": ("sales", "volume"),
    "IQVIA": ("sales", "unit", "dosage_unit", "counting_unit"),
}


@dataclass
class ValidationIssue:
    kind: str
    brand: str
    view: str | None = None
    source: str | None = None
    measure: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def get_json(path: str, *, base_url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{base_url}{path}", timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in {400, 404, 422}:
            return None
        raise


def load_brands(base_url: str) -> list[str]:
    payload = get_json("/api/market-status", base_url=base_url) or {}
    return [str(card.get("brand")) for card in payload.get("brand_cards", []) if card.get("brand")]


def cause_payload(
    brand: str,
    *,
    view: str,
    source: str,
    measure: str,
    base_url: str,
) -> dict[str, Any] | None:
    encoded = urllib.parse.quote(brand)
    payload = get_json(f"/api/cause/{encoded}?view={view}&source={source}&measure={measure}", base_url=base_url)
    if not payload or not payload.get("data"):
        return None
    return payload["data"]


def contains_key(obj: Any, key_name: str) -> bool:
    if isinstance(obj, dict):
        return any(key == key_name or contains_key(value, key_name) for key, value in obj.items())
    if isinstance(obj, list):
        return any(contains_key(value, key_name) for value in obj)
    return False


def validate_d3_levels(brand: str, *, view: str, source: str, measure: str, data: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    by_level = data.get("level_top5_trend", {}).get("by_level") or {}
    if "Brand" in by_level:
        issues.append(ValidationIssue("d3_brand_level_present", brand, view, source, measure))
    for level, payload in by_level.items():
        options = payload.get("all_options") or []
        if len(options) <= 1:
            issues.append(
                ValidationIssue(
                    "d3_single_option_level_present",
                    brand,
                    view,
                    source,
                    measure,
                    {"level": level, "options": options},
                )
            )
    return issues


def validate_forecast_disclosure(brand: str, *, base_url: str) -> list[ValidationIssue]:
    encoded = urllib.parse.quote(brand)
    payload = get_json(f"/api/deep-analysis/{encoded}", base_url=base_url)
    if not payload or not payload.get("data"):
        return [ValidationIssue("deep_analysis_missing", brand)]
    forecast = payload["data"].get("forecast") or {}
    issues: list[ValidationIssue] = []
    if forecast.get("method") != "deterministic_history_only_v0.9.1":
        issues.append(ValidationIssue("forecast_method_missing", brand, detail={"method": forecast.get("method")}))
    if not forecast.get("disclaimer"):
        issues.append(ValidationIssue("forecast_disclaimer_missing", brand))
    if forecast.get("is_statistical_model") is not False:
        issues.append(ValidationIssue("forecast_statistical_flag_wrong", brand, detail={"value": forecast.get("is_statistical_model")}))
    if forecast.get("backtest_available") is not False:
        issues.append(ValidationIssue("forecast_backtest_flag_wrong", brand, detail={"value": forecast.get("backtest_available")}))
    if contains_key(payload.get("data"), "anomaly_signals"):
        issues.append(ValidationIssue("anomaly_signals_present", brand))
    return issues


def run(base_url: str) -> dict[str, Any]:
    brands = load_brands(base_url)
    issues: list[ValidationIssue] = []
    checked_payloads = 0
    skipped_payloads = 0

    for brand in brands:
        issues.extend(validate_forecast_disclosure(brand, base_url=base_url))
        for view in VIEWS:
            for source, measures in SOURCE_MEASURES.items():
                for measure in measures:
                    data = cause_payload(brand, view=view, source=source, measure=measure, base_url=base_url)
                    if not data:
                        skipped_payloads += 1
                        continue
                    checked_payloads += 1
                    issues.extend(validate_d3_levels(brand, view=view, source=source, measure=measure, data=data))

    return {
        "brands": len(brands),
        "checked_payloads": checked_payloads,
        "skipped_payloads": skipped_payloads,
        "issues": [issue.__dict__ for issue in issues],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--json-out")
    args = parser.parse_args()

    result = run(args.base_url)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)

    print("=== Phase 25 Extended Validation ===")
    print(f"brands={result['brands']} checked_payloads={result['checked_payloads']} skipped={result['skipped_payloads']}")
    print(f"issues={len(result['issues'])}")
    if result["issues"]:
        for issue in result["issues"][:50]:
            print(json.dumps(issue, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
