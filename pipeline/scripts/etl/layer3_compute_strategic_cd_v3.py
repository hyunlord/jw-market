#!/usr/bin/env python3
"""Build and load strategic CD JSON marts from general-view rows."""

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

from brand_key_normalize import normalize_brand_name
from layer3_compute_general_v3 import ALLOWED_SOURCES, dumps, general_brand_jsonl_path, json_ready, mariadb_connect, read_jsonl, write_jsonl
from layer3_compute_market_metric import compute_market_mart_payload
from layer3_compute_strategic_ml_v3 import (
    _display_brand_name,
    _output_brand_key,
    _truthy,
    delete_existing_rows,
    expected_measure_pairs,
    fetch_general_rows_from_db,
    is_jw_name,
    insert_rows,
    recompute_market_scoped_metric_history,
)
from ops_utils import configure_logging, find_project_root


LOGGER = configure_logging(__name__)
PROJECT_ROOT = find_project_root(Path(__file__).resolve())
CATALOG_DIR = PROJECT_ROOT / "output" / "catalog"
DRY_RUN_DIR = Path("/tmp")
CD_BRAND_JSONL = "strategic_cd_v3_brand_rows.jsonl"
CD_MARKET_JSONL = "strategic_cd_v3_market_rows.jsonl"
OVERRIDE_COLS = ["class", "molecule", "dosage_form", "strength_pack", "nhi_type", "ox_gx", "fish_oil", "판매사", "제조사"]
CD_BRAND_COLUMNS = [
    "cd_market_id",
    "cd_brand_id",
    "brand_key",
    "brand_name",
    "source",
    "measure",
    "is_jw",
    "unit_label",
    "metric_history",
    "extended_metric_history",
    "channel_data",
    "specialty_data",
    "by_dimension",
    "raw_value_history",
    "cd_overlay",
    "overlay_data",
    "payload",
]
CD_MARKET_COLUMNS = [
    "cd_market_id",
    "cd_market_name",
    "source",
    "measure",
    "unit_label",
    "market_size_series",
    "hhi_series_5y",
    "brand_ranking_stacked",
    "company_ranking_stacked",
    "company_concentration_trend",
    "ei_ms_matrix",
    "growth_contribution_ms_matrix",
    "growth_contribution",
    "analysis_levels",
    "level_top5_trend",
    "target_customer_competition",
    "payload",
]


def load_catalogs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    cd_market = pd.read_parquet(CATALOG_DIR / "cd_market" / "cd_market.parquet")
    cd_brand = pd.read_parquet(CATALOG_DIR / "cd_brand" / "cd_brand.parquet")
    cd_filter = pd.read_parquet(CATALOG_DIR / "cd_filter" / "cd_filter.parquet")
    if "general_brand_key" in cd_brand.columns:
        cd_brand["brand_key"] = cd_brand["general_brand_key"].fillna(cd_brand["name"]).map(normalize_brand_name)
    else:
        cd_brand["brand_key"] = cd_brand["name"].map(normalize_brand_name)
    return cd_market, cd_brand, cd_filter


def load_general_rows(output_dir: Path, source: str) -> list[dict[str, Any]]:
    rows = fetch_general_rows_from_db(source)
    if not rows:
        jsonl_rows = read_jsonl(general_brand_jsonl_path(source, output_dir))
        if jsonl_rows:
            raise RuntimeError(f"DB returned no {source} general rows while stale JSONL rows exist")
    return rows


def catalog_by_key(brands: pd.DataFrame) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    brands = brands.copy()
    if "is_jw" not in brands.columns:
        brands["is_jw"] = False
    brands["_jw_sort"] = brands["is_jw"].map(_truthy).astype(int)
    brands = brands.sort_values(["_jw_sort", "brand_id"], ascending=[False, True])
    for key, part in brands.groupby("brand_key", dropna=False):
        if not key:
            continue
        first = part.iloc[0].to_dict()
        first["catalog_brand_ids"] = part["brand_id"].astype(str).tolist()
        first["catalog_names"] = part["name"].astype(str).tolist()
        grouped[str(key)] = first
    return grouped


def validate_market_completeness(cd_row: pd.Series, catalog_rows: pd.DataFrame, selected: list[dict[str, Any]]) -> None:
    expected_pairs = expected_measure_pairs(cd_row.get("data_source"))
    actual_pairs = {(str(row.get("source")), str(row.get("measure"))) for row in selected}
    missing_market_pairs = expected_pairs - actual_pairs

    jw_catalog = catalog_rows.loc[catalog_rows.get("is_jw", False).map(_truthy)] if "is_jw" in catalog_rows.columns else pd.DataFrame()
    missing_jw: list[str] = []
    for _, catalog_row in jw_catalog.iterrows():
        join_key = str(catalog_row.get("brand_key") or "")
        display = str(catalog_row.get("canonical_name") or catalog_row.get("name") or join_key)
        present = {
            (str(row.get("source")), str(row.get("measure")))
            for row in selected
            if row.get("_catalog_join_key") == join_key
        }
        missing_pairs = expected_pairs - present
        if missing_pairs:
            missing_jw.append(f"{display}:{sorted(missing_pairs)}")

    if missing_market_pairs or missing_jw:
        raise RuntimeError(
            f"Strategic CD completeness failed for {cd_row.get('cd_id')} "
            f"market_missing={sorted(missing_market_pairs)} jw_missing={missing_jw}"
        )


def filter_payload(cd_row: pd.Series, cd_filter: pd.DataFrame) -> dict[str, Any]:
    filter_id = cd_row.get("cd_filter_id")
    match = cd_filter.loc[cd_filter["cd_filter_id"] == filter_id] if filter_id is not None else pd.DataFrame()
    if match.empty:
        return {"cd_filter_id": filter_id}
    row = match.iloc[0]
    return {key: row.get(key) for key in ["cd_filter_id", "name", "atc3", "atc4", "molecule", "class", "nhi", "dosage_form"]}


def _group_by_source_measure(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("source")), str(row.get("measure")))].append(row)
    return grouped


def build_cd_rows(cd_row: pd.Series, catalog_rows: pd.DataFrame, cd_filter: pd.DataFrame, general_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cd_filter_info = filter_payload(cd_row, cd_filter)
    by_key = catalog_by_key(catalog_rows)
    expected_pairs = expected_measure_pairs(cd_row.get("data_source"))
    selected: list[dict[str, Any]] = []
    for row in general_rows:
        source_measure = (str(row.get("source")), str(row.get("measure")))
        if source_measure not in expected_pairs:
            continue
        overlay = by_key.get(str(row.get("brand_key")))
        if not overlay:
            continue
        override_columns = {col: overlay.get(col) for col in OVERRIDE_COLS if pd.notna(overlay.get(col))}
        copied = dict(row)
        display_name = _display_brand_name(copied, overlay)
        output_key = _output_brand_key(copied, overlay, display_name)
        dim = dict(copied.get("by_dimension") or {})
        dim.update({k: v for k, v in override_columns.items() if k not in {"판매사", "제조사"}})
        copied.update(
            {
                "cd_market_id": cd_row["cd_id"],
                "cd_brand_id": overlay.get("brand_id"),
                "brand_key": output_key,
                "brand_name": display_name,
                "is_jw": _truthy(overlay.get("is_jw")) if "is_jw" in overlay else is_jw_name(overlay.get("name")),
                "by_dimension": dim,
                "_catalog_join_key": str(overlay.get("brand_key") or row.get("brand_key") or ""),
                "cd_overlay": {
                    "filter": cd_filter_info,
                    "override_columns": override_columns,
                    "additional_classes": [v for v in [overlay.get("class"), cd_filter_info.get("class")] if pd.notna(v)],
                },
                "overlay_data": {
                    "catalog_source": "cd_brand",
                    "ml_id": overlay.get("ml_id"),
                    "cd_id": overlay.get("cd_id"),
                    "canonical_name": overlay.get("canonical_name"),
                    "general_brand_key": overlay.get("general_brand_key"),
                    "is_target": overlay.get("is_target"),
                    "catalog_brand_ids": overlay.get("catalog_brand_ids"),
                    "catalog_names": overlay.get("catalog_names"),
                    **override_columns,
                },
            }
        )
        selected.append(copied)
    validate_market_completeness(cd_row, catalog_rows, selected)
    for rows in _group_by_source_measure(selected).values():
        recompute_market_scoped_metric_history(rows)
    market_rows: list[dict[str, Any]] = []
    for (source, measure), rows in _group_by_source_measure(selected).items():
        payload = compute_market_mart_payload(rows, source=source, measure=measure, view_type="strategic_cd", catalog_market_row=cd_row.to_dict())
        market_rows.append(
            {
                "cd_market_id": cd_row["cd_id"],
                "cd_market_name": cd_row.get("name"),
                "source": source,
                "measure": measure,
                "unit_label": rows[0].get("unit_label") if rows else "",
                **payload,
            }
        )
    return selected, market_rows


def compute_strategic_cd(dry_run: bool, insert: bool, output_dir: Path, cd_market: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not dry_run and not insert:
        raise RuntimeError("Use --dry-run or --insert")
    cd_markets, cd_brand, cd_filter = load_catalogs()
    if cd_market:
        cd_markets = cd_markets.loc[cd_markets["cd_id"] == cd_market]
    all_general: list[dict[str, Any]] = []
    for source in ALLOWED_SOURCES:
        all_general.extend(load_general_rows(output_dir, source))
    brand_rows: list[dict[str, Any]] = []
    market_rows: list[dict[str, Any]] = []
    for _, row in cd_markets.iterrows():
        catalog_rows = cd_brand.loc[cd_brand["cd_id"] == row["cd_id"]].copy()
        rows, markets = build_cd_rows(row, catalog_rows, cd_filter, all_general)
        brand_rows.extend(rows)
        market_rows.extend(markets)
    if dry_run:
        write_jsonl(output_dir / CD_BRAND_JSONL, brand_rows)
        write_jsonl(output_dir / CD_MARKET_JSONL, market_rows)
    if insert:
        market_ids = {str(row["cd_id"]) for _, row in cd_markets.iterrows()}
        delete_existing_rows("mart_strategic_cd_brand_metric", "cd_market_id", market_ids)
        delete_existing_rows("mart_strategic_cd_market_metric", "cd_market_id", market_ids)
        insert_rows("mart_strategic_cd_brand_metric", CD_BRAND_COLUMNS, brand_rows, {"cd_market_id", "cd_brand_id", "source", "measure"})
        insert_rows("mart_strategic_cd_market_metric", CD_MARKET_COLUMNS, market_rows, {"cd_market_id", "source", "measure"})
    stats = {"brand_rows": len(brand_rows), "market_rows": len(market_rows), "cd_market_count": int(cd_markets["cd_id"].nunique())}
    return brand_rows, market_rows, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cd-market")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--insert", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DRY_RUN_DIR)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    brand_rows, market_rows, stats = compute_strategic_cd(args.dry_run, args.insert, args.output_dir, cd_market=args.cd_market)
    print("\n=== strategic CD v3.1 ===")
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
