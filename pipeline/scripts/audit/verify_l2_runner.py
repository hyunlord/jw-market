#!/usr/bin/env python3
"""Run Layer 2 verification and write a concise Markdown audit summary."""

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
    from .verify_l2_enriched import verify_l2, write_result as write_l2_result
except ImportError:
    from verify_l2_enriched import verify_l2, write_result as write_l2_result


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
AUDIT_DIR = PROJECT_ROOT / "docs" / "audit" / "phase_16g4_side_verify_l2"


def summarize_status_counts(checks: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(check.get("status", "INFO")) for check in checks)
    return dict(sorted(counts.items()))


def compact_detail(check: dict[str, Any], max_len: int = 280) -> str:
    excluded = {
        "name",
        "status",
        "source_results",
        "observed",
        "unexpected",
        "sample_l2_not_in_catalog",
        "missing_partitions",
        "warn_brands",
    }
    parts = [f"{key}={value}" for key, value in check.items() if key not in excluded]
    detail = ", ".join(parts)
    return detail if len(detail) <= max_len else detail[: max_len - 3] + "..."


def write_check_table(lines: list[str], checks: list[dict[str, Any]]) -> None:
    lines.append("| Check | Status | Detail |")
    lines.append("|---|---|---|")
    for check in checks:
        lines.append(f"| {check['name']} | {check.get('status', 'INFO')} | {compact_detail(check)} |")


def append_distribution(lines: list[str], title: str, distribution: dict[str, int]) -> None:
    lines.extend([f"## {title}", "", "| Value | Rows |", "|---|---:|"])
    for value, rows in sorted(distribution.items(), key=lambda item: -item[1]):
        display = value if value != "" else "(blank)"
        lines.append(f"| {display} | {rows:,} |")
    lines.append("")


def append_warn_fail(lines: list[str], checks: list[dict[str, Any]]) -> None:
    findings = [check for check in checks if check.get("status") in {"WARN", "FAIL"}]
    if not findings:
        lines.append("- No WARN/FAIL checks.")
        return
    for check in findings:
        lines.append(f"- [{check.get('status')}] {check['name']}: {compact_detail(check)}")
        for key in ("missing_partitions", "unexpected", "warn_brands", "sample_l2_not_in_catalog"):
            if check.get(key):
                lines.append(f"  - {key}: {check[key]}")


def build_summary(result: dict[str, Any]) -> str:
    generated_at = datetime.now().isoformat(timespec="seconds")
    lines: list[str] = [
        "# 00. Layer 2 Verification Summary",
        "",
        f"Generated: {generated_at}",
        "",
        "## Overall",
        "",
        "| Status | Count |",
        "|---|---:|",
    ]
    for status, count in summarize_status_counts(result["checks"]).items():
        lines.append(f"| {status} | {count} |")
    lines.extend(["", "## Check Results", ""])
    write_check_table(lines, result["checks"])

    lines.extend(["", "## Partition Breakdown", "", "| ml_id | exists | rows | size_MB |", "|---|---|---:|---:|"])
    for ml_id, info in result["partition_breakdown"].items():
        lines.append(f"| {ml_id} | {info['exists']} | {info.get('rows', 0):,} | {info.get('size_mb', '-')} |")
    lines.append("")

    lines.extend(["## Source by Partition", "", "| ml_id | source | rows |", "|---|---|---:|"])
    for row in result.get("source_by_partition", []):
        lines.append(f"| {row['ml_id']} | {row['source']} | {row['rows']:,} |")
    lines.append("")

    append_distribution(lines, "Channel Distribution", result.get("channel_distribution", {}))
    append_distribution(lines, "Specialty Distribution", result.get("specialty_distribution", {}))

    lines.extend(
        [
            "## JW Brand Tracking",
            "",
            "| Brand | In catalog | brand_ids | product_ids | L2 rows | Match basis | Sources |",
            "|---|---|---:|---:|---:|---|---|",
        ]
    )
    for brand, info in result.get("jw_brand_tracking", {}).items():
        sources = json.dumps(info.get("sources", {}), ensure_ascii=False)
        lines.append(
            f"| {brand} | {info.get('in_catalog')} | {info.get('brand_ids_count', 0)} | "
            f"{info.get('product_ids_count', 0)} | {info.get('l2_rows', 0):,} | "
            f"{info.get('match_basis', '')} | {sources} |"
        )
    lines.append("")

    lines.extend(["## WARN / FAIL Findings", ""])
    append_warn_fail(lines, result["checks"])
    lines.extend(
        [
            "",
            "## Read-only Statement",
            "",
            "- This phase executed Parquet reads and aggregate scans only.",
            "- No DB/mart/cache/ETL/migration/catalog/response_store writes were performed.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_summary(result: dict[str, Any]) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = AUDIT_DIR / "00_summary.md"
    summary_path.write_text(build_summary(result), encoding="utf-8")
    return summary_path


def run_all() -> dict[str, Path]:
    print("=== Phase 16-G-4-Side-Verify-L2 ===")
    print("Running Layer 2 enriched verification...")
    result = verify_l2()
    json_path = write_l2_result(result)
    summary_path = write_summary(result)
    print(f"L2 audit: {json_path.relative_to(PROJECT_ROOT)}")
    print(f"Summary audit: {summary_path.relative_to(PROJECT_ROOT)}")
    return {"json": json_path, "summary": summary_path}


def main() -> int:
    run_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
