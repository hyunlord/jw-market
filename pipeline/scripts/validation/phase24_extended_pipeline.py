#!/usr/bin/env python3
"""Phase 24 extended validation for D.2/D.3, forecast, and sparse history.

This script intentionally sits on top of the Phase 23 validator. Phase 23 keeps
the cross-chart share consistency gate; Phase 24 adds the gaps PL called out:
source-specific measure coverage, non-empty deterministic forecasts, explicit
channel history quality, complete D.3 option lists, and atomic dose options.
"""

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


def available_source_measures(brand: str, view: str, source: str, base_url: str) -> dict[str, dict[str, Any]]:
    payloads = {}
    for measure in SOURCE_MEASURES[source]:
        data = cause_payload(brand, view=view, source=source, measure=measure, base_url=base_url)
        if data:
            payloads[measure] = data
    return payloads


def validate_source_measure_completeness(
    *,
    brand: str,
    view: str,
    source: str,
    payloads: dict[str, dict[str, Any]],
) -> list[ValidationIssue]:
    if not payloads:
        return []
    expected = set(SOURCE_MEASURES[source])
    actual = set(payloads)
    if actual == expected:
        return []
    return [
        ValidationIssue(
            kind="source_measure_incomplete",
            brand=brand,
            view=view,
            source=source,
            detail={"expected": sorted(expected), "actual": sorted(actual)},
        )
    ]


def validate_d2_channel_history(
    *,
    brand: str,
    view: str,
    source: str,
    measure: str,
    data: dict[str, Any],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    d2 = data.get("target_customer_competition") or {}
    for channel_view in d2.get("views") or []:
        periods = channel_view.get("periods") or []
        target_name = str(channel_view.get("target_name"))
        quality = channel_view.get("data_quality") or {}
        if len(periods) < 10:
            issues.append(
                ValidationIssue(
                    kind="d2_channel_history_too_short",
                    brand=brand,
                    view=view,
                    source=source,
                    measure=measure,
                    detail={"target_name": target_name, "period_count": len(periods)},
                )
            )
        if quality.get("period_count") != len(periods):
            issues.append(
                ValidationIssue(
                    kind="d2_channel_quality_period_mismatch",
                    brand=brand,
                    view=view,
                    source=source,
                    measure=measure,
                    detail={"target_name": target_name, "quality": quality, "period_count": len(periods)},
                )
            )
        nonzero = quality.get("nonzero_period_count")
        if isinstance(nonzero, int) and nonzero < len(periods) and not quality.get("note"):
            issues.append(
                ValidationIssue(
                    kind="d2_sparse_channel_missing_note",
                    brand=brand,
                    view=view,
                    source=source,
                    measure=measure,
                    detail={"target_name": target_name, "quality": quality},
                )
            )
    return issues


def validate_d3_options(
    *,
    brand: str,
    view: str,
    source: str,
    measure: str,
    data: dict[str, Any],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    by_level = data.get("level_top5_trend", {}).get("by_level") or {}
    for level, payload in by_level.items():
        values = payload.get("values") or []
        options = payload.get("all_options") or []
        value_names = [item.get("value") for item in values if item.get("value") not in (None, "")]
        if set(value_names) != set(options) or len(value_names) != len(options):
            issues.append(
                ValidationIssue(
                    kind="d3_options_not_complete",
                    brand=brand,
                    view=view,
                    source=source,
                    measure=measure,
                    detail={
                        "level": level,
                        "values_count": len(value_names),
                        "options_count": len(options),
                        "missing_in_options": sorted(set(value_names) - set(options))[:20],
                        "missing_in_values": sorted(set(options) - set(value_names))[:20],
                    },
                )
            )
        if level == "용량":
            composite = [option for option in options if "|" in str(option)]
            if composite:
                issues.append(
                    ValidationIssue(
                        kind="d3_dose_option_composite",
                        brand=brand,
                        view=view,
                        source=source,
                        measure=measure,
                        detail={"examples": composite[:20], "count": len(composite)},
                    )
                )
    return issues


def validate_deep_forecast(brand: str, *, base_url: str) -> list[ValidationIssue]:
    encoded = urllib.parse.quote(brand)
    payload = get_json(f"/api/deep-analysis/{encoded}", base_url=base_url)
    if not payload or not payload.get("data"):
        return [ValidationIssue(kind="forecast_payload_missing", brand=brand)]
    by_combo = payload["data"].get("forecast", {}).get("by_combo") or {}
    issues: list[ValidationIssue] = []
    if not by_combo:
        return [ValidationIssue(kind="forecast_combo_missing", brand=brand)]
    checked_series = 0
    for combo, combo_payload in by_combo.items():
        brands = combo_payload.get("brands") or []
        for brand_row in brands:
            history = brand_row.get("history_values") or []
            if not history:
                continue
            checked_series += 1
            forecast = brand_row.get("forecast_values") or []
            periods = combo_payload.get("forecast_periods") or []
            if not forecast:
                issues.append(
                    ValidationIssue(
                        kind="forecast_values_empty",
                        brand=brand,
                        source=str(combo).split(".", 1)[0],
                        measure=str(combo).split(".", 1)[1] if "." in str(combo) else None,
                        detail={"combo": combo, "row_brand": brand_row.get("brand")},
                    )
                )
            elif periods and len(forecast) != len(periods):
                issues.append(
                    ValidationIssue(
                        kind="forecast_period_value_length_mismatch",
                        brand=brand,
                        source=str(combo).split(".", 1)[0],
                        measure=str(combo).split(".", 1)[1] if "." in str(combo) else None,
                        detail={"combo": combo, "row_brand": brand_row.get("brand"), "periods": len(periods), "values": len(forecast)},
                    )
                )
    if checked_series == 0:
        issues.append(ValidationIssue(kind="forecast_history_series_missing", brand=brand))
    return issues


def validate_payload(
    *,
    brand: str,
    view: str,
    source: str,
    measure: str,
    data: dict[str, Any],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues.extend(validate_d2_channel_history(brand=brand, view=view, source=source, measure=measure, data=data))
    issues.extend(validate_d3_options(brand=brand, view=view, source=source, measure=measure, data=data))
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--json-out")
    parser.add_argument("--fail-on-brand-count", type=int, default=25)
    args = parser.parse_args()

    brands = load_brands(args.base_url)
    issues: list[ValidationIssue] = []
    checked_payloads = 0
    checked_source_buckets = 0
    skipped_source_buckets = 0

    if len(brands) != args.fail_on_brand_count:
        issues.append(
            ValidationIssue(
                kind="brand_count_mismatch",
                brand="*",
                detail={"expected": args.fail_on_brand_count, "actual": len(brands), "brands": brands},
            )
        )

    for brand in brands:
        issues.extend(validate_deep_forecast(brand, base_url=args.base_url))
        for view in VIEWS:
            for source in SOURCE_MEASURES:
                payloads = available_source_measures(brand, view, source, args.base_url)
                if not payloads:
                    skipped_source_buckets += 1
                    continue
                checked_source_buckets += 1
                issues.extend(
                    validate_source_measure_completeness(
                        brand=brand,
                        view=view,
                        source=source,
                        payloads=payloads,
                    )
                )
                for measure, data in payloads.items():
                    checked_payloads += 1
                    issues.extend(
                        validate_payload(
                            brand=brand,
                            view=view,
                            source=source,
                            measure=measure,
                            data=data,
                        )
                    )

    report = {
        "brands": len(brands),
        "planned_source_buckets": len(brands) * len(VIEWS) * len(SOURCE_MEASURES),
        "checked_source_buckets": checked_source_buckets,
        "skipped_unsupported_source_buckets": skipped_source_buckets,
        "checked_payloads": checked_payloads,
        "issues": [issue.__dict__ for issue in issues],
    }

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fp:
            json.dump(report, fp, ensure_ascii=False, indent=2)

    print("=== Phase 24 extended validation ===")
    print(f"brands={report['brands']}")
    print(f"planned_source_buckets={report['planned_source_buckets']}")
    print(f"checked_source_buckets={checked_source_buckets}")
    print(f"skipped_unsupported_source_buckets={skipped_source_buckets}")
    print(f"checked_payloads={checked_payloads}")
    print(f"issues={len(issues)}")
    for issue in issues[:50]:
        print(json.dumps(issue.__dict__, ensure_ascii=False, sort_keys=True))
    if len(issues) > 50:
        print(f"... {len(issues) - 50} more")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
