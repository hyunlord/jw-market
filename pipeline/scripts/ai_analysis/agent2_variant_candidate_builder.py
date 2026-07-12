from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import pymysql

from agent2_variant_contract import VariantContractError, validate_variant_payload


STAGES = ("phenomenon", "cause", "prediction", "recommendation")
ZERO_MODEL = "deterministic-template-zero"
LLM_MODEL = "genos_workflow_217"


def _connect(args: argparse.Namespace) -> Any:
    return pymysql.connect(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=os.environ.get("DB_PASSWORD", args.db_password),
        database=args.db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _chunks(values: list[str], size: int = 400) -> Iterable[list[str]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value.get("rows") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise ValueError("route manifest must contain a rows array")
    keys = [str(row.get("brand_key") or "") for row in rows]
    if len(rows) != 24_789 or len(set(keys)) != len(rows) or any(not key for key in keys):
        raise ValueError("route manifest must contain 24,789 unique non-empty brand keys")
    return rows


def _select_runs(conn: Any, brands: list[str], variant: str, zero: bool) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for brand_chunk in _chunks(brands):
        placeholders = ",".join(["%s"] * len(brand_chunk))
        model = ZERO_MODEL if zero else LLM_MODEL
        variant_filter = "legacy" if zero else variant
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT r.run_id, r.brand, r.snapshot_at, r.bundle_hash, r.model_version,
                       r.created_at, r.analysis_variant
                FROM zeta_analysis_runs r
                JOIN (
                    SELECT zr.brand, MAX(zr.run_id) AS run_id
                    FROM zeta_analysis_runs zr
                    JOIN zeta_analysis_outputs zo ON zo.run_id = zr.run_id
                    WHERE zr.brand IN ({placeholders})
                      AND zr.model_version = %s
                      AND zr.analysis_variant = %s
                      AND zr.status = 'ok'
                      AND zo.validated = 1
                    GROUP BY zr.brand, zr.run_id
                    HAVING COUNT(DISTINCT zo.stage) = 4
                ) latest ON latest.run_id = r.run_id
                ORDER BY r.brand, r.run_id DESC
                """,
                [*brand_chunk, model, variant_filter],
            )
            for row in cursor.fetchall():
                selected.setdefault(str(row["brand"]), row)
    return selected


def _load_payload(conn: Any, run: Mapping[str, Any], variant: str) -> dict[str, Any]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT stage, title, body, bullets FROM zeta_analysis_outputs WHERE run_id=%s AND validated=1",
            (run["run_id"],),
        )
        rows = cursor.fetchall()
    payload: dict[str, Any] = {
        "analysis_variant": variant,
        "generated_at": _iso(run["created_at"]),
        "model_version": run["model_version"],
        "run_id_phase_zeta": int(run["run_id"]),
    }
    for row in rows:
        stage = str(row["stage"])
        if stage not in STAGES:
            continue
        bullets = row.get("bullets") or "[]"
        if isinstance(bullets, str):
            bullets = json.loads(bullets)
        payload[stage] = {"title": row.get("title") or "", "body": row.get("body") or "", "bullets": bullets}
    validate_variant_payload(payload, variant)
    return payload


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _lineage(run: Mapping[str, Any], deterministic: bool) -> dict[str, Any]:
    bundle_hash = str(run.get("bundle_hash") or "")
    input_hash = bundle_hash.removeprefix("sha256:")
    if len(input_hash) != 64:
        input_hash = hashlib.sha256(bundle_hash.encode()).hexdigest()
    return {
        "workflow_id": None if deterministic else 217,
        "workflow_revision_id": None if deterministic else 3727,
        "generation_id": f"zeta-run-{run['run_id']}",
        "input_hash": input_hash,
        "generated_at": _iso(run["created_at"]),
        "source_epoch": _iso(run["snapshot_at"]),
        "generation_status": "complete",
        "deterministic": deterministic,
    }


def build_rows(conn: Any, manifest_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    zero_rows = [row for row in manifest_rows if row.get("mode") == "template_zero"]
    llm_rows = [row for row in manifest_rows if row.get("mode") != "template_zero"]
    zero_runs = _select_runs(conn, [str(row["canonical_brand_name"]) for row in zero_rows], "legacy", True)
    short_runs = _select_runs(conn, [str(row["canonical_brand_name"]) for row in llm_rows], "short", False)
    long_runs = _select_runs(conn, [str(row["canonical_brand_name"]) for row in llm_rows], "long", False)
    output: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for row in manifest_rows:
        brand = str(row["canonical_brand_name"])
        deterministic = row.get("mode") == "template_zero"
        short_run = zero_runs.get(brand) if deterministic else short_runs.get(brand)
        long_run = zero_runs.get(brand) if deterministic else long_runs.get(brand)
        if short_run is None or long_run is None:
            missing.append(
                {
                    "brand_key": str(row["brand_key"]),
                    "brand": brand,
                    "missing": "|".join(name for name, run in (("short", short_run), ("long", long_run)) if run is None),
                }
            )
            continue
        try:
            output.append(
                {
                    "brand": brand,
                    "brand_key": str(row["brand_key"]),
                    "market_id": None,
                    "short": {"payload": _load_payload(conn, short_run, "short"), "lineage": _lineage(short_run, deterministic)},
                    "long": {"payload": _load_payload(conn, long_run, "long"), "lineage": _lineage(long_run, deterministic)},
                }
            )
        except (VariantContractError, json.JSONDecodeError) as exc:
            missing.append({"brand_key": str(row["brand_key"]), "brand": brand, "missing": f"invalid:{exc}"})
    return output, missing


def main() -> int:
    parser = argparse.ArgumentParser(description="Build fail-closed Agent2 short/long promotion rows from validated trace")
    parser.add_argument("--route-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--missing-output", type=Path, required=True)
    parser.add_argument("--db-host", default="127.0.0.1")
    parser.add_argument("--db-port", type=int, default=3306)
    parser.add_argument("--db-user", default="root")
    parser.add_argument("--db-password", default="")
    parser.add_argument("--db-name", default="jw_mart_d2_stage_20260630_r2")
    args = parser.parse_args()
    rows = _load_manifest(args.route_manifest)
    conn = _connect(args)
    try:
        output, missing = build_rows(conn, rows)
    finally:
        conn.close()
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in output), encoding="utf-8")
    args.missing_output.write_text(json.dumps(missing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"route_count": len(rows), "built": len(output), "missing": len(missing)}, ensure_ascii=False))
    return 0 if not missing else 2


if __name__ == "__main__":
    raise SystemExit(main())
