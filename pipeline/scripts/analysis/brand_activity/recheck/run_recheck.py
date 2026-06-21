#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))

from pipeline.scripts.analysis.brand_activity.recheck.execution import (  # noqa: E402
    loader_environment,
    run_legacy_loaders,
    stage_snapshot,
)
from pipeline.scripts.analysis.brand_activity.recheck.inventory import (  # noqa: E402
    compare_manifests,
    load_manifest_records,
    month_coverage,
    scan_source_roots,
    write_records,
)
from pipeline.scripts.analysis.brand_activity.recheck.reports import render_reports  # noqa: E402
from pipeline.scripts.analysis.brand_activity.recheck.sanitization import sanitize_shareable_outputs  # noqa: E402
from pipeline.scripts.analysis.brand_activity.recheck.safety import require_stage_schema  # noqa: E402
from pipeline.scripts.analysis.brand_activity.recheck.summaries import (  # noqa: E402
    broken_csd_assumptions,
    broken_km_assumptions,
    baseline_from_previous_artifacts,
    csd_header_summary,
    duplicate_file_names,
    input_completeness_failures,
    new_enum_values,
    one_month_file_violations,
    product_variant_hits,
    read_json,
    selected_root_by_kind,
    table_delta_rows,
    top_class_counts,
    write_json,
)


JsonObject = dict[str, Any]
PREVIOUS_ROWS = {"csd": 22016, "keyword": 9512, "meeting": 757}


def parse_args() -> argparse.Namespace:
    """Parse the local-only recheck runner arguments."""
    parser = argparse.ArgumentParser(description="Run Brand Activity CSD/Keyword/Meeting recheck.")
    parser.add_argument("--repo-csd-root", type=Path, default=Path("data/IQVIA/CSD"))
    parser.add_argument("--downloads-csd-root", type=Path, default=Path("~/Downloads/IQVIA/CSD"))
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--docs-dir", type=Path, required=True)
    parser.add_argument("--stage-schema", default="jw_brand_activity_stage")
    parser.add_argument("--db-host", default="127.0.0.1")
    parser.add_argument("--db-port", type=int, default=3308)
    parser.add_argument("--db-user", default="root")
    parser.add_argument("--db-password-env", default="MARIADB_ROOT_PASSWORD")
    parser.add_argument("--db-load", action="store_true")
    return parser.parse_args()


def db_snapshot(args: argparse.Namespace, env: dict[str, str]) -> JsonObject:
    """Capture a small DB snapshot using credentials kept out of artifacts."""
    return stage_snapshot(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=env.get(args.db_password_env, ""),
        schema=args.stage_schema,
    )


def build_payload(args: argparse.Namespace) -> JsonObject:
    """Run discovery, legacy isolated reloads, and validation synthesis."""
    require_stage_schema(args.stage_schema)
    args.audit_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.docs_dir.mkdir(parents=True, exist_ok=True)
    env = loader_environment()
    previous_csd_path = Path("audit/brand_activity_csd/csd_ingest_validation.json")
    previous_km_path = Path("audit/brand_activity_keyword_meeting/km_ingest_validation.json")
    roots_to_scan = [args.downloads_csd_root.expanduser(), args.repo_csd_root]
    current_records, missing_roots = scan_source_roots(roots_to_scan)
    previous_records = [
        *load_manifest_records(Path("audit/brand_activity_csd/source_sha256_manifest.json")),
        *load_manifest_records(Path("audit/brand_activity_keyword_meeting/source_sha256_manifest.json")),
    ]
    diff = compare_manifests(previous_records, current_records)
    coverage = month_coverage(current_records)
    loader_roots = selected_root_by_kind(current_records)
    write_records(args.audit_dir / "input_sha256_manifest.json", current_records)
    db_before = db_snapshot(args, env)
    if args.db_load:
        run_legacy_loaders(args.audit_dir, args.output_dir, loader_roots, coverage, args.stage_schema, env)
    db_after = db_snapshot(args, env)
    sanitization = sanitize_shareable_outputs(args.audit_dir, args.output_dir, args.stage_schema)
    csd_validation = read_json(args.audit_dir / "load_csd" / "csd_ingest_validation.json")
    km_validation = read_json(args.audit_dir / "load_km" / "km_ingest_validation.json")
    previous_km = read_json(previous_km_path)
    header_summary = csd_header_summary([record for record in current_records if record.kind == "csd"])
    enum_additions = new_enum_values(previous_km, km_validation)
    current_product_variants = product_variant_hits(db_after["low_osmo_peri_variants"])
    broken_csd = broken_csd_assumptions(header_summary, csd_validation)
    broken_km = broken_km_assumptions(km_validation, enum_additions)
    input_failures = input_completeness_failures(missing_roots)
    decision_items = pl_decision_items(coverage, current_records, current_product_variants, missing_roots)
    return {
        "scan_roots": [str(path) for path in roots_to_scan],
        "missing_roots": missing_roots,
        "selected_loader_roots": loader_roots,
        "current_records": current_records,
        "duplicate_file_names": duplicate_file_names(current_records),
        "month_coverage": coverage,
        "manifest_diff": diff,
        "manifest_diff_counts": {key: len(value) for key, value in diff.items()},
        "db_before": db_before,
        "db_after": db_after,
        "baseline_from_prior_artifacts": baseline_from_previous_artifacts(previous_csd_path, previous_km_path),
        "csd_header_summary": header_summary,
        "csd_validation": csd_validation,
        "km_validation": km_validation,
        "shareable_sanitization": sanitization,
        "km_one_month_violations": {
            "keyword": one_month_file_violations(km_validation["file_period_distribution"]["keyword"]),
            "meeting": one_month_file_violations(km_validation["file_period_distribution"]["meeting"]),
        },
        "new_enum_values": enum_additions,
        "top_class_counts": {
            "keyword": top_class_counts(km_validation["class_month_summary"], "keyword", 10),
            "meeting": top_class_counts(km_validation["class_month_summary"], "meeting", 10),
        },
        "pl_product_variants": current_product_variants,
        "broken_csd_assumptions": broken_csd,
        "broken_km_assumptions": broken_km,
        "input_completeness_failures": input_failures,
        "broken_assumptions": [*input_failures, *broken_csd, *broken_km],
        "pl_decision_items": decision_items,
        "table_delta_rows": table_delta_rows(db_after, PREVIOUS_ROWS),
    }


def pl_decision_items(coverage: dict[str, list[str]], records: list[Any], variants: list[str], missing_roots: list[str]) -> list[str]:
    """Summarize unresolved PL decisions surfaced by the recheck."""
    items: list[str] = []
    if missing_roots:
        items.append(f"Downloads 원본 루트 부재 확인 필요: {missing_roots}")
    if variants:
        items.append("LOWOSMOPERI / LOW OSMO PERI 제품명 정규화 기준 확정 필요")
    if coverage.get("csd") and "2025-09" not in coverage["csd"]:
        items.append("CSD 2025-09 source file 부재를 의도된 누락으로 볼지 확인 필요")
    duplicates = duplicate_file_names(records)
    if duplicates:
        items.append(f"동명 파일 중복 스캔 루트 확인 필요: {duplicates}")
    return items


def json_ready(payload: JsonObject) -> JsonObject:
    """Convert dataclass-heavy payload fields to deterministic JSON values."""
    converted = dict(payload)
    converted["current_records"] = [record.to_json() for record in payload["current_records"]]
    converted["manifest_diff"] = {
        key: [record.to_json() for record in rows]
        for key, rows in payload["manifest_diff"].items()
    }
    return converted


def main() -> int:
    """Run source discovery, isolated reload, validation synthesis, and report writing."""
    args = parse_args()
    payload = build_payload(args)
    write_json(args.audit_dir / "recheck_summary.json", json_ready(payload))
    render_reports(args.docs_dir, payload)
    print(json.dumps({"docs_dir": str(args.docs_dir), "audit_dir": str(args.audit_dir), "output_dir": str(args.output_dir)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
