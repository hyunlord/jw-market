"""Build the rolling Tier2 crawl brand universe from mart metrics."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pymysql

from pipeline.scripts.crawler.tier2_match_score import Tier2Brand


DEFAULT_SALES_THRESHOLD_KRW = 3_000_000_000
DEFAULT_RECENT_NEW_MONTHS = 6
DEFAULT_RECENT_NEW_MIN_SALES_KRW = 100_000_000


@dataclass(frozen=True)
class MetricBrandRow:
    brand_key: str
    brand_name: str
    source: str
    atc4_code: str | None
    raw_value_history: dict[str, float]


def normalize_name(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def parse_history(value: Any) -> dict[str, float]:
    if isinstance(value, dict):
        raw = value
    else:
        raw = json.loads(str(value or "{}"))
    out: dict[str, float] = {}
    for period, amount in raw.items():
        try:
            out[str(period)] = float(amount or 0)
        except (TypeError, ValueError):
            out[str(period)] = 0.0
    return out


def latest_periods(rows: Iterable[MetricBrandRow], *, months: int) -> list[str]:
    periods = sorted({period for row in rows for period in row.raw_value_history})
    return periods[-months:]


def first_positive_period(history: dict[str, float]) -> str | None:
    for period in sorted(history):
        if history[period] > 0:
            return period
    return None


def stable_weekday_slice(brand_key: str, *, modulo: int = 7) -> int:
    digest = hashlib.sha256(brand_key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulo


def select_tier2_brands(
    rows: list[MetricBrandRow],
    *,
    sales_threshold_krw: int = DEFAULT_SALES_THRESHOLD_KRW,
    recent_new_months: int = DEFAULT_RECENT_NEW_MONTHS,
    recent_new_min_sales_krw: int = DEFAULT_RECENT_NEW_MIN_SALES_KRW,
    jw_brand_names: set[str] | None = None,
) -> list[Tier2Brand]:
    jw_lookup = {normalize_name(name) for name in (jw_brand_names or set())}
    recent_12 = set(latest_periods(rows, months=12))
    recent_new_window = set(latest_periods(rows, months=recent_new_months))
    by_name: dict[str, tuple[float, Tier2Brand]] = {}

    for row in rows:
        if normalize_name(row.brand_name) in jw_lookup:
            continue
        recent_sales = sum(row.raw_value_history.get(period, 0.0) for period in recent_12)
        first_positive = first_positive_period(row.raw_value_history)
        reason: str | None = None
        if recent_sales >= sales_threshold_krw:
            reason = f"sales_ge_{sales_threshold_krw}"
        elif (
            first_positive
            and first_positive in recent_new_window
            and sum(row.raw_value_history.get(period, 0.0) for period in recent_new_window)
            >= recent_new_min_sales_krw
        ):
            reason = f"first_nonzero_recent_{recent_new_months}m"
        if reason is None:
            continue

        brand = Tier2Brand(
            brand_name=row.brand_name,
            brand_key=row.brand_key,
            source=row.source,
            atc4_code=row.atc4_code,
            reason=reason,
        )
        key = normalize_name(row.brand_name)
        previous = by_name.get(key)
        if previous is None or recent_sales > previous[0]:
            by_name[key] = (recent_sales, brand)

    def sort_key(pair: tuple[float, Tier2Brand]) -> tuple[int, str]:
        _sales, brand = pair
        reason_rank = 0 if (brand.reason or "").startswith("sales_ge_") else 1
        return reason_rank, brand.brand_name

    return [item[1] for item in sorted(by_name.values(), key=sort_key)]


def load_metric_rows_from_db(
    *,
    db_host: str,
    db_port: int,
    db_user: str,
    db_password: str,
    db_name: str,
    sources: tuple[str, ...] = ("ubist", "iqvia_nsa"),
) -> list[MetricBrandRow]:
    conn = pymysql.connect(
        host=db_host,
        port=db_port,
        user=db_user,
        password=db_password,
        database=db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    placeholders = ", ".join(["%s"] * len(sources))
    sql = f"""
        SELECT brand_key, brand_name, source, atc4_code, raw_value_history
        FROM mart_general_brand_metric
        WHERE measure = 'sales'
          AND source IN ({placeholders})
    """
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, sources)
            rows = cursor.fetchall()
    finally:
        conn.close()
    return [
        MetricBrandRow(
            brand_key=str(row["brand_key"]),
            brand_name=str(row["brand_name"]),
            source=str(row["source"]),
            atc4_code=str(row["atc4_code"]) if row.get("atc4_code") else None,
            raw_value_history=parse_history(row.get("raw_value_history")),
        )
        for row in rows
    ]


def load_jw_brand_names(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(item.get("name") or item.get("brand_name") or item) for item in data}
        if isinstance(data, dict):
            names = {str(key) for key in data}
            for value in data.values():
                if isinstance(value, dict):
                    for column in ("name", "brand_name", "canonical_name", "merge_name"):
                        if value.get(column):
                            names.add(str(value[column]))
            return names
    if path.suffix.lower() == ".parquet":
        import pandas as pd

        frame = pd.read_parquet(path)
        names: set[str] = set()
        for column in ("name", "brand_name", "canonical_name", "merge_name"):
            if column in frame.columns:
                names.update(str(value) for value in frame[column].dropna().tolist())
        return names
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def brands_for_weekday(brands: list[Tier2Brand], weekday: int) -> list[Tier2Brand]:
    return [brand for brand in brands if stable_weekday_slice(brand.brand_key) == weekday]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jw-catalog", type=Path)
    parser.add_argument("--weekday", type=int, choices=range(7))
    parser.add_argument("--sales-threshold-krw", type=int, default=DEFAULT_SALES_THRESHOLD_KRW)
    parser.add_argument("--recent-new-months", type=int, default=DEFAULT_RECENT_NEW_MONTHS)
    parser.add_argument("--recent-new-min-sales-krw", type=int, default=DEFAULT_RECENT_NEW_MIN_SALES_KRW)
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("DB_PORT", "3306")))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "jw_mart_d2_stage_20260630_r2"))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "root"))
    parser.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    args = parser.parse_args()

    rows = load_metric_rows_from_db(
        db_host=args.db_host,
        db_port=args.db_port,
        db_user=args.db_user,
        db_password=args.db_password,
        db_name=args.db_name,
    )
    selected = select_tier2_brands(
        rows,
        sales_threshold_krw=args.sales_threshold_krw,
        recent_new_months=args.recent_new_months,
        recent_new_min_sales_krw=args.recent_new_min_sales_krw,
        jw_brand_names=load_jw_brand_names(args.jw_catalog),
    )
    if args.weekday is not None:
        selected = brands_for_weekday(selected, args.weekday)
    payload = [brand.__dict__ | {"weekday_slice": stable_weekday_slice(brand.brand_key)} for brand in selected]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"tier2_brand_count": len(payload), "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
