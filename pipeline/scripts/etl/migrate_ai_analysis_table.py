#!/usr/bin/env python3
"""Create and optionally seed the dedicated Phase zeta ai_analysis cache table.

Phase 30.3 splits ownership:
* cache_deep_analysis.response_json is rebuilt by jw-market-test cache ETL.
* cache_deep_analysis_ai_analysis.ai_analysis_json is owned by Phase zeta.

This script is intentionally idempotent. If all 25 JW brands already have a
phase_zeta_stage marker in the dedicated table, it reports that state and skips
row updates unless --force is supplied.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pymysql

try:
    from pipeline.scripts.etl.cache_build_common import CANONICAL_25
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from pipeline.scripts.etl.cache_build_common import CANONICAL_25


TARGET_TABLE = "cache_deep_analysis_ai_analysis"
STAGES = ("phenomenon", "cause", "prediction", "recommendation")
STAGE_MARKER = "stage3a7"
CREATE_TABLE_SQL = Path(__file__).with_name("schema").joinpath("create_ai_analysis_table.sql").read_text(encoding="utf-8")
JW25 = sorted(CANONICAL_25)


@dataclass(frozen=True)
class SelectedRun:
    brand: str
    run_id: int
    status: str
    model_version: str
    created_at: Any
    bundle_hash: str


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def connect(args: argparse.Namespace) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=os.getenv("DB_ROOT_PASSWORD", args.db_password),
        database=args.db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def ensure_table(conn: pymysql.connections.Connection) -> dict[str, Any]:
    with conn.cursor() as cursor:
        cursor.execute(f"SHOW TABLES LIKE %s", (TARGET_TABLE,))
        existed_before = cursor.fetchone() is not None
        cursor.execute(CREATE_TABLE_SQL)
        cursor.execute(f"DESCRIBE {TARGET_TABLE}")
        describe = cursor.fetchall()
        cursor.execute(f"SELECT COUNT(*) AS row_count FROM {TARGET_TABLE}")
        row_count = int(cursor.fetchone()["row_count"])
    conn.commit()
    return {"existed_before": existed_before, "row_count": row_count, "describe": describe}


def existing_stage_rows(conn: pymysql.connections.Connection) -> dict[str, dict[str, Any]]:
    placeholders = ",".join(["%s"] * len(JW25))
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT brand,
                   market_id,
                   JSON_UNQUOTE(JSON_EXTRACT(ai_analysis_json, '$.phase_zeta_stage')) AS stage,
                   JSON_LENGTH(ai_analysis_json) AS analysis_size,
                   updated_at
            FROM {TARGET_TABLE}
            WHERE brand IN ({placeholders})
            """,
            JW25,
        )
        return {str(row["brand"]): row for row in cursor.fetchall()}


def select_latest_runs(conn: pymysql.connections.Connection) -> dict[str, SelectedRun]:
    placeholders = ",".join(["%s"] * len(JW25))
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT run_id, brand, status, model_version, created_at, bundle_hash
            FROM zeta_analysis_runs
            WHERE brand IN ({placeholders})
              AND model_version = 'genos_workflow_217'
              AND created_at >= '2026-05-25 00:00:00'
              AND status IN ('ok', 'partial')
            ORDER BY brand, run_id DESC
            """,
            JW25,
        )
        rows = cursor.fetchall()

    selected: dict[str, SelectedRun] = {}
    for row in rows:
        brand = str(row["brand"])
        if brand in selected:
            continue
        selected[brand] = SelectedRun(
            brand=brand,
            run_id=int(row["run_id"]),
            status=str(row["status"]),
            model_version=str(row["model_version"]),
            created_at=row["created_at"],
            bundle_hash=str(row.get("bundle_hash") or ""),
        )
    return selected


def load_parsed_output(conn: pymysql.connections.Connection, run: SelectedRun) -> dict[str, Any]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT stage, title, body, bullets
            FROM zeta_analysis_outputs
            WHERE run_id = %s
            ORDER BY FIELD(stage, 'phenomenon', 'cause', 'prediction', 'recommendation')
            """,
            (run.run_id,),
        )
        rows = cursor.fetchall()

    parsed: dict[str, Any] = {}
    for row in rows:
        bullets_raw = row.get("bullets")
        try:
            bullets = json.loads(bullets_raw) if bullets_raw else []
        except json.JSONDecodeError:
            bullets = []
        parsed[str(row["stage"])] = {
            "title": row.get("title") or "",
            "body": row.get("body") or "",
            "bullets": bullets,
        }
    return parsed


def build_ai_analysis(run: SelectedRun, parsed: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "generated_at": _json_default(run.created_at),
        "model_version": run.model_version,
        "phase_zeta_stage": STAGE_MARKER,
        "run_id_phase_zeta": run.run_id,
        "bundle_hash": run.bundle_hash,
        "ownership": "cache_deep_analysis_ai_analysis",
        "migration_note": "Phase 30.3 separated ai_analysis from cache_deep_analysis response_json.",
    }
    for stage in STAGES:
        payload[stage] = parsed[stage]
    return payload


def load_market_ids(conn: pymysql.connections.Connection) -> dict[str, str | None]:
    placeholders = ",".join(["%s"] * len(JW25))
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT brand, market_id
            FROM cache_deep_analysis
            WHERE brand IN ({placeholders})
            """,
            JW25,
        )
        return {str(row["brand"]): row.get("market_id") for row in cursor.fetchall()}


def upsert_payloads(conn: pymysql.connections.Connection, payloads: dict[str, dict[str, Any]], market_ids: dict[str, str | None]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with conn.cursor() as cursor:
        for brand in JW25:
            payload = payloads[brand]
            cursor.execute(
                f"""
                INSERT INTO {TARGET_TABLE} (brand, market_id, ai_analysis_json)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  ai_analysis_json = VALUES(ai_analysis_json),
                  market_id = VALUES(market_id)
                """,
                (brand, market_ids.get(brand), _json_dumps(payload)),
            )
            rows.append({"brand": brand, "rows_affected": int(cursor.rowcount), "stage": payload["phase_zeta_stage"]})
    conn.commit()
    return rows


def build_payloads_from_zeta(conn: pymysql.connections.Connection) -> dict[str, dict[str, Any]]:
    runs = select_latest_runs(conn)
    missing_runs = sorted(set(JW25) - set(runs))
    if missing_runs:
        raise RuntimeError(f"Missing Phase zeta runs for {missing_runs}")

    payloads: dict[str, dict[str, Any]] = {}
    for brand in JW25:
        run = runs[brand]
        parsed = load_parsed_output(conn, run)
        missing_stages = [stage for stage in STAGES if stage not in parsed]
        if missing_stages:
            raise RuntimeError(f"{brand} missing zeta output stages: {missing_stages}")
        payloads[brand] = build_ai_analysis(run, parsed)
    return payloads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("DB_PORT", "3308")))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "root"))
    parser.add_argument("--db-password", default=os.getenv("DB_ROOT_PASSWORD", ""))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "jw_mart"))
    parser.add_argument("--audit-dir", default=None, help="Optional directory for a JSON migration report.")
    parser.add_argument("--apply", action="store_true", help="Insert/update missing ai_analysis rows from zeta outputs.")
    parser.add_argument("--force", action="store_true", help="Update rows even when all JW25 markers already exist.")
    args = parser.parse_args()

    with connect(args) as conn:
        table = ensure_table(conn)
        existing = existing_stage_rows(conn)
        complete = len(existing) == len(JW25) and all(existing[brand].get("stage") for brand in JW25 if brand in existing)
        report: dict[str, Any] = {
            "table": table,
            "jw25": len(JW25),
            "existing_marker_rows": sum(1 for row in existing.values() if row.get("stage")),
            "existing_complete": complete,
            "applied": False,
            "skipped_reason": None,
            "updated_rows": [],
        }

        if not args.apply:
            report["skipped_reason"] = "dry_run_without_apply"
        elif complete and not args.force:
            report["skipped_reason"] = "dedicated_table_already_has_all_jw25_phase_zeta_markers"
        else:
            payloads = build_payloads_from_zeta(conn)
            market_ids = load_market_ids(conn)
            report["updated_rows"] = upsert_payloads(conn, payloads, market_ids)
            report["applied"] = True

        report["post_rows"] = list(existing_stage_rows(conn).values())

    if args.audit_dir:
        audit_dir = Path(args.audit_dir)
        audit_dir.mkdir(parents=True, exist_ok=True)
        (audit_dir / "ai_analysis_migration_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=_json_default) + "\n",
            encoding="utf-8",
        )

    print(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
