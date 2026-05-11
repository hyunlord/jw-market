"""
prototype_05_csd_meetings_to_parquet.py
=======================================
IQVIA CSD Meetings xlsx -> Parquet report-month partitions.

Reference policy mirrored from jw-market:
- SQL DDL source: sql/schema_iqvia.sql, stg_iqvia_csd_meetings_raw
- CSD row id: SHA256(meetings|source_file|source_sheet|source_row_id)[:32]
- Header detection and raw_row_json construction follow
  jw-market/etl/iqvia_csd_loader.py and iqvia_transform.py.
- Meetings keep full source rows as JSON; there is no observations_json column.
- Sparse policy: empty raw rows are skipped.

Prototype storage:
- 1 row = 1 raw source row, matching stg_iqvia_csd_meetings_raw.
- Output partition key is report_month (YYYY-MM), written to
  parquet/csd_meetings/YYYY-MM.parquet.
- Core DDL columns are preserved; prototype helper columns are period and
  source_files.

Usage:
    python3 scripts/prototype_05_csd_meetings_to_parquet.py \\
        --input-dir data/IQVIA/CSD/Meetings \\
        --output-dir parquet/csd_meetings
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import duckdb
    import pyarrow as pa
except ImportError as e:
    sys.exit(f"ERROR: {e}\n  pip3 install duckdb pyarrow --break-system-packages")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from prototype_03_csd_channel_to_parquet import (  # noqa: E402
    detect_header_row,
    is_empty_row,
    iter_workbook_sheets,
    make_csd_row_id,
    parse_report_month_from_filename,
    raw_row_json,
)


# SQL DDL columns from /Users/rexxa/github/jw-market/sql/schema_iqvia.sql
DDL_COLS = [
    "csd_row_id",
    "source_file",
    "source_sheet",
    "source_row_id",
    "report_month",
    "raw_row_json",
    "ingested_at",
]

# Prototype helper columns for partition traceability and source tracking.
OUTPUT_COLS = [
    "csd_row_id",
    "source_file",
    "source_sheet",
    "source_row_id",
    "report_month",
    "period",
    "raw_row_json",
    "source_files",
    "ingested_at",
]


def resolve_input_files(input_dir: Path) -> list[Path]:
    if not input_dir.exists() or not input_dir.is_dir():
        sys.exit(f"ERROR: input-dir 가 없거나 디렉토리가 아님: {input_dir}")
    files = sorted(
        path for path in input_dir.glob("*.xlsx") if not path.name.startswith(("~", "."))
    )
    if not files:
        sys.exit(f"ERROR: input-dir 안에 xlsx 파일 없음: {input_dir}")
    return files


def iter_csd_meeting_rows(xlsx_path: Path, ingested_at: str | None = None) -> Iterable[dict[str, Any]]:
    report_month = parse_report_month_from_filename(xlsx_path.name)
    if report_month is None:
        raise ValueError(f"report month parse 실패: {xlsx_path.name}")

    timestamp = ingested_at or datetime.now(timezone.utc).isoformat()
    for sheet_name, rows in iter_workbook_sheets(xlsx_path):
        header_row = detect_header_row(rows)
        if header_row is None:
            continue

        headers = tuple(rows[header_row - 1])
        for source_row_id, values in enumerate(rows[header_row:], start=header_row + 1):
            values_tuple = tuple(values)
            if is_empty_row(values_tuple):
                continue

            yield {
                "csd_row_id": make_csd_row_id("meetings", xlsx_path.name, sheet_name, source_row_id),
                "source_file": xlsx_path.name,
                "source_sheet": sheet_name,
                "source_row_id": source_row_id,
                "report_month": report_month,
                "period": report_month,
                "raw_row_json": raw_row_json(headers, values_tuple, source_row_id),
                "source_files": xlsx_path.name,
                "ingested_at": timestamp,
            }


def collect_raw_structure(xlsx_path: Path) -> dict[str, Any]:
    report_month = parse_report_month_from_filename(xlsx_path.name)
    sheet_count = 0
    loaded_sheets = 0
    skipped_sheets = 0
    header_rows: dict[int, int] = {}
    rows_by_sheet: dict[str, int] = {}
    headers_by_sheet: dict[str, list[str | None]] = {}

    for sheet_name, rows in iter_workbook_sheets(xlsx_path):
        sheet_count += 1
        header_row = detect_header_row(rows)
        if header_row is None:
            skipped_sheets += 1
            rows_by_sheet[sheet_name] = 0
            continue

        loaded_sheets += 1
        header_rows[header_row] = header_rows.get(header_row, 0) + 1
        headers = tuple(rows[header_row - 1])
        headers_by_sheet[sheet_name] = [None if h is None else str(h) for h in headers[:25]]
        data_rows = 0
        for values in rows[header_row:]:
            if not is_empty_row(tuple(values)):
                data_rows += 1
        rows_by_sheet[sheet_name] = data_rows

    return {
        "source_file": xlsx_path.name,
        "size_mb": xlsx_path.stat().st_size / 1024 / 1024,
        "report_month": report_month,
        "sheet_count": sheet_count,
        "loaded_sheets": loaded_sheets,
        "skipped_sheets": skipped_sheets,
        "header_rows": dict(sorted(header_rows.items())),
        "rows_by_sheet": rows_by_sheet,
        "headers_by_sheet": headers_by_sheet,
        "total_rows": sum(rows_by_sheet.values()),
    }


def create_staging_table(con: duckdb.DuckDBPyConnection) -> None:
    col_types = []
    for col in OUTPUT_COLS:
        if col == "source_row_id":
            col_types.append((col, "INTEGER"))
        else:
            col_types.append((col, "VARCHAR"))
    cols_def = ", ".join(f'"{col}" {typ}' for col, typ in col_types)
    con.execute(f"CREATE OR REPLACE TABLE stg_raw ({cols_def})")


def bulk_insert(con: duckdb.DuckDBPyConnection, batch_rows: list[dict[str, Any]]) -> None:
    if not batch_rows:
        return
    batch_dict = {col: [] for col in OUTPUT_COLS}
    for row in batch_rows:
        for col in OUTPUT_COLS:
            batch_dict[col].append(row.get(col))
    arrow_table = pa.Table.from_pydict(batch_dict)
    con.register("_batch", arrow_table)
    con.execute("INSERT INTO stg_raw SELECT * FROM _batch")
    con.unregister("_batch")


def write_partition_parquets(con: duckdb.DuckDBPyConnection, output_dir: Path) -> list[tuple[str, int, float]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob("*.parquet"):
        stale.unlink()

    periods = [row[0] for row in con.execute("SELECT DISTINCT period FROM stg_raw ORDER BY period").fetchall()]
    if not periods:
        return []

    select_cols = ", ".join(f'"{col}"' for col in OUTPUT_COLS)
    written = []
    for period in periods:
        out_path = output_dir / f"{period}.parquet"
        con.execute(
            f"""
            COPY (
                SELECT {select_cols}
                FROM stg_raw
                WHERE period = ?
                ORDER BY source_file, source_sheet, source_row_id, csd_row_id
            ) TO '{out_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """,
            [period],
        )
        n_rows = con.execute(f"SELECT COUNT(*) FROM '{out_path}'").fetchone()[0]
        size_mb = out_path.stat().st_size / 1024 / 1024
        written.append((period, n_rows, size_mb))
    return written


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", default="data/IQVIA/CSD/Meetings", help="CSD Meetings xlsx 폴더")
    parser.add_argument("--output-dir", default="parquet/csd_meetings")
    parser.add_argument("--db-path", default="staging_csd_meetings.duckdb")
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument("--keep-db", action="store_true")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    db_path = Path(args.db_path)
    files = resolve_input_files(input_dir)

    print("=" * 72)
    print("IQVIA CSD Meetings -> Parquet (report-month partitions)")
    print("=" * 72)
    print(f"  input dir:     {input_dir}")
    print(f"  files:         {len(files)}")
    print(f"  output dir:    {output_dir}")
    print(f"  duckdb path:   {db_path}")
    print(f"  chunk size:    {args.chunk_size:,}")
    print(f"  core columns:  {len(DDL_COLS)} DDL + period/source_files helpers")

    if db_path.exists():
        print(f"\n  기존 DuckDB 삭제: {db_path}")
        db_path.unlink()

    con = duckdb.connect(str(db_path))
    create_staging_table(con)

    ingested_at = datetime.now(timezone.utc).isoformat()
    total_inserted = 0
    structure_stats: list[dict[str, Any]] = []

    print("\n[Step 1] raw structure check + DuckDB insert")
    start = time.time()
    for index, xlsx_path in enumerate(files, start=1):
        t0 = time.time()
        structure = collect_raw_structure(xlsx_path)
        structure_stats.append(structure)

        chunk: list[dict[str, Any]] = []
        inserted_for_file = 0
        for row in iter_csd_meeting_rows(xlsx_path, ingested_at=ingested_at):
            chunk.append(row)
            inserted_for_file += 1
            if len(chunk) >= args.chunk_size:
                bulk_insert(con, chunk)
                chunk = []
        if chunk:
            bulk_insert(con, chunk)

        total_inserted += inserted_for_file
        elapsed = time.time() - t0
        print(
            f"  [{index}/{len(files)}] {xlsx_path.name:<34s} "
            f"sheets {structure['loaded_sheets']:>1}/{structure['sheet_count']:<1} "
            f"rows {inserted_for_file:>6,} report_month {structure['report_month']:<7s} "
            f"({structure['size_mb']:>4.1f}MB, {elapsed:>4.1f}s)"
        )

    elapsed_total = time.time() - start
    print(f"\n  total inserted: {total_inserted:,} raw rows ({elapsed_total:.1f}s)")

    print("\n[Step 2] staging stats")
    total_rows = con.execute("SELECT COUNT(*) FROM stg_raw").fetchone()[0]
    distinct_csd_rows = con.execute("SELECT COUNT(DISTINCT csd_row_id) FROM stg_raw").fetchone()[0]
    distinct_periods = con.execute("SELECT COUNT(DISTINCT period) FROM stg_raw").fetchone()[0]
    duplicate_csd_row_id = con.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT csd_row_id, COUNT(*) AS n
            FROM stg_raw
            GROUP BY csd_row_id
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    print(f"  total rows:              {total_rows:,}")
    print(f"  distinct csd_row_id:     {distinct_csd_rows:,}")
    print(f"  distinct periods:        {distinct_periods:,}")
    print(f"  duplicate csd_row_id groups: {duplicate_csd_row_id:,}")

    print("\n  sheet distribution:")
    for source_file, source_sheet, n_rows in con.execute(
        """
        SELECT source_file, source_sheet, COUNT(*)
        FROM stg_raw
        GROUP BY source_file, source_sheet
        ORDER BY source_file, source_sheet
        """
    ).fetchall():
        print(f"    {source_file:<34s} {source_sheet:<20s} {n_rows:>6,}")

    print("\n[Step 3] write Parquet")
    t0 = time.time()
    written = write_partition_parquets(con, output_dir)
    print(f"  partitions written: {len(written)} ({time.time() - t0:.1f}s)")

    con.close()
    if args.keep_db:
        print(f"\n  DuckDB preserved: {db_path}")
    else:
        db_path.unlink(missing_ok=True)
        print(f"\n  DuckDB deleted:   {db_path}")

    print()
    print("=" * 72)
    print("Result")
    print("=" * 72)
    print(f"  output:      {output_dir}/")
    print(f"  partitions:  {len(written)}")
    print(f"  rows:        {sum(n for _, n, _ in written):,}")
    print()
    print(f"  {'period':<10}  {'rows':>8}  {'size_mb':>9}")
    print(f"  {'-' * 10}  {'-' * 8}  {'-' * 9}")
    for period, n_rows, size_mb in written:
        print(f"  {period:<10}  {n_rows:>8,}  {size_mb:>9.2f}")


if __name__ == "__main__":
    main()
