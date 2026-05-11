"""
verify_chso_parquet.py
======================
Verify parquet/chso/*.parquet against IQVIA CHSO Sell-Out raw xlsx.

Phases:
- Phase 0: full sparse long-row count by month.
- Phase 1: partition / period / product_key statistics.
- Phase 2: random 10 (product_key, month) samples, metadata and
  observations_json exact comparison against raw xlsx.
- Phase 3: explicit 0 vs NULL/empty distinction.
- Phase 4: observations_json and raw_extra_json validity.
- Phase 5: source_files, product_key uniqueness, and Grand Total checks.

Usage:
    python3 scripts/verify_chso_parquet.py \\
        --input-file "data/IQVIA/CHSO/CHSO_KOR_SellOut_Basic_Feb-19-2026(2026년 1월까지).xlsx" \\
        --parquet-dir parquet/chso
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

from prototype_06_chso_to_parquet import (  # noqa: E402
    METRIC_TO_JSON_KEY,
    OUTPUT_COLS,
    analyze_chso_raw,
    iter_chso_rows,
    resolve_input_file,
)


EXPECTED_METRICS = set(METRIC_TO_JSON_KEY.values())


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


def phase0_full_count(con: duckdb.DuckDBPyConnection, raw_analysis: dict[str, Any]) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 0] 전수 row count 검증 (raw sparse long rows)")
    print("=" * 72)

    parquet_counts = {
        period: count
        for period, count in con.execute(
            "SELECT period, COUNT(*) FROM parquet GROUP BY period ORDER BY period"
        ).fetchall()
    }
    raw_counts = raw_analysis["period_counts"]

    periods = sorted(set(raw_counts) | set(parquet_counts))
    print(f"  {'period':<10}  {'raw':>8}  {'parquet':>8}  ok")
    print(f"  {'-' * 10}  {'-' * 8}  {'-' * 8}  --")
    n_pass = 0
    n_fail = 0
    for period in periods:
        raw_n = raw_counts.get(period, 0)
        pq_n = parquet_counts.get(period, 0)
        ok = raw_n == pq_n
        if ok:
            n_pass += 1
        else:
            n_fail += 1
        print(f"  {period:<10}  {raw_n:>8,}  {pq_n:>8,}  {'✓' if ok else '✗'}")

    total_raw = sum(raw_counts.values())
    total_pq = sum(parquet_counts.values())
    ok_total = total_raw == total_pq
    print(f"  {'-' * 10}  {'-' * 8}  {'-' * 8}  --")
    print(f"  {'TOTAL':<10}  {total_raw:>8,}  {total_pq:>8,}  {'✓' if ok_total else '✗'}")
    if not ok_total and n_fail == 0:
        n_fail = 1
    print(f"\n  [Phase 0 결과] pass={n_pass} / fail={n_fail}")
    return n_pass, n_fail


def phase1_stats(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    print()
    print("=" * 72)
    print("[Phase 1] 통계")
    print("=" * 72)

    total = con.execute("SELECT COUNT(*) FROM parquet").fetchone()[0]
    distinct_products = con.execute("SELECT COUNT(DISTINCT product_key) FROM parquet").fetchone()[0]
    periods = con.execute("SELECT COUNT(DISTINCT period) FROM parquet").fetchone()[0]
    period_min, period_max = con.execute("SELECT MIN(period), MAX(period) FROM parquet").fetchone()
    duplicate_product_period = con.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT product_key, period, COUNT(*) AS n
            FROM parquet
            GROUP BY product_key, period
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    grand_total_rows = con.execute("SELECT COUNT(*) FROM parquet WHERE is_grand_total").fetchone()[0]

    print(f"  total rows:                   {total:,}")
    print(f"  distinct product_key:         {distinct_products:,}")
    print(f"  periods:                      {periods} ({period_min} ~ {period_max})")
    print(f"  duplicate product_key+period: {duplicate_product_period:,}")
    print(f"  grand total rows:             {grand_total_rows:,}")

    print("\n  product_key 당 month 수 분포:")
    for n_periods, n_products in con.execute(
        """
        SELECT n_periods, COUNT(*) AS n_products
        FROM (
            SELECT product_key, COUNT(DISTINCT period) AS n_periods
            FROM parquet
            GROUP BY product_key
        )
        GROUP BY n_periods
        ORDER BY n_periods
        """
    ).fetchall():
        print(f"    {n_periods:>2} months: {n_products:,}")

    return {
        "total_rows": total,
        "distinct_product_key": distinct_products,
        "period_count": periods,
        "period_min": period_min,
        "period_max": period_max,
        "duplicate_product_period": duplicate_product_period,
        "grand_total_rows": grand_total_rows,
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


def build_expected_records(path: Path, targets: set[tuple[int, str]]) -> dict[tuple[int, str], dict[str, Any]]:
    found: dict[tuple[int, str], dict[str, Any]] = {}
    for record in iter_chso_rows(path, ingested_at="__VERIFY__"):
        key = (record["source_row_id"], record["period"])
        if key in targets:
            found[key] = record
            if len(found) == len(targets):
                break
    return found


def phase2_sample_check(
    con: duckdb.DuckDBPyConnection,
    input_file: Path,
    n_samples: int,
) -> tuple[int, int]:
    print()
    print("=" * 72)
    print(f"[Phase 2] 랜덤 sample raw 비교 (n={n_samples})")
    print("=" * 72)

    samples = fetch_random_samples(con, n_samples)
    targets = {(int(sample["source_row_id"]), sample["period"]) for sample in samples}
    expected = build_expected_records(input_file, targets)
    compare_cols = [col for col in OUTPUT_COLS if col != "ingested_at"]

    pass_count = 0
    fail_count = 0
    for index, sample in enumerate(samples, start=1):
        key = (int(sample["source_row_id"]), sample["period"])
        raw_record = expected.get(key)
        title = (
            f"row {sample['source_row_id']} | {sample['period']} | "
            f"{sample.get('product_name_kor') or sample.get('audit_desc')}"
        )

        if raw_record is None:
            print(f"  [{index}] ✗ raw record not found: {title}")
            fail_count += 1
            continue

        mismatches = []
        for col in compare_cols:
            if col in {"observations_json", "raw_extra_json"}:
                left = json.loads(sample[col])
                right = json.loads(raw_record[col])
            elif col == "source_file":
                left = nfc(sample[col])
                right = nfc(raw_record[col])
            elif col == "source_files":
                left = nfc(sample[col])
                right = nfc(raw_record[col])
            else:
                left = sample[col]
                right = raw_record[col]
            if left != right:
                mismatches.append((col, left, right))

        if mismatches:
            fail_count += 1
            print(f"  [{index}] ✗ {title}")
            for col, got, exp in mismatches[:5]:
                print(f"       {col}: parquet={got!r} raw={exp!r}")
        else:
            pass_count += 1
            obs = json.loads(sample["observations_json"])
            metric_keys = sorted(obs[sample["period"]].keys())
            print(f"  [{index}] ✓ {title} metrics={metric_keys}")

    print(f"\n  [Phase 2 결과] pass={pass_count} / fail={fail_count}")
    return pass_count, fail_count


def iter_parquet_json_rows(parquet_dir: Path):
    for parquet_file in sorted(parquet_dir.glob("*.parquet")):
        pf = pq.ParquetFile(parquet_file)
        for batch in pf.iter_batches(batch_size=2048, columns=["period", "observations_json", "raw_extra_json"]):
            periods = batch.column(0).to_pylist()
            observations = batch.column(1).to_pylist()
            extras = batch.column(2).to_pylist()
            for period, obs_json, extra_json in zip(periods, observations, extras):
                yield period, obs_json, extra_json


def phase3_zero_vs_null(parquet_dir: Path, raw_analysis: dict[str, Any]) -> tuple[int, int, dict[str, int]]:
    print()
    print("=" * 72)
    print("[Phase 3] 0 값 vs NULL/empty 구분")
    print("=" * 72)

    parquet_metric_values = 0
    parquet_zero_values = 0
    for period, obs_json, _extra_json in iter_parquet_json_rows(parquet_dir):
        obs = json.loads(obs_json)
        metrics = obs.get(period, {})
        for value in metrics.values():
            parquet_metric_values += 1
            if value == 0:
                parquet_zero_values += 1

    raw_metric_values = raw_analysis["non_empty_metric_cells"]
    raw_zero_values = raw_analysis["explicit_zero_cells"]
    raw_empty_values = raw_analysis["empty_metric_cells"]

    print(f"  raw non-empty metric cells:     {raw_metric_values:,}")
    print(f"  parquet metric values:         {parquet_metric_values:,}")
    print(f"  raw explicit zero cells:        {raw_zero_values:,}")
    print(f"  parquet zero metric values:     {parquet_zero_values:,}")
    print(f"  raw empty metric cells skipped: {raw_empty_values:,}")

    fail = 0
    if raw_metric_values != parquet_metric_values:
        fail += 1
    if raw_zero_values != parquet_zero_values:
        fail += 1
    print(f"\n  [Phase 3 결과] {'pass' if fail == 0 else 'fail'}")
    return (1 if fail == 0 else 0), fail, {
        "raw_metric_values": raw_metric_values,
        "parquet_metric_values": parquet_metric_values,
        "raw_zero_values": raw_zero_values,
        "parquet_zero_values": parquet_zero_values,
        "raw_empty_values": raw_empty_values,
    }


def phase4_json_validity(parquet_dir: Path) -> tuple[int, int, dict[str, int]]:
    print()
    print("=" * 72)
    print("[Phase 4] JSON validity / observations_json structure")
    print("=" * 72)

    rows_scanned = 0
    bad_json = 0
    bad_shape = 0
    raw_extra_not_dict = 0
    metric_key_counts: dict[str, int] = {metric: 0 for metric in sorted(EXPECTED_METRICS)}

    for period, obs_json, extra_json in iter_parquet_json_rows(parquet_dir):
        rows_scanned += 1
        try:
            obs = json.loads(obs_json)
            extra = json.loads(extra_json)
        except Exception:
            bad_json += 1
            continue
        if not isinstance(extra, dict):
            raw_extra_not_dict += 1
        if set(obs.keys()) != {period} or not isinstance(obs.get(period), dict):
            bad_shape += 1
            continue
        metrics = obs[period]
        if not set(metrics).issubset(EXPECTED_METRICS):
            bad_shape += 1
            continue
        for metric in metrics:
            metric_key_counts[metric] = metric_key_counts.get(metric, 0) + 1

    print(f"  rows scanned:              {rows_scanned:,}")
    print(f"  bad JSON rows:             {bad_json:,}")
    print(f"  bad observation shape rows:{bad_shape:,}")
    print(f"  raw_extra not dict rows:   {raw_extra_not_dict:,}")
    print("\n  metric key counts:")
    for metric, count in sorted(metric_key_counts.items()):
        print(f"    {metric:<24s} {count:>8,}")

    fail = sum(1 for value in [bad_json, bad_shape, raw_extra_not_dict] if value != 0)
    print(f"\n  [Phase 4 결과] {'pass' if fail == 0 else 'fail'}")
    return (1 if fail == 0 else 0), fail, {
        "rows_scanned": rows_scanned,
        "bad_json": bad_json,
        "bad_shape": bad_shape,
        "raw_extra_not_dict": raw_extra_not_dict,
        "metric_key_counts": metric_key_counts,
    }


def phase5_sources_keys_grand_total(
    con: duckdb.DuckDBPyConnection,
    raw_analysis: dict[str, Any],
) -> tuple[int, int, dict[str, int]]:
    print()
    print("=" * 72)
    print("[Phase 5] source_files / product_key / Grand Total")
    print("=" * 72)

    source_mismatch = con.execute(
        "SELECT COUNT(*) FROM parquet WHERE COALESCE(source_files, '') <> COALESCE(source_file, '')"
    ).fetchone()[0]
    duplicate_product_period = con.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT product_key, period, COUNT(*) AS n
            FROM parquet
            GROUP BY product_key, period
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    null_required = con.execute(
        """
        SELECT COUNT(*)
        FROM parquet
        WHERE product_key IS NULL OR period IS NULL OR observations_json IS NULL
           OR raw_extra_json IS NULL OR source_file IS NULL OR source_sheet IS NULL
           OR source_row_id IS NULL OR source_files IS NULL OR is_grand_total IS NULL
        """
    ).fetchone()[0]
    distinct_product_keys = con.execute("SELECT COUNT(DISTINCT product_key) FROM parquet").fetchone()[0]
    grand_total_rows = con.execute("SELECT COUNT(*) FROM parquet WHERE is_grand_total").fetchone()[0]
    grand_total_products = con.execute(
        "SELECT COUNT(DISTINCT product_key) FROM parquet WHERE is_grand_total"
    ).fetchone()[0]

    print(f"  source_files != source_file rows:     {source_mismatch:,}")
    print(f"  duplicate product_key+period groups:  {duplicate_product_period:,}")
    print(f"  required NULL rows:                   {null_required:,}")
    print(f"  raw unique product_key:               {raw_analysis['unique_product_keys']:,}")
    print(f"  parquet distinct product_key:         {distinct_product_keys:,}")
    print(f"  raw duplicate product_key groups:     {raw_analysis['duplicate_product_key_groups']:,}")
    print(f"  raw Grand Total rows:                 {raw_analysis['grand_total_raw_rows']:,}")
    print(f"  parquet Grand Total product_keys:     {grand_total_products:,}")
    print(f"  parquet Grand Total long rows:        {grand_total_rows:,}")

    fail = 0
    checks = [
        source_mismatch,
        duplicate_product_period,
        null_required,
        raw_analysis["duplicate_product_key_groups"],
    ]
    if any(value != 0 for value in checks):
        fail += 1
    if distinct_product_keys != raw_analysis["unique_product_keys"]:
        fail += 1
    expected_grand_total_long_rows = (
        raw_analysis["period_count"] if raw_analysis["grand_total_raw_rows"] else 0
    )
    if grand_total_products != raw_analysis["grand_total_raw_rows"]:
        fail += 1
    if grand_total_rows != expected_grand_total_long_rows:
        fail += 1

    print(f"\n  [Phase 5 결과] {'pass' if fail == 0 else 'fail'}")
    return (1 if fail == 0 else 0), fail, {
        "source_mismatch": source_mismatch,
        "duplicate_product_period": duplicate_product_period,
        "null_required": null_required,
        "distinct_product_keys": distinct_product_keys,
        "grand_total_products": grand_total_products,
        "grand_total_rows": grand_total_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-file", default=None)
    parser.add_argument("--input-dir", default="data/IQVIA/CHSO")
    parser.add_argument("--parquet-dir", default="parquet/chso")
    parser.add_argument("--samples", type=int, default=10)
    args = parser.parse_args()

    input_file = resolve_input_file(args.input_file, args.input_dir)
    parquet_dir = Path(args.parquet_dir)
    con = load_parquet_view(parquet_dir)

    print("=" * 72)
    print("Verify IQVIA CHSO Sell-Out Parquet")
    print("=" * 72)
    print(f"  input file:  {input_file}")
    print(f"  parquet dir: {parquet_dir}")

    print("\n  raw workbook scan...")
    raw_analysis = analyze_chso_raw(input_file)
    print(
        f"    raw rows={raw_analysis['raw_rows']:,}, "
        f"sparse long rows={raw_analysis['sparse_long_rows']:,}, "
        f"periods={raw_analysis['period_count']}"
    )

    _, phase0_fail = phase0_full_count(con, raw_analysis)
    phase1 = phase1_stats(con)
    _, phase2_fail = phase2_sample_check(con, input_file, args.samples)
    _, phase3_fail, phase3 = phase3_zero_vs_null(parquet_dir, raw_analysis)
    _, phase4_fail, phase4 = phase4_json_validity(parquet_dir)
    _, phase5_fail, phase5 = phase5_sources_keys_grand_total(con, raw_analysis)

    total_fail = phase0_fail + phase2_fail + phase3_fail + phase4_fail + phase5_fail

    print()
    print("=" * 72)
    print("검증 요약")
    print("=" * 72)
    print(f"  Phase 0 fail: {phase0_fail}")
    print(f"  Phase 2 fail: {phase2_fail}")
    print(f"  Phase 3 fail: {phase3_fail}")
    print(f"  Phase 4 fail: {phase4_fail}")
    print(f"  Phase 5 fail: {phase5_fail}")
    print(f"  total rows:   {phase1['total_rows']:,}")
    print(f"  raw rows:     {raw_analysis['sparse_long_rows']:,}")
    print(f"  metric values:{phase3['parquet_metric_values']:,}")
    print(f"  JSON rows:    {phase4['rows_scanned']:,}")
    print(f"  Grand rows:   {phase5['grand_total_rows']:,}")

    con.close()
    if total_fail:
        print("\n  RESULT: FAIL")
        sys.exit(1)
    print("\n  RESULT: PASS")


if __name__ == "__main__":
    main()
