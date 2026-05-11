"""
verify_csd_channel_parquet.py
=============================
Verify parquet/csd_channel/*.parquet against IQVIA CSD ChannelDynamics raw xlsx.

Phases:
- Phase 0: full raw-row count, raw file by raw file.
- Phase 1: partition / period / sheet / channel statistics.
- Phase 2: random 10 (csd_row_id, period) samples, raw meta + observation + raw_row_json.
- Phase 3: sparse policy, explicit 0 preservation, JSON shape.
- Phase 4: source_files and key uniqueness checks.

Usage:
    python3 scripts/verify_csd_channel_parquet.py \\
        --input-dir "data/IQVIA/CSD/ChannelDynamics (콜 수=영업 횟수)" \\
        --parquet-dir parquet/csd_channel
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import unicodedata
from pathlib import Path
from typing import Any

try:
    import duckdb
except ImportError as e:
    sys.exit(f"ERROR: {e}\n  pip3 install duckdb --break-system-packages")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from prototype_03_csd_channel_to_parquet import (  # noqa: E402
    OUTPUT_COLS,
    iter_csd_raw_rows,
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


def count_raw_sparse_rows(path: Path) -> int:
    return sum(1 for _ in iter_csd_raw_rows(path))


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

    print(f"  {'#':>2}  {'source_file':<70s}  {'raw':>10}  {'parquet':>10}  ok")
    print(f"  {'-' * 2}  {'-' * 70}  {'-' * 10}  {'-' * 10}  --")

    raw_counts: dict[str, int] = {}
    n_pass = 0
    n_fail = 0
    for index, (label, path) in enumerate(sorted(file_index.items()), start=1):
        raw_n = count_raw_sparse_rows(path)
        pq_n = parquet_counts.get(label, 0)
        raw_counts[label] = raw_n
        ok = raw_n == pq_n
        if ok:
            n_pass += 1
        else:
            n_fail += 1
        print(f"  {index:>2}  {label:<70s}  {raw_n:>10,}  {pq_n:>10,}  {'✓' if ok else '✗'}")

    total_raw = sum(raw_counts.values())
    total_pq = sum(parquet_counts.values())
    ok_total = total_raw == total_pq
    print(f"  {'-' * 2}  {'-' * 70}  {'-' * 10}  {'-' * 10}  --")
    print(f"      {'TOTAL':<70s}  {total_raw:>10,}  {total_pq:>10,}  {'✓' if ok_total else '✗'}")
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
    sheets = con.execute("SELECT COUNT(DISTINCT source_sheet) FROM parquet").fetchone()[0]
    period_min, period_max = con.execute("SELECT MIN(period), MAX(period) FROM parquet").fetchone()

    print(f"  total rows:          {total:,}")
    print(f"  distinct csd_row_id: {csd_rows:,}")
    print(f"  periods:             {periods} ({period_min} ~ {period_max})")
    print(f"  source sheets:        {sheets}")

    print("\n  period 분포:")
    for period, n_rows in con.execute(
        "SELECT period, COUNT(*) FROM parquet GROUP BY period ORDER BY period"
    ).fetchall():
        print(f"    {period}: {n_rows:,}")

    print("\n  channel / section 분포 top 20:")
    for channel, section, n_rows in con.execute(
        """
        SELECT COALESCE(channel, '(NULL)') AS channel,
               COALESCE(report_section, '(NULL)') AS report_section,
               COUNT(*) AS n_rows
        FROM parquet
        GROUP BY channel, report_section
        ORDER BY n_rows DESC
        LIMIT 20
        """
    ).fetchall():
        print(f"    {channel:<12s} {section:<18s} {n_rows:>10,}")

    return {
        "total_rows": total,
        "distinct_csd_row_id": csd_rows,
        "period_count": periods,
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


def build_expected_records(path: Path, targets: set[tuple[str, int, str]]) -> dict[tuple[str, int, str], dict[str, Any]]:
    found: dict[tuple[str, int, str], dict[str, Any]] = {}
    for record in iter_csd_raw_rows(path, ingested_at="__VERIFY__"):
        key = (record["source_sheet"], record["source_row_id"], record["period"])
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
    print(f"[Phase 2] 랜덤 sample 검증 (n={n_samples})")
    print("=" * 72)

    random.seed(20260511)
    samples = fetch_random_samples(con, n_samples)
    grouped: dict[str, set[tuple[str, int, str]]] = {}
    for sample in samples:
        label = nfc(sample["source_file"])
        grouped.setdefault(label, set()).add(
            (sample["source_sheet"], sample["source_row_id"], sample["period"])
        )

    expected_by_file: dict[str, dict[tuple[str, int, str], dict[str, Any]]] = {}
    for label, targets in grouped.items():
        path = file_index.get(label)
        if path is None:
            expected_by_file[label] = {}
            continue
        expected_by_file[label] = build_expected_records(path, targets)

    pass_count = 0
    fail_count = 0
    compare_cols = [col for col in OUTPUT_COLS if col not in {"ingested_at"}]

    for index, sample in enumerate(samples, start=1):
        label = nfc(sample["source_file"])
        key = (sample["source_sheet"], sample["source_row_id"], sample["period"])
        expected = expected_by_file.get(label, {}).get(key)
        title = (
            f"{sample['source_file']} | {sample['source_sheet']} | "
            f"row {sample['source_row_id']} | {sample['period']}"
        )
        if expected is None:
            print(f"  [{index}] ✗ raw record not found: {title}")
            fail_count += 1
            continue

        mismatches = []
        for col in compare_cols:
            if col in {"observations_json", "raw_row_json"}:
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
            obs = json.loads(sample["observations_json"])
            print(f"  [{index}] ✓ {title} obs={obs}")

    print(f"\n  [Phase 2 결과] pass={pass_count} / fail={fail_count}")
    return pass_count, fail_count


def phase3_sparse_and_zero(con: duckdb.DuckDBPyConnection, file_index: dict[str, Path]) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 3] sparse / 0 vs NULL / JSON shape 검증")
    print("=" * 72)

    def obs_stats(obs_json_values: list[str]) -> dict[str, int]:
        stats = {
            "rows": len(obs_json_values),
            "empty_obs_rows": 0,
            "zero_values": 0,
            "null_values": 0,
            "value_count": 0,
            "bad_json": 0,
        }
        for obs_json in obs_json_values:
            try:
                obs = json.loads(obs_json)
            except Exception:
                stats["bad_json"] += 1
                continue
            if not obs:
                stats["empty_obs_rows"] += 1
            for value in obs.values():
                stats["value_count"] += 1
                if value is None:
                    stats["null_values"] += 1
                elif value == 0:
                    stats["zero_values"] += 1
        return stats

    parquet_obs = [row[0] for row in con.execute("SELECT observations_json FROM parquet").fetchall()]
    pq_stats = obs_stats(parquet_obs)

    raw_obs = []
    for path in file_index.values():
        for record in iter_csd_raw_rows(path):
            raw_obs.append(record["observations_json"])
    raw_stats = obs_stats(raw_obs)

    print(f"  parquet rows:         {pq_stats['rows']:,}")
    print(f"  empty obs rows:       raw={raw_stats['empty_obs_rows']:,} / parquet={pq_stats['empty_obs_rows']:,}")
    print(f"  observation values:   raw={raw_stats['value_count']:,} / parquet={pq_stats['value_count']:,}")
    print(f"  explicit zero values: raw={raw_stats['zero_values']:,} / parquet={pq_stats['zero_values']:,}")
    print(f"  null values:          raw={raw_stats['null_values']:,} / parquet={pq_stats['null_values']:,}")
    print(f"  bad JSON rows:        raw={raw_stats['bad_json']:,} / parquet={pq_stats['bad_json']:,}")

    fail = 0
    for key in ["rows", "empty_obs_rows", "zero_values", "null_values", "value_count", "bad_json"]:
        if raw_stats[key] != pq_stats[key]:
            fail += 1
    if raw_stats["bad_json"] != 0 or pq_stats["bad_json"] != 0:
        fail += 1

    print(f"\n  [Phase 3 결과] {'pass' if fail == 0 else 'fail'}")
    return (1 if fail == 0 else 0), fail


def phase4_source_and_json(con: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 4] source_files / observations_json / key uniqueness")
    print("=" * 72)

    source_mismatch = con.execute(
        "SELECT COUNT(*) FROM parquet WHERE COALESCE(source_files, '') <> COALESCE(source_file, '')"
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
           OR source_row_id IS NULL OR period IS NULL
           OR observations_json IS NULL OR raw_row_json IS NULL
        """
    ).fetchone()[0]

    obs_json_bad = 0
    raw_json_bad = 0
    for obs_json, row_json in con.execute("SELECT observations_json, raw_row_json FROM parquet").fetchall():
        try:
            json.loads(obs_json)
        except Exception:
            obs_json_bad += 1
        try:
            payload = json.loads(row_json)
            if "source_row_id" not in payload or "cells" not in payload or "values_by_header" not in payload:
                raw_json_bad += 1
        except Exception:
            raw_json_bad += 1

    print(f"  source_files != source_file rows: {source_mismatch:,}")
    print(f"  duplicate csd_row_id:             {duplicate_csd_row_id:,}")
    print(f"  required NULL rows:               {null_required:,}")
    print(f"  bad observations_json rows:       {obs_json_bad:,}")
    print(f"  bad raw_row_json rows:            {raw_json_bad:,}")

    fail = sum(
        1
        for value in [source_mismatch, duplicate_csd_row_id, null_required, obs_json_bad, raw_json_bad]
        if value != 0
    )
    print(f"\n  [Phase 4 결과] {'pass' if fail == 0 else 'fail'}")
    return (1 if fail == 0 else 0), fail


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input-dir",
        default="data/IQVIA/CSD/ChannelDynamics (콜 수=영업 횟수)",
        help="ChannelDynamics xlsx 폴더",
    )
    parser.add_argument("--parquet-dir", default="parquet/csd_channel")
    parser.add_argument("--samples", type=int, default=10)
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    parquet_dir = Path(args.parquet_dir)
    file_index = build_file_index(input_dir)
    con = load_parquet_view(parquet_dir)

    print("=" * 72)
    print("Verify IQVIA CSD ChannelDynamics Parquet")
    print("=" * 72)
    print(f"  input dir:   {input_dir}")
    print(f"  parquet dir: {parquet_dir}")
    print(f"  raw files:   {len(file_index)}")

    _, phase0_fail, raw_counts = phase0_full_count(con, file_index)
    phase1 = phase1_stats(con)
    _, phase2_fail = phase2_sample_check(con, file_index, args.samples)
    _, phase3_fail = phase3_sparse_and_zero(con, file_index)
    _, phase4_fail = phase4_source_and_json(con)

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

    con.close()
    if total_fail:
        print("\n  RESULT: FAIL")
        sys.exit(1)
    print("\n  RESULT: PASS")


if __name__ == "__main__":
    main()
