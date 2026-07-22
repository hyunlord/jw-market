#!/usr/bin/env python3
"""Build isolated CSD ChannelDynamics stage artifacts without touching production mart tables."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Final

import openpyxl

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from pipeline.scripts.etl.brand_activity.csd_core import (
    CsdRow,
    MarketSheetScan,
    deduplicate_rows,
    scan_market_sheet,
    select_market_sheets,
    source_month_key,
)
from pipeline.scripts.etl.brand_activity.csd_validation import build_validation


EXPECTED_SOURCE_MONTHS: Final[tuple[str, ...]] = ("2025-06", "2025-07", "2025-08", "2025-09", "2025-10")
DEFAULT_STAGE_SCHEMA: Final[str] = "jw_brand_activity_stage"
CSV_COLUMNS: Final[tuple[str, ...]] = (
    "period_ym",
    "market",
    "jw_channel",
    "master_product",
    "representing_company",
    "product_details",
    "source_file",
    "source_sheet",
    "source_row_no",
)
DDL_TEMPLATE: Final[str] = """CREATE SCHEMA IF NOT EXISTS `{schema}`;

CREATE TABLE IF NOT EXISTS `{schema}`.`csd_channel_dynamics_stage` (
  `period_ym` char(7) NOT NULL,
  `market` varchar(128) NOT NULL,
  `jw_channel` varchar(32) NOT NULL,
  `master_product` varchar(255) NOT NULL,
  `representing_company` varchar(255) NOT NULL,
  `product_details` int NOT NULL,
  `source_file` varchar(255) NOT NULL,
  `source_sheet` varchar(128) NOT NULL,
  `source_row_no` int NOT NULL,
  `loaded_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`period_ym`, `market`, `jw_channel`, `master_product`, `representing_company`),
  KEY `idx_csd_stage_market_period` (`market`, `period_ym`),
  KEY `idx_csd_stage_product` (`master_product`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""
BRAND_ACTIVITY_SCHEMA_PATTERN: Final = re.compile(
    r"^(?:jw_brand_activity_|jw_ingest_)[A-Za-z0-9_]+$"
)


def quote_schema_name(schema: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", schema):
        raise ValueError(f"unsafe schema name: {schema!r}")
    if schema != "jw_brand_activity_stage" and BRAND_ACTIVITY_SCHEMA_PATTERN.fullmatch(schema) is None:
        raise ValueError(f"refusing non-stage schema: {schema!r}")
    return schema


def stage_ddl(schema: str) -> str:
    return DDL_TEMPLATE.format(schema=quote_schema_name(schema))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_month_ym(path: Path) -> str | None:
    year, month, _ = source_month_key(path.name)
    if year == 0 or month == 0:
        return None
    return f"{year}-{month:02d}"


def discover_workbooks(source_root: Path, expected_months: tuple[str, ...]) -> tuple[list[Path], list[str], list[str]]:
    candidates = sorted(path for path in source_root.glob("*.xlsx") if not path.name.startswith("~$"))
    selected: list[Path] = []
    ignored: list[str] = []
    for path in candidates:
        month = source_month_ym(path)
        if month in expected_months:
            selected.append(path)
        else:
            ignored.append(path.name)
    present = {source_month_ym(path) for path in selected}
    missing = [month for month in expected_months if month not in present]
    return selected, missing, ignored


def load_csd_rows(workbooks: list[Path]) -> tuple[list[CsdRow], list[MarketSheetScan], dict[str, list[str]]]:
    rows: list[CsdRow] = []
    scans: list[MarketSheetScan] = []
    sheet_map: dict[str, list[str]] = {}
    for workbook_path in workbooks:
        workbook = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
        try:
            market_sheets = select_market_sheets(tuple(workbook.sheetnames))
        finally:
            workbook.close()
        sheet_map[workbook_path.name] = list(market_sheets)
        for sheet_name in market_sheets:
            sheet_rows, scan = scan_market_sheet(workbook_path, sheet_name)
            rows.extend(sheet_rows)
            scans.append(scan)
    return rows, scans, sheet_map


def write_stage_csv(path: Path, rows: list[CsdRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            data = row.to_dict()
            writer.writerow({column: data[column] for column in CSV_COLUMNS})


def load_isolated_db(args: argparse.Namespace, rows: list[CsdRow]) -> dict[str, int | str]:
    import pymysql

    schema = quote_schema_name(args.stage_schema)
    password = args.db_password or os.environ.get(args.db_password_env, "")
    connection = pymysql.connect(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=password,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=8,
    )
    table_name = f"`{schema}`.`csd_channel_dynamics_stage`"
    try:
        with connection.cursor() as cursor:
            for statement in stage_ddl(schema).split(";"):
                sql = statement.strip()
                if sql:
                    cursor.execute(sql)
            cursor.execute(f"TRUNCATE TABLE {table_name}")
            cursor.executemany(
                f"""
                INSERT INTO {table_name}
                (period_ym, market, jw_channel, master_product, representing_company, product_details, source_file, source_sheet, source_row_no)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        row.period_ym,
                        row.market,
                        row.jw_channel,
                        row.master_product,
                        row.representing_company,
                        row.product_details,
                        row.source_file,
                        row.source_sheet,
                        row.source_row_no,
                    )
                    for row in rows
                ],
            )
            cursor.execute(f"SELECT COUNT(*), COALESCE(SUM(product_details), 0) FROM {table_name}")
            count, total = cursor.fetchone()
        connection.commit()
        return {"schema": schema, "table": "csd_channel_dynamics_stage", "rows": int(count), "product_details": int(total)}
    except pymysql.MySQLError:
        connection.rollback()
        raise
    finally:
        connection.close()


def write_manifest(path: Path, workbooks: list[Path]) -> None:
    manifest = [{"path": str(workbook), "file": workbook.name, "sha256": sha256_file(workbook)} for workbook in workbooks]
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create isolated CSD ChannelDynamics stage artifacts.")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-months", nargs="*", default=list(EXPECTED_SOURCE_MONTHS))
    parser.add_argument("--no-db-load", action="store_true")
    parser.add_argument("--db-load", action="store_true")
    parser.add_argument("--stage-schema", default=DEFAULT_STAGE_SCHEMA)
    parser.add_argument("--db-host", default="127.0.0.1")
    parser.add_argument("--db-port", type=int, default=3308)
    parser.add_argument("--db-user", default="root")
    parser.add_argument("--db-password", default="")
    parser.add_argument("--db-password-env", default="MARIADB_ROOT_PASSWORD")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.no_db_load and args.db_load:
        raise SystemExit("--no-db-load and --db-load are mutually exclusive")
    expected_months = tuple(args.expected_months)
    args.audit_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    workbooks, missing_months, ignored_files = discover_workbooks(args.source_root, expected_months)
    if not workbooks:
        raise SystemExit(f"No CSD workbooks found under {args.source_root}")
    raw_rows, scans, sheet_map = load_csd_rows(workbooks)
    deduped, dedup_report = deduplicate_rows(raw_rows)
    stage_csv = args.output_dir / "csd_channel_dynamics_stage.csv"
    write_stage_csv(stage_csv, deduped)
    (args.output_dir / "csd_channel_dynamics_stage.sql").write_text(stage_ddl(args.stage_schema), encoding="utf-8")
    validation = build_validation(workbooks, raw_rows, scans, deduped, dedup_report, missing_months, ignored_files, sheet_map)
    validation["db_load"] = load_isolated_db(args, deduped) if args.db_load else "skipped"
    (args.audit_dir / "csd_ingest_validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_manifest(args.audit_dir / "source_sha256_manifest.json", workbooks)
    run_log = {
        "source_root": str(args.source_root),
        "audit_dir": str(args.audit_dir),
        "output_dir": str(args.output_dir),
        "stage_csv": str(stage_csv),
        "missing_expected_months": missing_months,
        "rows_written": len(deduped),
    }
    (args.audit_dir / "csd_ingest_run_log.json").write_text(json.dumps(run_log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(run_log, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
