from __future__ import annotations

"""Build the F-124a May repair into one guarded staging table.

The live sidecar remains untouched. The builder copies it byte-for-byte into
``__staging_f124a`` and merges only histories produced from the verified May
UBIST parquet. Promotion is deliberately outside this module.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Final, Sequence

import pymysql

from pipeline.etl.io.mart.filter_dimension_load import quote_id
from pipeline.etl.io.mart.filter_dimension_metric import build_filter_dimension_rows
from pipeline.etl.io.mart.general_config import MEASURES_BY_SOURCE
from pipeline.etl.io.mart.general_json import dumps
from pipeline.etl.io.mart.general_ubist import iter_ubist_base_frames
from pipeline.etl.io.mart.general_ubist import ubist_measure_frame
from pipeline.scripts.deploy.mart_load_verify import table_exists
from pipeline.scripts.etl.build_filter_dimension_metric import _connect_admin

F124A_TARGET_DB: Final[str] = "jw_mart_d2_stage_20260630_r2"
F124A_LIVE_TABLE: Final[str] = "mart_general_filter_dimension_metric"
F124A_STAGING_TABLE: Final[str] = f"{F124A_LIVE_TABLE}__staging_f124a"
F124A_PERIOD: Final[str] = "2026-05"
INSERT_COLUMNS: Final[tuple[str, ...]] = (
    "source",
    "measure",
    "atc4_code",
    "brand_key",
    "brand_name",
    "product_code",
    "dimension_type",
    "dimension_value",
    "dimension_value_norm",
    "dimension_value_hash",
    "raw_value_history",
)


def guard_f124a_target(target_db: str, target_table: str) -> None:
    if target_db != F124A_TARGET_DB or target_table != F124A_STAGING_TABLE:
        raise ValueError(f"F-124a only permits {F124A_TARGET_DB}.{F124A_STAGING_TABLE}")


def verify_may_parquet(ubist_dir: Path, expected_sha256: str) -> Path:
    parquet = ubist_dir / "year=2026" / "month=05" / "data.parquet"
    if not parquet.is_file():
        raise FileNotFoundError(f"May UBIST parquet not found: {parquet}")
    hasher = hashlib.sha256()
    with parquet.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if digest != expected_sha256:
        raise RuntimeError(f"May UBIST parquet sha256 mismatch: actual={digest} expected={expected_sha256}")
    return parquet


def prepare_may_input_root(parquet: Path, spool_dir: Path) -> Path:
    """Expose only the verified May partition to the shared UBIST reader."""
    input_root = spool_dir / "verified-may-input"
    shutil.rmtree(input_root, ignore_errors=True)
    linked_parquet = input_root / "year=2026" / "month=05" / "data.parquet"
    linked_parquet.parent.mkdir(parents=True)
    linked_parquet.symlink_to(parquet.resolve())
    return input_root


def create_and_copy_live_table(
    conn: pymysql.connections.Connection,
    *,
    target_db: str = F124A_TARGET_DB,
    target_table: str = F124A_STAGING_TABLE,
    batch_size: int = 200,
) -> int:
    guard_f124a_target(target_db, target_table)
    if batch_size < 1 or batch_size > 200:
        raise ValueError("batch_size must be between 1 and 200")
    if not table_exists(conn, target_db, F124A_LIVE_TABLE):
        raise RuntimeError(f"live table missing: {target_db}.{F124A_LIVE_TABLE}")
    if table_exists(conn, target_db, target_table):
        raise RuntimeError(f"staging table already exists: {target_db}.{target_table}")

    live = f"{quote_id(target_db)}.{quote_id(F124A_LIVE_TABLE)}"
    staging = f"{quote_id(target_db)}.{quote_id(target_table)}"
    copied = 0
    last_id = 0
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE {staging} LIKE {live}")
        while True:
            cur.execute(
                f"INSERT INTO {staging} SELECT * FROM {live} "
                f"WHERE id > %s ORDER BY id LIMIT {batch_size}",
                (last_id,),
            )
            inserted = int(cur.rowcount)
            if inserted == 0:
                break
            copied += inserted
            cur.execute(
                f"SELECT MAX(id) AS max_id FROM ("
                f"SELECT id FROM {live} WHERE id > %s ORDER BY id LIMIT {inserted}"
                f") AS copied_rows",
                (last_id,),
            )
            last_id = int(cur.fetchone()["max_id"])
    return copied


def merge_may_rows(
    conn: pymysql.connections.Connection,
    rows: Sequence[dict[str, Any]],
    *,
    target_db: str = F124A_TARGET_DB,
    target_table: str = F124A_STAGING_TABLE,
    batch_size: int = 200,
) -> int:
    guard_f124a_target(target_db, target_table)
    if batch_size < 1 or batch_size > 200:
        raise ValueError("batch_size must be between 1 and 200")
    for row in rows:
        history = row.get("raw_value_history")
        if not isinstance(history, dict) or set(history) != {F124A_PERIOD}:
            raise RuntimeError(f"F-124a row contains a non-May period: {sorted(history or {})}")

    columns_sql = ",".join(quote_id(column) for column in INSERT_COLUMNS)
    sql = (
        f"INSERT INTO {quote_id(target_db)}.{quote_id(target_table)} ({columns_sql}) "
        f"VALUES ({','.join(['%s'] * len(INSERT_COLUMNS))}) "
        "ON DUPLICATE KEY UPDATE "
        "raw_value_history=JSON_MERGE_PATCH(raw_value_history, VALUES(raw_value_history))"
    )
    payloads = [_row_payload(row) for row in rows]
    with conn.cursor() as cur:
        for start in range(0, len(payloads), batch_size):
            cur.executemany(sql, payloads[start : start + batch_size])
    return len(payloads)


def build_and_merge_may_rows(
    conn: pymysql.connections.Connection,
    *,
    spool_dir: Path,
    target_db: str,
    target_table: str,
    batch_size: int,
) -> tuple[dict[str, int], int]:
    measures = {measure: 0 for measure in MEASURES_BY_SOURCE["ubist"]}
    total = 0
    for base in iter_ubist_base_frames(spool_dir=spool_dir, partition_count=64):
        periods = {str(period) for period in base["period_yyyymm"].dropna().tolist()}
        if periods != {F124A_PERIOD}:
            raise RuntimeError(f"F-124a input must contain only {F124A_PERIOD}: {sorted(periods)}")
        for measure in MEASURES_BY_SOURCE["ubist"]:
            rows = build_filter_dimension_rows("ubist", measure, ubist_measure_frame(base, measure))
            merged_count = merge_may_rows(
                conn,
                rows,
                target_db=target_db,
                target_table=target_table,
                batch_size=batch_size,
            )
            measures[measure] += merged_count
            total += merged_count
    return measures, total


def run(args: argparse.Namespace) -> dict[str, Any]:
    guard_f124a_target(args.target_db, args.target_table)
    parquet = verify_may_parquet(args.ubist_dir, args.expected_sha256)
    os.environ["S4_UBIST_DIR"] = str(prepare_may_input_root(parquet, args.spool_dir))
    os.environ["S4_INPUT_MODE"] = "raw"

    conn = _connect_admin()
    try:
        copied = create_and_copy_live_table(
            conn,
            target_db=args.target_db,
            target_table=args.target_table,
            batch_size=args.batch_size,
        )
        measures, may_rows_merged = build_and_merge_may_rows(
            conn,
            spool_dir=args.spool_dir,
            target_db=args.target_db,
            target_table=args.target_table,
            batch_size=args.batch_size,
        )
        summary = _staging_summary(conn, args.target_db, args.target_table)
        manifest = {
            "target_db": args.target_db,
            "live_table": F124A_LIVE_TABLE,
            "staging_table": args.target_table,
            "period": F124A_PERIOD,
            "parquet": {"path": str(parquet), "sha256": args.expected_sha256},
            "copied_live_rows": copied,
            "may_rows_merged": may_rows_merged,
            "measures": measures,
            "staging": summary,
            "live_identity_touched": False,
        }
        args.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        args.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return manifest
    finally:
        conn.close()


def _row_payload(row: dict[str, Any]) -> tuple[object, ...]:
    values = dict(row)
    values["dimension_value_hash"] = hashlib.sha256(
        str(row["dimension_value_norm"]).encode("utf-8")
    ).hexdigest()
    values["raw_value_history"] = dumps(row["raw_value_history"])
    return tuple(values[column] for column in INSERT_COLUMNS)


def _staging_summary(
    conn: pymysql.connections.Connection,
    target_db: str,
    target_table: str,
) -> dict[str, int]:
    table = f"{quote_id(target_db)}.{quote_id(target_table)}"
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
        total = int(cur.fetchone()["n"])
        cur.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE source='ubist' "
            f"AND JSON_CONTAINS_PATH(raw_value_history, 'one', '$.\"{F124A_PERIOD}\"')=1"
        )
        may_rows = int(cur.fetchone()["n"])
    return {"row_count": total, "ubist_may_rows": may_rows}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-db", default=F124A_TARGET_DB)
    parser.add_argument("--target-table", default=F124A_STAGING_TABLE)
    parser.add_argument("--ubist-dir", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--manifest-path", type=Path, required=True)
    parser.add_argument("--spool-dir", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=200)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    print(json.dumps(run(parse_args(argv)), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
