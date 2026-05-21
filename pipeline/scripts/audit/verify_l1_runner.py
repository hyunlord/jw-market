#!/usr/bin/env python3
"""Run Layer 1 raw verification and write a human-readable audit summary."""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops_utils import find_project_root

try:
    from .verify_l1_iqvia import verify_iqvia, write_result as write_iqvia_result
    from .verify_l1_ubist import verify_ubist, write_result as write_ubist_result
except ImportError:
    from verify_l1_iqvia import verify_iqvia, write_result as write_iqvia_result
    from verify_l1_ubist import verify_ubist, write_result as write_ubist_result


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
AUDIT_DIR = PROJECT_ROOT / "docs" / "audit" / "phase_16g4_side_verify_l1"


def summarize_status_counts(checks: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(check.get("status", "INFO")) for check in checks)
    return dict(sorted(counts.items()))


def compact_detail(check: dict[str, Any], max_len: int = 260) -> str:
    excluded = {
        "name",
        "status",
        "distribution",
        "static_keys_observed",
        "rows_by_period",
        "missing_periods",
        "extra_periods",
        "missing",
        "extra",
    }
    parts = [f"{key}={value}" for key, value in check.items() if key not in excluded]
    detail = ", ".join(parts)
    return detail if len(detail) <= max_len else detail[: max_len - 3] + "..."


def write_check_table(lines: list[str], checks: list[dict[str, Any]]) -> None:
    lines.append("| Check | Status | Detail |")
    lines.append("|---|---|---|")
    for check in checks:
        lines.append(f"| {check['name']} | {check.get('status', 'INFO')} | {compact_detail(check)} |")


def append_brand_counts(lines: list[str], title: str, counts: dict[str, int]) -> None:
    lines.append(f"### {title}")
    lines.append("")
    lines.append("| Brand | Rows |")
    lines.append("|---|---:|")
    for brand, count in counts.items():
        lines.append(f"| {brand} | {count:,} |")
    lines.append("")


def append_findings(lines: list[str], result: dict[str, Any]) -> None:
    warnings = [check for check in result["checks"] if check.get("status") in {"WARN", "FAIL"}]
    if not warnings:
        lines.append(f"- {result['layer']}: No WARN/FAIL checks.")
        return
    for check in warnings:
        lines.append(f"- [{check.get('status')}] {result['layer']} / {check['name']}: {compact_detail(check)}")
        for key in ("missing_periods", "extra_periods", "missing", "extra"):
            if check.get(key):
                lines.append(f"  - {key}: {check[key]}")


def build_summary(ubist_result: dict[str, Any], iqvia_result: dict[str, Any]) -> str:
    generated_at = datetime.now().isoformat(timespec="seconds")
    lines: list[str] = [
        "# 00. Layer 1 Verification Summary",
        "",
        f"Generated: {generated_at}",
        "",
        "## Overall",
        "",
        "| Source | Status Counts |",
        "|---|---|",
        f"| UBIST raw | {json.dumps(summarize_status_counts(ubist_result['checks']), ensure_ascii=False)} |",
        f"| IQVIA NSA raw | {json.dumps(summarize_status_counts(iqvia_result['checks']), ensure_ascii=False)} |",
        "",
        "## UBIST raw",
        "",
    ]
    write_check_table(lines, ubist_result["checks"])
    lines.extend(
        [
            "",
            "### UBIST Partition Coverage",
            "",
            f"- Total partitions: {len(ubist_result['partition_breakdown'])}",
            f"- First period: {ubist_result['partition_breakdown'][0]['period']}",
            f"- Last period: {ubist_result['partition_breakdown'][-1]['period']}",
            f"- Requested source root exists: {ubist_result['source_inventory']['requested_source_root_exists']}",
            f"- Actual source root: {ubist_result['source_inventory']['actual_source_root']}",
            "",
        ]
    )
    append_brand_counts(lines, "JW brand row counts (UBIST)", ubist_result["jw_brand_row_counts"])
    lines.extend(["## IQVIA NSA raw", ""])
    write_check_table(lines, iqvia_result["checks"])
    lines.extend(
        [
            "",
            "### IQVIA Source Coverage",
            "",
            f"- Source file counts: {json.dumps(iqvia_result['source_file_counts'], ensure_ascii=False)}",
            f"- Requested source root exists: {iqvia_result['source_inventory']['requested_source_root_exists']}",
            f"- Actual source root: {iqvia_result['source_inventory']['actual_source_root']}",
            "",
        ]
    )
    append_brand_counts(lines, "JW brand row counts (IQVIA)", iqvia_result["jw_brand_row_counts"])
    lines.extend(["## External Source Cross-check", ""])
    for result in (ubist_result, iqvia_result):
        cross_check = result.get("external_cross_check")
        if cross_check:
            lines.append(f"- {cross_check['name']}: {cross_check.get('status', 'INFO')}")
            for key in ("source_path", "external_rows", "layer1_rows", "months_observed", "source_period_count", "layer1_to_external_ratio", "note"):
                if key in cross_check:
                    lines.append(f"  - {key}: {cross_check[key]}")
    lines.extend(["", "## WARN / FAIL Findings", ""])
    append_findings(lines, ubist_result)
    append_findings(lines, iqvia_result)
    lines.extend(
        [
            "",
            "## Read-only Statement",
            "",
            "- This phase executed Parquet reads, source file reads, and MariaDB SELECT statements only.",
            "- No mart/cache/ETL/migration/catalog/response_store writes were performed.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_summary(ubist_result: dict[str, Any], iqvia_result: dict[str, Any]) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = AUDIT_DIR / "00_summary.md"
    summary_path.write_text(build_summary(ubist_result, iqvia_result), encoding="utf-8")
    return summary_path


def run_all() -> dict[str, Path]:
    print("=== Phase 16-G-4-Side-Verify-L1 ===")
    print("Running UBIST verification...")
    ubist_result = verify_ubist()
    ubist_path = write_ubist_result(ubist_result)
    print(f"UBIST audit: {ubist_path.relative_to(PROJECT_ROOT)}")

    print("Running IQVIA verification...")
    iqvia_result = verify_iqvia()
    iqvia_path = write_iqvia_result(iqvia_result)
    print(f"IQVIA audit: {iqvia_path.relative_to(PROJECT_ROOT)}")

    summary_path = write_summary(ubist_result, iqvia_result)
    print(f"Summary audit: {summary_path.relative_to(PROJECT_ROOT)}")
    return {"summary": summary_path, "ubist": ubist_path, "iqvia": iqvia_path}


def main() -> int:
    run_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
