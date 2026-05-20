#!/usr/bin/env python3
"""Build dry-run strategic ML JSON mart rows from general v3 rows.

Phase 16-G-4-Fix-ETL-v3 is dry-run only. If general dry-run JSONL files are
missing, this script creates a bounded general dry-run for the requested ml_id
and then applies catalog filter/overlay logic.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from layer3_compute_general_v3 import (
    ALLOWED_SOURCES,
    compute_general,
    general_brand_jsonl_path,
    json_ready,
    read_jsonl,
    write_jsonl,
)
from ops_utils import configure_logging, find_project_root


LOGGER = configure_logging(__name__)
PROJECT_ROOT = find_project_root(Path(__file__).resolve())
CATALOG_DIR = PROJECT_ROOT / "output" / "catalog"
DRY_RUN_DIR = Path("/tmp")
ML_BRAND_JSONL = DRY_RUN_DIR / "strategic_ml_v3_{ml_id}_brand_rows.jsonl"
ML_MARKET_JSONL = DRY_RUN_DIR / "strategic_ml_v3_{ml_id}_market_rows.jsonl"


def load_catalogs() -> tuple[pd.DataFrame, pd.DataFrame]:
    ml_market = pd.read_parquet(CATALOG_DIR / "ml_market" / "ml_market.parquet")
    strategic_brand = pd.read_parquet(CATALOG_DIR / "strategic_brand" / "strategic_brand.parquet")
    return ml_market, strategic_brand


def source_rows_from_general_or_compute(source: str, ml_id: str, max_rows: int, limit_atc4: int | None) -> list[dict[str, Any]]:
    path = general_brand_jsonl_path(source, ml=ml_id)
    rows = read_jsonl(path)
    if rows:
        return rows
    brand_rows, _, _ = compute_general(source=source, dry_run=True, limit_atc4=limit_atc4, max_rows=max_rows, ml=ml_id)
    return brand_rows


def metric_periods(row: dict[str, Any]) -> list[str]:
    return sorted((row.get("metric_history") or {}).keys())


def latest_period(row: dict[str, Any]) -> str | None:
    periods = metric_periods(row)
    return periods[-1] if periods else None


def latest_metric(row: dict[str, Any], key: str) -> Any:
    period = latest_period(row)
    if not period:
        return None
    return (row.get("metric_history") or {}).get(period, {}).get(key)


def build_company_ranking(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_period: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        company = (row.get("by_dimension") or {}).get("company") or "Unknown"
        for period, metric in (row.get("metric_history") or {}).items():
            value = metric.get("raw_value") or 0
            by_period[period][company] += float(value)
    result: dict[str, list[dict[str, Any]]] = {}
    for period, values in by_period.items():
        total = sum(values.values())
        ranked = []
        for idx, (company, value) in enumerate(sorted(values.items(), key=lambda kv: kv[1], reverse=True), start=1):
            ranked.append({"company": company, "rank": idx, "raw_value": value, "ms": (value / total * 100) if total else None})
        result[period] = ranked[:20]
    return result


def build_market_row(ml_row: pd.Series, source: str, measure: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    market_size: dict[str, float] = defaultdict(float)
    hhi_series: dict[str, float] = {}
    brand_ranking: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ei_ms_matrix = []
    gc_ms_matrix = []

    for row in rows:
        for period, metric in (row.get("metric_history") or {}).items():
            market_size[period] += float(metric.get("raw_value") or 0)
            brand_ranking[period].append(
                {
                    "brand_id": row.get("brand_id"),
                    "brand": row.get("brand_name"),
                    "rank": metric.get("rank"),
                    "ms": metric.get("ms"),
                    "raw_value": metric.get("raw_value"),
                }
            )
        period = latest_period(row)
        if period:
            ext = (row.get("extended_metric_history") or {}).get(period, {})
            metric = (row.get("metric_history") or {}).get(period, {})
            if ext.get("hhi") is not None:
                hhi_series[period] = ext.get("hhi")
            ei_ms_matrix.append(
                {
                    "brand_id": row.get("brand_id"),
                    "brand": row.get("brand_name"),
                    "ms": metric.get("ms"),
                    "ei_5y": ext.get("ei_5y"),
                    "momentum_score": ext.get("momentum_score"),
                }
            )
            gc_ms_matrix.append(
                {
                    "brand_id": row.get("brand_id"),
                    "brand": row.get("brand_name"),
                    "ms": metric.get("ms"),
                    "growth_contribution": ext.get("growth_contribution"),
                }
            )

    ranking_clean = {
        period: sorted(items, key=lambda item: (item.get("rank") is None, item.get("rank") or 999999))[:20]
        for period, items in brand_ranking.items()
    }
    return {
        "ml_id": ml_row["ml_id"],
        "ml_name": ml_row.get("name"),
        "source": source,
        "measure": measure,
        "unit_label": rows[0].get("unit_label") if rows else "",
        "market_size_series": dict(market_size),
        "hhi_series_5y": hhi_series,
        "brand_ranking_stacked": ranking_clean,
        "company_ranking_stacked": build_company_ranking(rows),
        "company_concentration_trend": {},
        "ei_ms_matrix": ei_ms_matrix,
        "growth_contribution_ms_matrix": gc_ms_matrix,
        "growth_contribution": {},
        "analysis_levels": {},
        "level_top5_trend": {},
        "target_customer_competition": {},
        "payload": {"phase": "16-G-4-Fix-ETL-v3", "dry_run": True, "brand_rows": len(rows)},
    }


def compute_strategic_ml(ml_id: str, dry_run: bool, max_rows: int, limit_atc4: int | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not dry_run:
        raise RuntimeError("Phase 16-G-4-Fix-ETL-v3 is dry-run only; INSERT is deferred to Fix-Load")
    ml_market, strategic_brand = load_catalogs()
    ml_match = ml_market.loc[ml_market["ml_id"] == ml_id]
    if ml_match.empty:
        raise RuntimeError(f"unknown ml_id: {ml_id}")
    ml_row = ml_match.iloc[0]
    brands = strategic_brand.loc[strategic_brand["ml_id"] == ml_id].copy()
    brand_ids = set(brands["brand_id"].astype(str))
    brand_overlay = brands.set_index("brand_id").to_dict(orient="index")

    ml_brand_rows: list[dict[str, Any]] = []
    ml_market_rows: list[dict[str, Any]] = []
    for source in ALLOWED_SOURCES:
        general_rows = source_rows_from_general_or_compute(source, ml_id, max_rows=max_rows, limit_atc4=limit_atc4)
        selected = [row for row in general_rows if str(row.get("brand_id")) in brand_ids]
        if not selected:
            general_rows, _, _ = compute_general(source=source, dry_run=True, limit_atc4=limit_atc4, max_rows=max_rows, ml=ml_id)
            selected = [row for row in general_rows if str(row.get("brand_id")) in brand_ids]
        by_measure: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in selected:
            overlay = brand_overlay.get(str(row.get("brand_id")), {})
            copied = dict(row)
            copied.update(
                {
                    "ml_id": ml_id,
                    "is_jw": str(row.get("brand_id")) in set(brands.loc[brands["name"].astype(str).str.contains("리바로|가드|라베칸|제이클|타발리스|시그마트|악템라|페린젝트|베노훼럼|헴리브라|엔커버|위너프|플라주오피", na=False), "brand_id"].astype(str)),
                    "overlay_data": {
                        "catalog_source": "strategic_brand",
                        "class": overlay.get("class"),
                        "molecule": overlay.get("molecule"),
                        "dosage_form": overlay.get("dosage_form"),
                        "strength_pack": overlay.get("strength_pack"),
                        "nhi_type": overlay.get("nhi_type"),
                        "ox_gx": overlay.get("ox_gx"),
                        "company": overlay.get("판매사"),
                        "manufacturer": overlay.get("제조사"),
                    },
                }
            )
            ml_brand_rows.append(copied)
            by_measure[(copied["source"], copied["measure"])].append(copied)
        for (source_name, measure), rows in by_measure.items():
            ml_market_rows.append(build_market_row(ml_row, source_name, measure, rows))

    write_jsonl(ML_BRAND_JSONL.with_name(ML_BRAND_JSONL.name.format(ml_id=ml_id)), ml_brand_rows)
    write_jsonl(ML_MARKET_JSONL.with_name(ML_MARKET_JSONL.name.format(ml_id=ml_id)), ml_market_rows)
    stats = {
        "ml_id": ml_id,
        "catalog_brands": int(len(brands)),
        "brand_rows": len(ml_brand_rows),
        "market_rows": len(ml_market_rows),
        "sources": sorted({r["source"] for r in ml_brand_rows}),
        "measures": sorted({r["measure"] for r in ml_brand_rows}),
    }
    return ml_brand_rows, ml_market_rows, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ml", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-rows", type=int, default=250_000)
    parser.add_argument("--limit-atc4", type=int, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    brand_rows, market_rows, stats = compute_strategic_ml(args.ml, args.dry_run, args.max_rows, args.limit_atc4)
    print(f"\n=== {args.ml} strategic ML dry-run ===")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if brand_rows:
        print("sample brand row:")
        print(json.dumps(json_ready(brand_rows[0]), ensure_ascii=False)[:1200])
    if market_rows:
        print("sample market row:")
        print(json.dumps(json_ready(market_rows[0]), ensure_ascii=False)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
