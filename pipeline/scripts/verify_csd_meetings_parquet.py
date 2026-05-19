"""
verify_csd_meetings_parquet.py
==============================
Verify parquet/csd_meetings/*.parquet against IQVIA CSD Meetings raw xlsx.

Phases:
- Phase 0: full raw-row count, raw file by raw file.
- Phase 1: partition / report_month / sheet statistics.
- Phase 2: random 10 csd_row_id samples, raw_row_json exact comparison.
- Phase 3: raw_row_json validity and payload-shape checks.
- Phase 4: source_files, csd_row_id uniqueness, required-field checks.

Usage:
    python3 scripts/verify_csd_meetings_parquet.py \\
        --input-dir data/IQVIA/CSD/Meetings \\
        --parquet-dir parquet/csd_meetings
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path
from typing import Any

try:
    import duckdb
    import pyarrow.parquet as pq
except ImportError as e:
    sys.exit(f"ERROR: {e}\n  pip3 install duckdb pyarrow --break-system-packages")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from prototype_05_csd_meetings_to_parquet import (  # noqa: E402
    OUTPUT_COLS,
    iter_csd_meeting_rows,
    resolve_input_files,
)


def nfc(value: str | None) -> str | None:
    if value is None:
        return None
    return unicodedata.normalize("NFC", value)


def load_parquet_view(parquet_dir: Path) -> duckdb.DuckDBPyConnection:
    parquet_files = sorted(parquet_dir.glob("*.parquet"))
    if not parquet_files:
        sys.exit(f"ERROR: parquet 파일 없음: {parquet_dir}")
    con = duckdb.connect(":memory:")
    pattern = str(parquet_dir / "*.parquet")
    con.execute(f"CREATE OR REPLACE VIEW parquet AS SELECT * FROM read_parquet('{pattern}', union_by_name=true)")
    return con


def build_file_index(input_dir: Path) -> dict[str, Path]:
    return {nfc(path.name): path for path in resolve_input_files(input_dir)}


def count_raw_rows(path: Path) -> int:
    return sum(1 for _ in iter_csd_meeting_rows(path))


def phase0_full_count(con: duckdb.DuckDBPyConnection, file_index: dict[str, Path]) -> tuple[int, int, dict[str, int]]:
    print()
    print("=" * 72)
    print("[Phase 0] 전수 row count 검증 (raw rows)")
    print("=" * 72)

    parquet_counts = {
        nfc(source_file): count
        for source_file, count in con.execute(
            "SELECT source_file, COUNT(*) FROM parquet GROUP BY source_file ORDER BY source_file"
        ).fetchall()
    }

    print(f"  {'#':>2}  {'source_file':<34s}  {'raw':>8}  {'parquet':>8}  ok")
    print(f"  {'-' * 2}  {'-' * 34}  {'-' * 8}  {'-' * 8}  --")

    raw_counts: dict[str, int] = {}
    n_pass = 0
    n_fail = 0
    for index, (label, path) in enumerate(sorted(file_index.items()), start=1):
        raw_n = count_raw_rows(path)
        pq_n = parquet_counts.get(label, 0)
        raw_counts[label] = raw_n
        ok = raw_n == pq_n
        if ok:
            n_pass += 1
        else:
            n_fail += 1
        print(f"  {index:>2}  {label:<34s}  {raw_n:>8,}  {pq_n:>8,}  {'✓' if ok else '✗'}")

    total_raw = sum(raw_counts.values())
    total_pq = sum(parquet_counts.values())
    ok_total = total_raw == total_pq
    print(f"  {'-' * 2}  {'-' * 34}  {'-' * 8}  {'-' * 8}  --")
    print(f"      {'TOTAL':<34s}  {total_raw:>8,}  {total_pq:>8,}  {'✓' if ok_total else '✗'}")
    if not ok_total and n_fail == 0:
        n_fail = 1
    print(f"\n  [Phase 0 결과] pass={n_pass} / fail={n_fail}")
    return n_pass, n_fail, raw_counts


def phase1_stats(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    print()
    print("=" * 72)
    print("[Phase 1] 통계")
    print("=" * 72)

    total = con.execute("SELECT COUNT(*) FROM parquet").fetchone()[0]
    csd_rows = con.execute("SELECT COUNT(DISTINCT csd_row_id) FROM parquet").fetchone()[0]
    periods = con.execute("SELECT COUNT(DISTINCT period) FROM parquet").fetchone()[0]
    report_months = con.execute("SELECT COUNT(DISTINCT report_month) FROM parquet").fetchone()[0]
    sheets = con.execute("SELECT COUNT(DISTINCT source_sheet) FROM parquet").fetchone()[0]
    period_min, period_max = con.execute("SELECT MIN(period), MAX(period) FROM parquet").fetchone()

    print(f"  total rows:          {total:,}")
    print(f"  distinct csd_row_id: {csd_rows:,}")
    print(f"  periods:             {periods} ({period_min} ~ {period_max})")
    print(f"  report_months:       {report_months}")
    print(f"  source sheets:        {sheets}")

    print("\n  period 분포:")
    for period, n_rows in con.execute(
        "SELECT period, COUNT(*) FROM parquet GROUP BY period ORDER BY period"
    ).fetchall():
        print(f"    {period}: {n_rows:,}")

    print("\n  sheet 분포:")
    for source_file, source_sheet, n_rows in con.execute(
        """
        SELECT source_file, source_sheet, COUNT(*) AS n_rows
        FROM parquet
        GROUP BY source_file, source_sheet
        ORDER BY source_file, source_sheet
        """
    ).fetchall():
        print(f"    {source_file:<34s} {source_sheet:<20s} {n_rows:>6,}")

    return {
        "total_rows": total,
        "distinct_csd_row_id": csd_rows,
        "period_count": periods,
        "report_month_count": report_months,
        "period_min": period_min,
        "period_max": period_max,
        "sheet_count": sheets,
    }


def fetch_random_samples(con: duckdb.DuckDBPyConnection, n_samples: int) -> list[dict[str, Any]]:
    rows = con.execute(
        f"""
        SELECT {", ".join(f'"{col}"' for col in OUTPUT_COLS)}
        FROM parquet
        ORDER BY RANDOM()
        LIMIT {n_samples}
        """
    ).fetchall()
    return [dict(zip(OUTPUT_COLS, row)) for row in rows]


def build_expected_records(path: Path, targets: set[tuple[str, int]]) -> dict[tuple[str, int], dict[str, Any]]:
    found: dict[tuple[str, int], dict[str, Any]] = {}
    for record in iter_csd_meeting_rows(path, ingested_at="__VERIFY__"):
        key = (record["source_sheet"], record["source_row_id"])
        if key in targets:
            found[key] = record
            if len(found) == len(targets):
                break
    return found


def phase2_sample_check(
    con: duckdb.DuckDBPyConnection,
    file_index: dict[str, Path],
    n_samples: int,
) -> tuple[int, int]:
    print()
    print("=" * 72)
    print(f"[Phase 2] 랜덤 sample raw_row_json 검증 (n={n_samples})")
    print("=" * 72)

    samples = fetch_random_samples(con, n_samples)
    grouped: dict[str, set[tuple[str, int]]] = {}
    for sample in samples:
        label = nfc(sample["source_file"])
        grouped.setdefault(label, set()).add((sample["source_sheet"], sample["source_row_id"]))

    expected_by_file: dict[str, dict[tuple[str, int], dict[str, Any]]] = {}
    for label, targets in grouped.items():
        path = file_index.get(label)
        if path is None:
            expected_by_file[label] = {}
            continue
        expected_by_file[label] = build_expected_records(path, targets)

    pass_count = 0
    fail_count = 0
    compare_cols = [col for col in OUTPUT_COLS if col != "ingested_at"]

    for index, sample in enumerate(samples, start=1):
        label = nfc(sample["source_file"])
        key = (sample["source_sheet"], sample["source_row_id"])
        expected = expected_by_file.get(label, {}).get(key)
        title = f"{sample['source_file']} | {sample['source_sheet']} | row {sample['source_row_id']}"

        if expected is None:
            print(f"  [{index}] ✗ raw record not found: {title}")
            fail_count += 1
            continue

        mismatches = []
        for col in compare_cols:
            if col == "raw_row_json":
                left = json.loads(sample[col])
                right = json.loads(expected[col])
            else:
                left = sample[col]
                right = expected[col]
            if left != right:
                mismatches.append((col, left, right))

        if mismatches:
            fail_count += 1
            print(f"  [{index}] ✗ {title}")
            for col, got, exp in mismatches[:3]:
                print(f"       {col}: parquet={got!r} raw={exp!r}")
        else:
            pass_count += 1
            payload = json.loads(sample["raw_row_json"])
            cell_count = len(payload.get("cells", []))
            print(f"  [{index}] ✓ {title} cells={cell_count}")

    print(f"\n  [Phase 2 결과] pass={pass_count} / fail={fail_count}")
    return pass_count, fail_count


def iter_parquet_raw_json(parquet_dir: Path):
    for parquet_file in sorted(parquet_dir.glob("*.parquet")):
        pf = pq.ParquetFile(parquet_file)
        for batch in pf.iter_batches(batch_size=64, columns=["raw_row_json"]):
            for value in batch.column(0).to_pylist():
                yield parquet_file.name, value


def phase3_raw_json_validity(parquet_dir: Path) -> tuple[int, int, dict[str, int]]:
    print()
    print("=" * 72)
    print("[Phase 3] raw_row_json validity / shape 검증")
    print("=" * 72)

    n_rows = 0
    bad_json = 0
    bad_shape = 0
    null_raw_json = 0
    message_count_rows = 0
    meetings_rows = 0
    max_cells = 0

    for _file_name, row_json in iter_parquet_raw_json(parquet_dir):
        n_rows += 1
        if row_json is None:
            null_raw_json += 1
            continue
        try:
            payload = json.loads(row_json)
        except Exception:
            bad_json += 1
            continue
        cells = payload.get("cells")
        values_by_header = payload.get("values_by_header")
        if "source_row_id" not in payload or not isinstance(cells, list) or not isinstance(values_by_header, dict):
            bad_shape += 1
            continue
        max_cells = max(max_cells, len(cells))
        headers = {cell.get("header") for cell in cells if isinstance(cell, dict)}
        if "PRODUCT NAME" in headers and "제공 시기" in headers:
            message_count_rows += 1
        if "Meeting date" in headers and "Meeting Topic" in headers:
            meetings_rows += 1

    print(f"  rows scanned:          {n_rows:,}")
    print(f"  bad JSON rows:         {bad_json:,}")
    print(f"  bad payload shape rows:{bad_shape:,}")
    print(f"  NULL raw_row_json rows:{null_raw_json:,}")
    print(f"  Message Count rows:    {message_count_rows:,}")
    print(f"  Meetings rows:         {meetings_rows:,}")
    print(f"  max cells in raw row:  {max_cells:,}")

    fail = sum(1 for value in [bad_json, bad_shape, null_raw_json] if value != 0)
    print(f"\n  [Phase 3 결과] {'pass' if fail == 0 else 'fail'}")
    return (1 if fail == 0 else 0), fail, {
        "rows_scanned": n_rows,
        "bad_json": bad_json,
        "bad_shape": bad_shape,
        "null_raw_json": null_raw_json,
        "message_count_rows": message_count_rows,
        "meetings_rows": meetings_rows,
        "max_cells": max_cells,
    }


def phase4_source_and_keys(con: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 4] source_files / csd_row_id / required fields")
    print("=" * 72)

    source_mismatch = con.execute(
        "SELECT COUNT(*) FROM parquet WHERE COALESCE(source_files, '') <> COALESCE(source_file, '')"
    ).fetchone()[0]
    period_mismatch = con.execute(
        "SELECT COUNT(*) FROM parquet WHERE COALESCE(period, '') <> COALESCE(report_month, '')"
    ).fetchone()[0]
    duplicate_csd_row_id = con.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT csd_row_id, COUNT(*) AS n
            FROM parquet
            GROUP BY csd_row_id
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    null_required = con.execute(
        """
        SELECT COUNT(*)
        FROM parquet
        WHERE csd_row_id IS NULL OR source_file IS NULL OR source_sheet IS NULL
           OR source_row_id IS NULL OR report_month IS NULL OR period IS NULL
           OR raw_row_json IS NULL
        """
    ).fetchone()[0]
    observations_cols = [
        row[1]
        for row in con.execute("DESCRIBE parquet").fetchall()
        if row[0] == "observations_json"
    ]

    print(f"  source_files != source_file rows: {source_mismatch:,}")
    print(f"  period != report_month rows:      {period_mismatch:,}")
    print(f"  duplicate csd_row_id:             {duplicate_csd_row_id:,}")
    print(f"  required NULL rows:               {null_required:,}")
    print(f"  observations_json column exists:  {bool(observations_cols)}")

    fail = sum(
        1
        for value in [source_mismatch, period_mismatch, duplicate_csd_row_id, null_required]
        if value != 0
    )
    if observations_cols:
        fail += 1
    print(f"\n  [Phase 4 결과] {'pass' if fail == 0 else 'fail'}")
    return (1 if fail == 0 else 0), fail


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", default="data/IQVIA/CSD/Meetings", help="CSD Meetings xlsx 폴더")
    parser.add_argument("--parquet-dir", default="parquet/csd_meetings")
    parser.add_argument("--samples", type=int, default=10)
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    parquet_dir = Path(args.parquet_dir)
    file_index = build_file_index(input_dir)
    con = load_parquet_view(parquet_dir)

    print("=" * 72)
    print("Verify IQVIA CSD Meetings Parquet")
    print("=" * 72)
    print(f"  input dir:   {input_dir}")
    print(f"  parquet dir: {parquet_dir}")
    print(f"  raw files:   {len(file_index)}")

    _, phase0_fail, raw_counts = phase0_full_count(con, file_index)
    phase1 = phase1_stats(con)
    _, phase2_fail = phase2_sample_check(con, file_index, args.samples)
    _, phase3_fail, phase3 = phase3_raw_json_validity(parquet_dir)
    _, phase4_fail = phase4_source_and_keys(con)

    total_fail = phase0_fail + phase2_fail + phase3_fail + phase4_fail

    print()
    print("=" * 72)
    print("검증 요약")
    print("=" * 72)
    print(f"  Phase 0 fail: {phase0_fail}")
    print(f"  Phase 2 fail: {phase2_fail}")
    print(f"  Phase 3 fail: {phase3_fail}")
    print(f"  Phase 4 fail: {phase4_fail}")
    print(f"  total rows:   {phase1['total_rows']:,}")
    print(f"  raw rows:     {sum(raw_counts.values()):,}")
    print(f"  max cells:    {phase3['max_cells']:,}")

    con.close()
    if total_fail:
        print("\n  RESULT: FAIL")
        sys.exit(1)
    print("\n  RESULT: PASS")


if __name__ == "__main__":
    main()
