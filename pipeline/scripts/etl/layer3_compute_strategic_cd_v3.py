#!/usr/bin/env python3
"""Build dry-run strategic CD JSON mart rows from general v3 rows."""

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

from layer3_compute_general_v3 import ALLOWED_SOURCES, compute_general, json_ready, write_jsonl
from layer3_compute_strategic_ml_v3 import build_market_row, source_rows_from_general_or_compute
from ops_utils import configure_logging, find_project_root


LOGGER = configure_logging(__name__)
PROJECT_ROOT = find_project_root(Path(__file__).resolve())
CATALOG_DIR = PROJECT_ROOT / "output" / "catalog"
DRY_RUN_DIR = Path("/tmp")
CD_BRAND_JSONL = DRY_RUN_DIR / "strategic_cd_v3_{cd_market_id}_brand_rows.jsonl"
CD_MARKET_JSONL = DRY_RUN_DIR / "strategic_cd_v3_{cd_market_id}_market_rows.jsonl"
OVERRIDE_COLS = ["class", "molecule", "dosage_form", "strength_pack", "nhi_type", "ox_gx", "fish_oil", "판매사", "제조사"]


def load_catalogs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    cd_market = pd.read_parquet(CATALOG_DIR / "cd_market" / "cd_market.parquet")
    cd_brand = pd.read_parquet(CATALOG_DIR / "cd_brand" / "cd_brand.parquet")
    cd_filter = pd.read_parquet(CATALOG_DIR / "cd_filter" / "cd_filter.parquet")
    return cd_market, cd_brand, cd_filter


def filter_payload(cd_row: pd.Series, cd_filter: pd.DataFrame) -> dict[str, Any]:
    filter_id = cd_row.get("cd_filter_id")
    match = cd_filter.loc[cd_filter["cd_filter_id"] == filter_id] if filter_id is not None else pd.DataFrame()
    if match.empty:
        return {"cd_filter_id": filter_id}
    row = match.iloc[0]
    return {
        "cd_filter_id": filter_id,
        "name": row.get("name"),
        "atc3": row.get("atc3"),
        "atc4": row.get("atc4"),
        "molecule": row.get("molecule"),
        "class": row.get("class"),
        "nhi": row.get("nhi"),
        "dosage_form": row.get("dosage_form"),
    }


def compute_strategic_cd(cd_market_id: str, dry_run: bool, max_rows: int, limit_atc4: int | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not dry_run:
        raise RuntimeError("Phase 16-G-4-Fix-ETL-v3 is dry-run only; INSERT is deferred to Fix-Load")
    cd_market, cd_brand, cd_filter = load_catalogs()
    cd_match = cd_market.loc[cd_market["cd_id"] == cd_market_id]
    if cd_match.empty:
        raise RuntimeError(f"unknown cd_market_id: {cd_market_id}")
    cd_row = cd_match.iloc[0]
    ml_id = cd_row.get("ml_id")
    market_brands = cd_brand.loc[cd_brand["cd_id"] == cd_market_id].copy()
    brand_ids = set(market_brands["brand_id"].astype(str))
    brand_overlay = market_brands.set_index("brand_id").to_dict(orient="index")
    cd_filter_info = filter_payload(cd_row, cd_filter)

    cd_brand_rows: list[dict[str, Any]] = []
    cd_market_rows: list[dict[str, Any]] = []
    for source in ALLOWED_SOURCES:
        general_rows = source_rows_from_general_or_compute(source, str(ml_id), max_rows=max_rows, limit_atc4=limit_atc4)
        if not general_rows:
            general_rows, _, _ = compute_general(source=source, dry_run=True, limit_atc4=limit_atc4, max_rows=max_rows, ml=str(ml_id))
        selected = [row for row in general_rows if str(row.get("brand_id")) in brand_ids]
        if not selected:
            general_rows, _, _ = compute_general(source=source, dry_run=True, limit_atc4=limit_atc4, max_rows=max_rows, ml=str(ml_id))
            selected = [row for row in general_rows if str(row.get("brand_id")) in brand_ids]
        by_measure: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in selected:
            overlay = brand_overlay.get(str(row.get("brand_id")), {})
            override_columns = {col: overlay.get(col) for col in OVERRIDE_COLS if pd.notna(overlay.get(col))}
            copied = dict(row)
            copied.update(
                {
                    "cd_market_id": cd_market_id,
                    "cd_brand_id": row.get("brand_id"),
                    "is_jw": str(row.get("brand_id")) in set(market_brands.loc[market_brands["name"].astype(str).str.contains("리바로|가드|라베칸|제이클|타발리스|시그마트|악템라|페린젝트|베노훼럼|헴리브라|엔커버|위너프|플라주오피", na=False), "brand_id"].astype(str)),
                    "cd_overlay": {
                        "filter": cd_filter_info,
                        "override_columns": override_columns,
                        "additional_classes": [v for v in [overlay.get("class"), cd_filter_info.get("class")] if pd.notna(v)],
                    },
                    "overlay_data": {
                        "catalog_source": "cd_brand",
                        "ml_id": overlay.get("ml_id"),
                        "cd_id": overlay.get("cd_id"),
                        **override_columns,
                    },
                }
            )
            cd_brand_rows.append(copied)
            by_measure[(copied["source"], copied["measure"])].append(copied)

        pseudo_market_row = pd.Series({"ml_id": cd_market_id, "name": cd_row.get("name")})
        for (source_name, measure), rows in by_measure.items():
            market_row = build_market_row(pseudo_market_row, source_name, measure, rows)
            market_row["cd_market_id"] = cd_market_id
            market_row["cd_market_name"] = cd_row.get("name")
            market_row.pop("ml_id", None)
            market_row.pop("ml_name", None)
            cd_market_rows.append(market_row)

    write_jsonl(CD_BRAND_JSONL.with_name(CD_BRAND_JSONL.name.format(cd_market_id=cd_market_id)), cd_brand_rows)
    write_jsonl(CD_MARKET_JSONL.with_name(CD_MARKET_JSONL.name.format(cd_market_id=cd_market_id)), cd_market_rows)
    stats = {
        "cd_market_id": cd_market_id,
        "ml_id": ml_id,
        "catalog_brands": int(len(market_brands)),
        "brand_rows": len(cd_brand_rows),
        "market_rows": len(cd_market_rows),
        "sources": sorted({r["source"] for r in cd_brand_rows}),
        "measures": sorted({r["measure"] for r in cd_brand_rows}),
    }
    return cd_brand_rows, cd_market_rows, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cd-market", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-rows", type=int, default=250_000)
    parser.add_argument("--limit-atc4", type=int, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    brand_rows, market_rows, stats = compute_strategic_cd(args.cd_market, args.dry_run, args.max_rows, args.limit_atc4)
    print(f"\n=== {args.cd_market} strategic CD dry-run ===")
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
