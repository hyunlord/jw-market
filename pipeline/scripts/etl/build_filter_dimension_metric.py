from __future__ import annotations

"""Build dynamic filter dimension sidecars into an isolated schema.

This tracked CLI is the only approved path for STAGE A UBIST dimension sidecar
loads. It writes a new ``jw_mart_dim_stage_*`` schema, never live ``jw_mart``.
The sidecar uses product-level rows because UBIST analysis dimensions such as
제형, 투여경로, 성분용량, and 급여구분 belong to products. Filtering a whole
brand row by one product's dimension would overstate market size.
"""

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import pymysql

from pipeline.etl.io.mart.filter_dimension_load import create_filter_dimension_table
from pipeline.etl.io.mart.filter_dimension_load import insert_filter_dimension_rows
from pipeline.etl.io.mart.filter_dimension_load import quote_id
from pipeline.etl.io.mart.filter_dimension_metric import FILTER_DIMENSION_TABLE
from pipeline.etl.io.mart.filter_dimension_metric import build_filter_dimension_rows
from pipeline.etl.io.mart.filter_dimension_metric import guard_dimension_stage_target
from pipeline.etl.io.mart.filter_dimension_metric import summarize_dimension_rows
from pipeline.etl.io.mart.general_config import PROJECT_ROOT
from pipeline.etl.io.mart.general_config import first_existing
from pipeline.etl.io.mart.general_config import load_env
from pipeline.etl.io.mart.general_ubist import load_ubist_base_frame
from pipeline.etl.io.mart.general_ubist import ubist_measure_frame


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-db", required=True, help="New isolated schema, must start with jw_mart_dim_stage_")
    parser.add_argument("--manifest-path", type=Path, required=True)
    parser.add_argument("--ubist-dir", type=Path, help="Raw UBIST parquet root. Defaults to S4_UBIST_DIR/output/ubist")
    parser.add_argument("--max-rows", type=int, default=None, help="Fast validation only; do not use for production evidence")
    parser.add_argument("--batch-size", type=int, default=200)
    return parser.parse_args()


def run(args: argparse.Namespace) -> dict[str, Any]:
    guard_dimension_stage_target(args.target_db)
    if args.batch_size > 200:
        raise ValueError("--batch-size must be <= 200 for Galera writeset safety")
    if args.ubist_dir:
        os.environ["S4_UBIST_DIR"] = str(args.ubist_dir)

    started = time.perf_counter()
    conn = _connect_admin()
    try:
        if _schema_exists(conn, args.target_db):
            raise RuntimeError(f"target schema already exists: {args.target_db}")
        before_live = _general_table_counts(conn, "jw_mart")
        create_filter_dimension_table(conn, args.target_db)

        base = load_ubist_base_frame(max_rows=args.max_rows)
        source_rows = int(len(base))
        manifest: dict[str, Any] = {
            "target_db": args.target_db,
            "table": FILTER_DIMENSION_TABLE,
            "source": "ubist",
            "policy": {
                "isolated_prefix": "jw_mart_dim_stage_",
                "live_schema_blocked": "jw_mart",
                "batch_size": args.batch_size,
                "molecule_dimension": "disabled",
                "pack_desc": "not_applicable_to_ubist",
            },
            "source_rows": source_rows,
            "measures": {},
            "live_before": before_live,
        }
        for measure in ("sales", "volume"):
            frame = ubist_measure_frame(base, measure)
            rows = build_filter_dimension_rows("ubist", measure, frame)
            insert_filter_dimension_rows(conn, args.target_db, rows, batch_size=args.batch_size)
            manifest["measures"][measure] = {
                "input_rows": int(len(frame)),
                "sidecar": summarize_dimension_rows(rows),
            }

        manifest["target"] = _target_summary(conn, args.target_db)
        manifest["live_after"] = _general_table_counts(conn, "jw_mart")
        manifest["live_unchanged"] = manifest["live_before"] == manifest["live_after"]
        manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        _write_json(args.manifest_path, manifest)
        return manifest
    finally:
        conn.close()


def _connect_admin() -> pymysql.connections.Connection:
    env_path = first_existing(PROJECT_ROOT / "pipeline" / "docker" / ".env", PROJECT_ROOT / "docker" / ".env")
    env = load_env(env_path)
    password = env.get("MARIADB_ROOT_PASSWORD") or env.get("MARIADB_PASSWORD")
    if not password:
        raise RuntimeError("MARIADB_ROOT_PASSWORD/MARIADB_PASSWORD is missing")
    return pymysql.connect(
        host=env.get("MARIADB_HOST", "127.0.0.1"),
        port=int(env.get("MARIADB_PORT") or env.get("HOST_PORT", "3308")),
        user="root" if env.get("MARIADB_ROOT_PASSWORD") else env.get("MARIADB_USER", "jwapp"),
        password=password,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _schema_exists(conn: pymysql.connections.Connection, db_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.schemata WHERE schema_name=%s", (db_name,))
        return int(cur.fetchone()["n"]) > 0


def _general_table_counts(conn: pymysql.connections.Connection, db_name: str) -> dict[str, int | None]:
    counts: dict[str, int | None] = {}
    for table in ("mart_general_brand_metric", "mart_general_market_metric"):
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS n FROM {quote_id(db_name)}.{quote_id(table)}")
                counts[table] = int(cur.fetchone()["n"])
        except pymysql.err.ProgrammingError:
            counts[table] = None
    return counts


def _target_summary(conn: pymysql.connections.Connection, db_name: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM {quote_id(db_name)}.{quote_id(FILTER_DIMENSION_TABLE)}")
        row_count = int(cur.fetchone()["n"])
        cur.execute(
            f"""
            SELECT source, measure, dimension_type,
                   COUNT(*) AS row_count,
                   COUNT(DISTINCT dimension_value_norm) AS option_count
            FROM {quote_id(db_name)}.{quote_id(FILTER_DIMENSION_TABLE)}
            GROUP BY source, measure, dimension_type
            ORDER BY source, measure, dimension_type
            """
        )
        coverage = cur.fetchall()
    return {"row_count": row_count, "coverage": coverage}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    manifest = run(parse_args())
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    if not manifest["live_unchanged"]:
        raise SystemExit("live jw_mart general table counts changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
