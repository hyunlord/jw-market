#!/usr/bin/env python3
"""Compute extended Layer 3 mart_core_brand_metric metrics.

Usage:
  python pipeline/scripts/etl/layer3_compute_extended.py --ml ml_006 --dry-run
  python pipeline/scripts/etl/layer3_compute_extended.py --ml ml_006
  python pipeline/scripts/etl/layer3_compute_extended.py --all

Phase 16-E-4-B executes dry-run only. Non-dry-run UPDATE support is included
for Phase 16-E-4-C, but this phase must not execute it.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import pymysql

from layer3_normalize import parse_period  # noqa: E402
from ops_utils import configure_logging, find_project_root, first_existing, retry  # noqa: E402


LOGGER = configure_logging(__name__)
PROJECT_ROOT = find_project_root(Path(__file__).resolve())
CATALOG_DIR = PROJECT_ROOT / "output" / "catalog"
DRY_RUN_PREFIX = Path("/tmp")
EXTENDED_METRIC_VERSION = "v1"
ML_LOAD_ORDER = [
    "ml_001",
    "ml_002",
    "ml_003",
    "ml_004",
    "ml_005",
    "ml_006",
    "ml_007",
    "ml_008",
    "ml_009",
    "ml_010",
    "ml_011",
    "ml_012",
    "ml_013",
    "ml_014",
    "ml_015",
    "ml_016",
]
EXTENDED_METRIC_COLUMNS = [
    "cagr_1y",
    "cagr_3y",
    "cagr_5y",
    "ei_5y",
    "momentum_score",
    "growth_contribution",
    "hhi",
    "market_cagr_5y",
]
ANOMALY_THRESHOLDS = {
    "abs_cagr_1y_gt_5": ("cagr_1y", "abs_gt", 5.0),
    "abs_cagr_3y_gt_5": ("cagr_3y", "abs_gt", 5.0),
    "abs_cagr_5y_gt_5": ("cagr_5y", "abs_gt", 5.0),
    "abs_momentum_gt_5": ("momentum_score", "abs_gt", 5.0),
    "hhi_gt_10000": ("hhi", "gt", 10000.0),
    "ei_abs_gt_1000": ("ei_5y", "abs_gt", 1000.0),
}


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


@retry((pymysql.err.OperationalError, pymysql.err.InterfaceError), logger=LOGGER)
def mariadb_connect() -> pymysql.connections.Connection:
    env_path = first_existing(PROJECT_ROOT / "pipeline" / "docker" / ".env", PROJECT_ROOT / "docker" / ".env")
    env = load_env(env_path)
    if "MARIADB_PASSWORD" not in env:
        raise RuntimeError(f"MARIADB_PASSWORD is missing in {env_path}")
    return pymysql.connect(
        host="127.0.0.1",
        port=int(env.get("HOST_PORT", "3307")),
        user=env.get("MARIADB_USER", "jwapp"),
        password=env["MARIADB_PASSWORD"],
        database=env.get("MARIADB_DATABASE", "jw_mart"),
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def period_kind_and_ord(period: str) -> tuple[str, int]:
    info = parse_period(str(period))
    if info.kind == "month" and info.month is not None:
        return "month", info.year * 12 + info.month
    if info.kind == "quarter" and info.quarter is not None:
        return "quarter", info.year * 4 + info.quarter
    raise ValueError(f"unsupported period: {period!r}")


def periods_per_year(period_kind: str) -> int:
    if period_kind == "month":
        return 12
    if period_kind == "quarter":
        return 4
    raise ValueError(f"unsupported period kind: {period_kind!r}")


def safe_ratio(numerator: Any, denominator: Any) -> float | None:
    try:
        if numerator is None or denominator is None or pd.isna(numerator) or pd.isna(denominator):
            return None
        denominator_f = float(denominator)
        if denominator_f == 0 or math.isnan(denominator_f):
            return None
        numerator_f = float(numerator)
        if math.isnan(numerator_f):
            return None
        return numerator_f / denominator_f
    except (TypeError, ValueError):
        return None


def compute_cagr_value(end_value: Any, start_value: Any, years: int) -> float | None:
    ratio = safe_ratio(end_value, start_value)
    if ratio is None or ratio < 0:
        return None
    return (ratio ** (1 / years)) - 1


def compute_ei(brand_cagr_5y: Any, market_cagr_5y: Any) -> float | None:
    ratio = safe_ratio(brand_cagr_5y, market_cagr_5y)
    if ratio is None:
        return None
    return ratio * 100


def compute_growth_contribution(brand_growth_abs: Any, market_growth_abs: Any) -> float | None:
    ratio = safe_ratio(brand_growth_abs, market_growth_abs)
    if ratio is None:
        return None
    return ratio * 100


def compute_hhi(brand_ms_list: Iterable[Any]) -> float | None:
    values: list[float] = []
    for value in brand_ms_list:
        if value is None or pd.isna(value):
            continue
        values.append(float(value))
    if not values:
        return None
    return sum((ms * 100) ** 2 for ms in values)


def compute_momentum(quarterly_ms_percent: list[float]) -> float | None:
    if len(quarterly_ms_percent) < 4 or any(value is None or pd.isna(value) for value in quarterly_ms_percent):
        return None
    xs = [1, 2, 3, 4]
    ys = [float(value) for value in quarterly_ms_percent[-4:]]
    sum_xy = sum(x * y for x, y in zip(xs, ys, strict=False))
    sum_y = sum(ys)
    return (4 * sum_xy - 10 * sum_y) / 20


def safe_db_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def load_mart_rows(ml_id: str, conn: pymysql.connections.Connection) -> pd.DataFrame:
    sql = """
        SELECT
          id,
          ml_id,
          brand_id,
          brand_name,
          is_jw,
          period_yyyymm,
          channel,
          specialty,
          channel_norm,
          specialty_norm,
          market_share,
          raw_value,
          growth_abs
        FROM mart_core_brand_metric
        WHERE ml_id = %s
        ORDER BY brand_id, channel_norm, specialty_norm, period_yyyymm
    """
    with conn.cursor() as cur:
        cur.execute(sql, (ml_id,))
        df = pd.DataFrame(cur.fetchall())
    if df.empty:
        raise RuntimeError(f"[{ml_id}] no mart_core_brand_metric rows found")
    df["is_jw"] = df["is_jw"].astype(bool)
    for col in ("market_share", "raw_value", "growth_abs"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    kinds: list[str] = []
    ords: list[int] = []
    for period in df["period_yyyymm"]:
        kind, ordinal = period_kind_and_ord(period)
        kinds.append(kind)
        ords.append(ordinal)
    df["period_kind"] = kinds
    df["period_ord"] = ords
    return df


def add_brand_cagrs(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    join_keys = ["brand_id", "channel_norm", "specialty_norm", "period_kind"]
    lookup = result[join_keys + ["period_ord", "raw_value"]].rename(
        columns={"period_ord": "target_ord", "raw_value": "start_value"}
    )
    for years in (1, 3, 5):
        col = f"cagr_{years}y"
        work = result[join_keys + ["period_ord", "raw_value"]].copy()
        work["target_ord"] = work["period_ord"] - work["period_kind"].map(lambda kind: periods_per_year(kind) * years)
        merged = work.merge(lookup, on=join_keys + ["target_ord"], how="left")
        values: list[float | None] = []
        for end_value, start_value in zip(merged["raw_value"], merged["start_value"], strict=False):
            values.append(compute_cagr_value(end_value, start_value, years))
        result[col] = values
    return result


def _monthly_quarter_blocks(current_ord: int) -> list[list[int]]:
    return [
        [current_ord - 11, current_ord - 10, current_ord - 9],
        [current_ord - 8, current_ord - 7, current_ord - 6],
        [current_ord - 5, current_ord - 4, current_ord - 3],
        [current_ord - 2, current_ord - 1, current_ord],
    ]


def _quarterly_quarter_points(current_ord: int) -> list[list[int]]:
    return [[current_ord - 3], [current_ord - 2], [current_ord - 1], [current_ord]]


def add_brand_momentum(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    momentum = pd.Series(index=result.index, dtype="float64")
    group_keys = ["brand_id", "channel_norm", "specialty_norm", "period_kind"]
    for _, part in result.groupby(group_keys, sort=False, dropna=False):
        ms_by_ord = {
            int(row.period_ord): float(row.market_share) * 100
            for row in part[["period_ord", "market_share"]].itertuples(index=False)
            if pd.notna(row.market_share)
        }
        kind = str(part["period_kind"].iloc[0])
        for idx, row in part[["period_ord"]].iterrows():
            current_ord = int(row["period_ord"])
            blocks = _monthly_quarter_blocks(current_ord) if kind == "month" else _quarterly_quarter_points(current_ord)
            quarter_values: list[float] = []
            complete = True
            for block in blocks:
                values = [ms_by_ord.get(ord_value) for ord_value in block]
                if any(value is None for value in values):
                    complete = False
                    break
                quarter_values.append(sum(values) / len(values))
            if complete:
                momentum.loc[idx] = compute_momentum(quarter_values)
    result["momentum_score"] = momentum
    return result


def add_market_metrics(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    market_keys = ["period_yyyymm", "period_kind", "period_ord", "channel_norm", "specialty_norm"]

    hhi = (
        result.groupby(market_keys, dropna=False)["market_share"]
        .apply(compute_hhi)
        .rename("hhi")
        .reset_index()
    )
    result = result.merge(hhi, on=market_keys, how="left")

    totals = (
        result.groupby(market_keys, dropna=False)["raw_value"]
        .sum(min_count=1)
        .rename("market_raw_value")
        .reset_index()
    )
    total_lookup = totals[["period_kind", "channel_norm", "specialty_norm", "period_ord", "market_raw_value"]].rename(
        columns={"period_ord": "target_ord", "market_raw_value": "market_start_value"}
    )
    totals["target_ord"] = totals["period_ord"] - totals["period_kind"].map(lambda kind: periods_per_year(kind) * 5)
    totals = totals.merge(
        total_lookup,
        on=["period_kind", "channel_norm", "specialty_norm", "target_ord"],
        how="left",
    )
    totals["market_cagr_5y"] = [
        compute_cagr_value(end_value, start_value, 5)
        for end_value, start_value in zip(totals["market_raw_value"], totals["market_start_value"], strict=False)
    ]
    market_cagr = totals[market_keys + ["market_cagr_5y"]]
    result = result.merge(market_cagr, on=market_keys, how="left")
    return result


def add_ei(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    result["ei_5y"] = [
        compute_ei(brand_cagr, market_cagr)
        for brand_cagr, market_cagr in zip(result["cagr_5y"], result["market_cagr_5y"], strict=False)
    ]
    return result


def add_growth_contribution(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    market_growth = (
        result.groupby(["period_yyyymm", "channel_norm", "specialty_norm"], dropna=False)["growth_abs"]
        .sum(min_count=1)
        .rename("market_growth_abs")
        .reset_index()
    )
    result = result.merge(market_growth, on=["period_yyyymm", "channel_norm", "specialty_norm"], how="left")
    result["growth_contribution"] = [
        compute_growth_contribution(brand_growth, market_growth_abs)
        for brand_growth, market_growth_abs in zip(result["growth_abs"], result["market_growth_abs"], strict=False)
    ]
    return result.drop(columns=["market_growth_abs"])


def compute_extended_metrics(df: pd.DataFrame) -> pd.DataFrame:
    result = add_brand_cagrs(df)
    result = add_brand_momentum(result)
    result = add_market_metrics(result)
    result = add_ei(result)
    result = add_growth_contribution(result)
    result["extended_metric_version"] = EXTENDED_METRIC_VERSION
    return result


def compute_extended_metrics_for_ml(ml_id: str, dry_run: bool = False) -> pd.DataFrame:
    start = time.monotonic()
    LOGGER.info("[%s] Loading mart_core_brand_metric", ml_id)
    with mariadb_connect() as conn:
        df = load_mart_rows(ml_id, conn)
    LOGGER.info("[%s] Rows loaded: %s", ml_id, f"{len(df):,}")
    result = compute_extended_metrics(df)
    LOGGER.info("[%s] Extended metrics computed in %.1fs", ml_id, time.monotonic() - start)
    if dry_run:
        out = DRY_RUN_PREFIX / f"extended_dry_run_{ml_id}.csv"
        result.to_csv(out, index=False)
        LOGGER.info("[%s] Dry-run CSV saved: %s", ml_id, out)
    else:
        with mariadb_connect() as conn:
            update_to_mariadb(result, ml_id, conn)
    return result


def update_to_mariadb(df: pd.DataFrame, ml_id: str, conn: pymysql.connections.Connection, batch_size: int = 5000) -> int:
    sql = """
        UPDATE mart_core_brand_metric
        SET cagr_1y = %(cagr_1y)s,
            cagr_3y = %(cagr_3y)s,
            cagr_5y = %(cagr_5y)s,
            ei_5y = %(ei_5y)s,
            momentum_score = %(momentum_score)s,
            growth_contribution = %(growth_contribution)s,
            hhi = %(hhi)s,
            market_cagr_5y = %(market_cagr_5y)s,
            extended_metric_version = %(extended_metric_version)s
        WHERE id = %(id)s
          AND ml_id = %(ml_id)s
    """
    records = []
    for row in df[["id", "ml_id", *EXTENDED_METRIC_COLUMNS, "extended_metric_version"]].itertuples(index=False):
        records.append(
            {
                "id": int(row.id),
                "ml_id": row.ml_id,
                "cagr_1y": safe_db_float(row.cagr_1y),
                "cagr_3y": safe_db_float(row.cagr_3y),
                "cagr_5y": safe_db_float(row.cagr_5y),
                "ei_5y": safe_db_float(row.ei_5y),
                "momentum_score": safe_db_float(row.momentum_score),
                "growth_contribution": safe_db_float(row.growth_contribution),
                "hhi": safe_db_float(row.hhi),
                "market_cagr_5y": safe_db_float(row.market_cagr_5y),
                "extended_metric_version": row.extended_metric_version,
            }
        )

    updated = 0
    with conn.cursor() as cur:
        try:
            for start in range(0, len(records), batch_size):
                batch = records[start : start + batch_size]
                cur.executemany(sql, batch)
                conn.commit()
                updated += len(batch)
                LOGGER.info("[%s] updated %s rows", ml_id, f"{updated:,}")
        except Exception:
            conn.rollback()
            raise
    return updated


def ml_ids_from_catalog() -> list[str]:
    ml_path = CATALOG_DIR / "ml_market" / "ml_market.parquet"
    if not ml_path.exists():
        return ML_LOAD_ORDER
    ml = pd.read_parquet(ml_path, columns=["ml_id"])
    return sorted(ml["ml_id"].dropna().unique().tolist())


def metric_null_breakdown(df: pd.DataFrame) -> dict[str, float]:
    return {col: float(df[col].isna().mean() * 100) for col in EXTENDED_METRIC_COLUMNS}


def anomaly_distribution(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    total = len(df)
    for name, (column, op, threshold) in ANOMALY_THRESHOLDS.items():
        series = pd.to_numeric(df[column], errors="coerce")
        if op == "abs_gt":
            mask = series.abs() > threshold
        elif op == "gt":
            mask = series > threshold
        else:
            raise ValueError(f"unsupported anomaly op: {op}")
        count = int(mask.fillna(False).sum())
        rows.append(
            {
                "anomaly": name,
                "metric": column,
                "threshold": threshold,
                "rows": count,
                "pct": 0.0 if total == 0 else count / total * 100,
            }
        )
    return pd.DataFrame(rows)


def hard_error_count(df: pd.DataFrame) -> int:
    errors = 0
    errors += int(df["raw_value"].isna().sum())
    errors += int((pd.to_numeric(df["hhi"], errors="coerce") < 0).fillna(False).sum())
    errors += int((pd.to_numeric(df["market_share"], errors="coerce") < -0.00001).fillna(False).sum())
    return errors


def print_dry_run_report(df: pd.DataFrame, ml_id: str) -> int:
    print(f"\n=== {ml_id} extended metric dry-run sample ===")
    sample_cols = [
        "id",
        "brand_name",
        "period_yyyymm",
        "channel",
        "specialty",
        "market_share",
        "raw_value",
        *EXTENDED_METRIC_COLUMNS,
    ]
    print(df[sample_cols].head(20).to_string(index=False))
    print(f"\nTotal rows: {len(df):,}")
    print("\nNull breakdown:")
    for col, pct in metric_null_breakdown(df).items():
        print(f"  {col}: {pct:.2f}% null")
    anomalies = anomaly_distribution(df)
    errors = hard_error_count(df)
    print("\nAnomaly check:")
    for row in anomalies.itertuples(index=False):
        print(f"  {row.anomaly}: {row.rows:,} rows ({row.pct:.2f}%)")
    print(f"\nERROR: {errors:,}")
    return 1 if errors else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ml", help="One ml_id to compute, e.g. ml_006")
    parser.add_argument("--all", action="store_true", help="Compute all ml markets")
    parser.add_argument("--dry-run", action="store_true", help="Write dry-run CSV only; do not UPDATE")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.all and args.ml:
        raise SystemExit("--all and --ml are mutually exclusive")
    if not args.all and not args.ml:
        raise SystemExit("Provide --ml ML_ID or --all")

    ml_ids = ml_ids_from_catalog() if args.all else [args.ml]
    exit_code = 0
    for ml_id in ml_ids:
        df = compute_extended_metrics_for_ml(ml_id, dry_run=args.dry_run)
        if args.dry_run:
            exit_code = max(exit_code, print_dry_run_report(df, ml_id))
        else:
            LOGGER.info("[%s] updated", ml_id)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
