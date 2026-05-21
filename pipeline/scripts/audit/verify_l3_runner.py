#!/usr/bin/env python3
"""Run all Layer 3 verification checks and write the audit summary."""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops_utils import find_project_root

try:
    from .verify_l3_general import verify_l3_general, write_result as write_general_result
    from .verify_l3_strategic import verify_l3_strategic, write_result as write_strategic_result
    from .verify_l3_target_customer import verify_target_customer, write_result as write_target_result
except ImportError:
    from verify_l3_general import verify_l3_general, write_result as write_general_result
    from verify_l3_strategic import verify_l3_strategic, write_result as write_strategic_result
    from verify_l3_target_customer import verify_target_customer, write_result as write_target_result


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
AUDIT_DIR = PROJECT_ROOT / "docs" / "audit" / "phase_16g4_side_verify_l3"


def status_counts(checks: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(check.get("status", "INFO")) for check in checks).items()))


def compact_detail(check: dict[str, Any], max_len: int = 260) -> str:
    excluded = {
        "name",
        "status",
        "distribution",
        "samples",
        "duplicate_samples",
        "mismatch_samples",
        "violation_samples",
        "detail_samples",
        "catalog_not_in_mart_samples",
        "mart_not_in_catalog_samples",
        "jw_brand_samples",
        "measure_distribution",
        "overall_distribution",
        "ml_by_market",
        "cd_by_market",
        "note",
    }
    detail = ", ".join(f"{key}={value}" for key, value in check.items() if key not in excluded)
    return detail if len(detail) <= max_len else detail[: max_len - 3] + "..."


def write_section(lines: list[str], title: str, result: dict[str, Any]) -> None:
    checks = result.get("checks", [])
    counts = status_counts(checks)
    lines.extend([f"## {title}", "", "| Status | Count |", "|---|---:|"])
    for status, count in counts.items():
        lines.append(f"| {status} | {count} |")
    lines.extend(["", "| Check | Status | Detail |", "|---|---|---|"])
    for check in checks:
        lines.append(f"| {check['name']} | {check.get('status', 'INFO')} | {compact_detail(check)} |")
    lines.append("")


def append_findings(lines: list[str], title: str, result: dict[str, Any]) -> None:
    findings = [check for check in result.get("checks", []) if check.get("status") in {"WARN", "FAIL"}]
    if not findings:
        lines.append(f"- {title}: No WARN/FAIL checks.")
        return
    for check in findings:
        lines.append(f"### [{check.get('status')}] {title} / {check['name']}")
        lines.append("")
        lines.append(f"- detail: {compact_detail(check, max_len=800)}")
        for key in (
            "duplicate_samples",
            "mismatch_samples",
            "violation_samples",
            "catalog_not_in_mart_samples",
            "mart_not_in_catalog_samples",
            "missing_field_samples",
            "out_of_range_samples",
        ):
            if check.get(key):
                lines.append(f"- {key}: {json.dumps(check[key][:5], ensure_ascii=False, default=str)}")
        if check.get("note"):
            lines.append(f"- note: {check['note']}")
        lines.append("")


def build_summary(general: dict[str, Any], strategic: dict[str, Any], target: dict[str, Any]) -> str:
    lines: list[str] = [
        "# 00. Layer 3 Verification Summary",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Scope",
        "",
        "- Read-only verification of 6 Layer 3 marts.",
        "- CHSO checks intentionally excluded because L3 mart does not include CHSO.",
        "- Specialty Unknown mapping checks intentionally excluded because S2 stores raw Korean labels directly.",
        "- General view follows Direction B: Layer 1 raw direct, Layer 2 bypass.",
        "",
    ]
    write_section(lines, "General View", general)
    write_section(lines, "Strategic View", strategic)
    write_section(lines, "Target Customer Competition", target)

    lines.extend(["## Key Distributions", ""])
    lines.append("### General Measure Distribution")
    lines.extend(["", "| source | measure | rows | distinct brands | distinct ATC4 |", "|---|---|---:|---:|---:|"])
    for row in general.get("measure_distribution", []):
        lines.append(
            f"| {row['source']} | {row['measure']} | {int(row['cnt']):,} | "
            f"{int(row['distinct_brand_keys']):,} | {int(row['distinct_atc4']):,} |"
        )
    lines.append("")

    lines.append("### General catalog_status")
    lines.extend(["", "| source | catalog_status | rows |", "|---|---|---:|"])
    for row in general.get("catalog_status_distribution", []):
        lines.append(f"| {row['source']} | {row['catalog_status']} | {int(row['cnt']):,} |")
    lines.append("")

    lines.append("### Target Customer source_type")
    lines.extend(["", "| source_type | rows |", "|---|---:|"])
    for source_type, count in sorted(target.get("source_type_distribution", {}).items()):
        lines.append(f"| {source_type} | {count:,} |")
    lines.append("")

    if strategic.get("catalog_inventory"):
        lines.append("### Catalog Inventory")
        lines.extend(["", "| catalog | rows |", "|---|---:|"])
        for name, count in strategic["catalog_inventory"].items():
            lines.append(f"| {name} | {count:,} |")
        lines.append("")

    lines.extend(["## WARN / FAIL Findings", ""])
    append_findings(lines, "General View", general)
    append_findings(lines, "Strategic View", strategic)
    append_findings(lines, "Target Customer Competition", target)

    lines.extend(
        [
            "",
            "## Read-only Statement",
            "",
            "- This phase executed MariaDB SELECT statements and catalog Parquet reads only.",
            "- No DB UPDATE/INSERT/DELETE was executed.",
            "- No mart/cache/response_store/catalog/migration/ETL writes were performed.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_summary(general: dict[str, Any], strategic: dict[str, Any], target: dict[str, Any]) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIT_DIR / "00_summary.md"
    path.write_text(build_summary(general, strategic, target), encoding="utf-8")
    return path


def run_all() -> dict[str, Path]:
    print("=== Phase 16-G-4-Side-Verify-L3 ===")
    print("Running general mart verification...")
    general = verify_l3_general()
    general_path = write_general_result(general)
    print(f"General audit: {general_path.relative_to(PROJECT_ROOT)}")

    print("Running strategic mart verification...")
    strategic = verify_l3_strategic()
    strategic_path = write_strategic_result(strategic)
    print(f"Strategic audit: {strategic_path.relative_to(PROJECT_ROOT)}")

    print("Running target_customer_competition verification...")
    target = verify_target_customer()
    target_path = write_target_result(target)
    print(f"Target customer audit: {target_path.relative_to(PROJECT_ROOT)}")

    summary_path = write_summary(general, strategic, target)
    print(f"Summary audit: {summary_path.relative_to(PROJECT_ROOT)}")
    return {
        "summary": summary_path,
        "general": general_path,
        "strategic": strategic_path,
        "target_customer": target_path,
    }


def main() -> int:
    run_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
