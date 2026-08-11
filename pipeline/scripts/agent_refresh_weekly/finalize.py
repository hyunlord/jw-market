"""Finalize weekly Agent2 manifests or attest reuse of the existing Agent3 artifact."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from pipeline.scripts.agent3.db import DbConfig, connect
from pipeline.scripts.agent_refresh_weekly.contract import weekly_verdict


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _write_object(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def finalize(root: Path, failure_threshold: int) -> dict[str, Any]:
    plan = _read_object(root / "worklist.json")
    route_by_key = {
        str(route["brand_key"]): route
        for route in plan.get("routes") or []
    }
    records: list[dict[str, Any]] = []
    variant_manifests: dict[str, dict[str, Any]] = {}
    for variant in ("short", "long"):
        manifest = _read_object(root / variant / "run_manifest.json")
        variant_manifests[variant] = manifest
        for brand_key, raw in sorted((manifest.get("brands") or {}).items()):
            record = dict(raw)
            route = route_by_key.get(str(brand_key)) or {}
            record.setdefault("brand", route.get("canonical_brand_name") or brand_key)
            record.setdefault("brand_key", brand_key)
            record.setdefault("cohort", route.get("cohort") or "nonstrategic")
            record["analysis_variant"] = variant
            records.append(record)

    verdict = weekly_verdict(records)
    failures = []
    for record in records:
        if record.get("status") != "failed":
            continue
        failures.append(
            {
                "analysis_variant": record["analysis_variant"],
                "brand": str(record.get("brand") or record.get("brand_key") or ""),
                "brand_key": str(record.get("brand_key") or ""),
                "cohort": str(record.get("cohort") or "nonstrategic"),
                "failure_type": str(record.get("reason") or "unknown"),
                "reason": record.get("detail") or record.get("reason") or "unknown",
            }
        )
    failures.sort(key=lambda item: (item["analysis_variant"], item["cohort"], item["brand_key"]))
    result = {
        **verdict,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "failure_threshold": failure_threshold,
        "threshold_is_reporting_only": True,
        "worklist_brand_count": len(route_by_key),
        "excluded_non_jw_market": (
            ((plan.get("diagnostics") or {}).get("density_worklist") or {}).get("excluded")
            or []
        ),
        "aliases": (
            ((plan.get("diagnostics") or {}).get("density_worklist") or {}).get("aliases")
            or []
        ),
        "failures": failures,
        "cohort_metrics": {
            variant: manifest.get("cohort_metrics") or {}
            for variant, manifest in variant_manifests.items()
        },
    }
    _write_object(root / "weekly_verdict.json", result)
    return result


def attest_agent3_reuse(root: Path) -> dict[str, Any]:
    config = DbConfig.from_env()
    with connect(config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS row_count, MAX(generated_at) AS latest_generated_at
                FROM agent3_brand_strength
                """
            )
            row = cursor.fetchone() or {}
    row_count = int(row.get("row_count") or 0)
    if row_count <= 0:
        raise RuntimeError("existing agent3_brand_strength artifact is empty")
    result = {
        "status": "reused_existing_artifact",
        "recomputation": 0,
        "table": "agent3_brand_strength",
        "row_count": row_count,
        "latest_generated_at": str(row.get("latest_generated_at")),
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_object(root / "agent3_reuse.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--failure-threshold", type=int, default=5)
    parser.add_argument("--reuse-agent3", action="store_true")
    args = parser.parse_args(argv)
    result = (
        attest_agent3_reuse(args.root)
        if args.reuse_agent3
        else finalize(args.root, args.failure_threshold)
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
