#!/usr/bin/env python3
"""Phase 23 consistency validation for cause-cache chart payloads.

The pipeline checks every JW brand across the required view/source/measure
matrix and fails when chart-level market-share fields drift apart, D.2 channel
tabs collapse to the same payload, D.3 segment totals repeat the same market
total, or D.1 sales contributions look unit-scaled.
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
SOURCES = ("UBIST", "IQVIA")
MEASURES = ("sales", "unit", "dosage_unit", "counting_unit")
CHART_TOLERANCE_PCT = 1.0


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
        with urllib.request.urlopen(f"{base_url}{path}", timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in {400, 404, 422}:
            return None
        raise


def cause_payload(
    brand: str,
    *,
    view: str,
    source: str,
    measure: str,
    base_url: str,
) -> dict[str, Any] | None:
    encoded = urllib.parse.quote(brand)
    path = f"/api/cause/{encoded}?view={view}&source={source}&measure={measure}"
    payload = get_json(path, base_url=base_url)
    if not payload or not payload.get("data"):
        return None
    return payload["data"]


def load_brands(base_url: str) -> list[str]:
    payload = get_json("/api/market-status", base_url=base_url) or {}
    brands = [
        str(card.get("brand"))
        for card in payload.get("brand_cards", [])
        if card.get("brand")
    ]
    return brands


def as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def latest_target_ms_from_a2(data: dict[str, Any]) -> float | None:
    yearly = data.get("brand_ranking_stacked", {}).get("yearly") or []
    if not yearly:
        return None
    for year_payload in reversed(yearly):
        rankings = year_payload.get("rankings") or []
        target = next((row for row in rankings if row.get("is_target")), None)
        if target:
            return as_float(target.get("ms_pct") or target.get("share_pct"))
    return None


def target_ms_from_rows(rows: list[dict[str, Any]]) -> float | None:
    target = next((row for row in rows if row.get("is_target")), None)
    if not target:
        return None
    return as_float(target.get("ms_pct") or target.get("share_pct"))


def target_ms_from_d3_brand_level(data: dict[str, Any], brand: str) -> float | None:
    level_payload = data.get("level_top5_trend", {}).get("by_level", {}).get("Brand") or {}
    for item in level_payload.get("values") or []:
        if item.get("value") == brand or item.get("is_target"):
            return as_float(item.get("ms_pct"))
    return None


def validate_cross_chart_consistency(
    *,
    brand: str,
    view: str,
    source: str,
    measure: str,
    data: dict[str, Any],
) -> list[ValidationIssue]:
    values = {
        "A.2": latest_target_ms_from_a2(data),
        "B.1": target_ms_from_rows(data.get("ei_ms_matrix", {}).get("data") or []),
        "B.2": target_ms_from_rows(data.get("growth_contribution_ms_matrix", {}).get("data") or []),
        "D.3.Brand": target_ms_from_d3_brand_level(data, brand),
        "KPI": as_float(data.get("kpi", {}).get("target_share_pct")),
    }
    comparable = {key: value for key, value in values.items() if value is not None}
    if len(comparable) < 2:
        return []
    delta = max(comparable.values()) - min(comparable.values())
    if delta <= CHART_TOLERANCE_PCT:
        return []
    return [
        ValidationIssue(
            kind="cross_chart_ms_mismatch",
            brand=brand,
            view=view,
            source=source,
            measure=measure,
            detail={"values": comparable, "delta_pct": round(delta, 4)},
        )
    ]


def d2_signature(view_payload: dict[str, Any]) -> tuple[tuple[str, float], ...]:
    composition = view_payload.get("composition") or []
    return tuple(
        (str(row.get("brand")), round(as_float(row.get("pct")) or 0.0, 4))
        for row in composition
    )


def validate_channel_filter(
    *,
    brand: str,
    view: str,
    source: str,
    measure: str,
    data: dict[str, Any],
) -> list[ValidationIssue]:
    d2 = data.get("target_customer_competition") or {}
    views = d2.get("views") or []
    signatures = {
        str(item.get("target_name")): d2_signature(item)
        for item in views
        if item.get("composition")
    }
    if len(signatures) <= 1:
        return []
    non_empty = {key: sig for key, sig in signatures.items() if sig}
    if len(non_empty) <= 1:
        return []
    if len(set(non_empty.values())) > 1:
        return []
    return [
        ValidationIssue(
            kind="d2_channel_filter_identical",
            brand=brand,
            view=view,
            source=source,
            measure=measure,
            detail={"channels": sorted(non_empty.keys())},
        )
    ]


def validate_d3_segments(
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
        if values and len(options) < len(values):
            issues.append(
                ValidationIssue(
                    kind="d3_options_incomplete",
                    brand=brand,
                    view=view,
                    source=source,
                    measure=measure,
                    detail={"level": level, "options": len(options), "values": len(values)},
                )
            )
        if level != "Class" or len(values) <= 1:
            continue
        totals = [round(as_float(item.get("total_value")) or 0.0, 2) for item in values]
        shares = [round(as_float(item.get("ms_pct")) or 0.0, 4) for item in values]
        if len(set(totals)) == 1 and len(set(shares)) > 1:
            issues.append(
                ValidationIssue(
                    kind="d3_class_total_repeated",
                    brand=brand,
                    view=view,
                    source=source,
                    measure=measure,
                    detail={"totals": totals, "shares": shares},
                )
            )
    return issues


def validate_d1_units(
    *,
    brand: str,
    view: str,
    source: str,
    measure: str,
    data: dict[str, Any],
) -> list[ValidationIssue]:
    if measure != "sales":
        return []
    top = data.get("growth_contribution", {}).get("by_brand", {}).get("top_contributors") or []
    issues: list[ValidationIssue] = []
    for row in top:
        contribution = as_float(row.get("contribution_value") or row.get("contribution"))
        start = as_float(row.get("value_start"))
        end = as_float(row.get("value_end"))
        if contribution is None or start is None or end is None:
            continue
        expected = end - start
        if abs(expected - contribution) > max(1.0, abs(expected) * 0.001):
            issues.append(
                ValidationIssue(
                    kind="d1_contribution_not_delta",
                    brand=brand,
                    view=view,
                    source=source,
                    measure=measure,
                    detail={
                        "row_brand": row.get("brand"),
                        "contribution": contribution,
                        "expected": expected,
                    },
                )
            )
        if 100 <= abs(contribution) < 10000 and abs(end) > 100_000_000:
            issues.append(
                ValidationIssue(
                    kind="d1_sales_unit_too_small",
                    brand=brand,
                    view=view,
                    source=source,
                    measure=measure,
                    detail={"row_brand": row.get("brand"), "contribution": contribution, "value_end": end},
                )
            )
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
    issues.extend(validate_cross_chart_consistency(brand=brand, view=view, source=source, measure=measure, data=data))
    issues.extend(validate_channel_filter(brand=brand, view=view, source=source, measure=measure, data=data))
    issues.extend(validate_d3_segments(brand=brand, view=view, source=source, measure=measure, data=data))
    issues.extend(validate_d1_units(brand=brand, view=view, source=source, measure=measure, data=data))
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--json-out")
    parser.add_argument("--fail-on-brand-count", type=int, default=25)
    args = parser.parse_args()

    brands = load_brands(args.base_url)
    issues: list[ValidationIssue] = []
    checked = 0
    skipped = 0

    if len(brands) != args.fail_on_brand_count:
        issues.append(
            ValidationIssue(
                kind="brand_count_mismatch",
                brand="*",
                detail={"expected": args.fail_on_brand_count, "actual": len(brands), "brands": brands},
            )
        )

    for brand in brands:
        for view in VIEWS:
            for source in SOURCES:
                for measure in MEASURES:
                    data = cause_payload(brand, view=view, source=source, measure=measure, base_url=args.base_url)
                    if not data:
                        skipped += 1
                        continue
                    checked += 1
                    issues.extend(
                        validate_payload(brand=brand, view=view, source=source, measure=measure, data=data)
                    )

    report = {
        "brands": len(brands),
        "planned_combinations": len(brands) * len(VIEWS) * len(SOURCES) * len(MEASURES),
        "checked_payloads": checked,
        "skipped_unsupported_or_empty": skipped,
        "issues": [issue.__dict__ for issue in issues],
    }

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fp:
            json.dump(report, fp, ensure_ascii=False, indent=2)

    print("=== Phase 23 consistency validation ===")
    print(f"brands={report['brands']}")
    print(f"planned_combinations={report['planned_combinations']}")
    print(f"checked_payloads={checked}")
    print(f"skipped_unsupported_or_empty={skipped}")
    print(f"issues={len(issues)}")
    for issue in issues[:30]:
        print(json.dumps(issue.__dict__, ensure_ascii=False, sort_keys=True))
    if len(issues) > 30:
        print(f"... {len(issues) - 30} more")

    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
