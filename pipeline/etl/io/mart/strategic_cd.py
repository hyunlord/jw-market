from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .brand_key_normalize import normalize_brand_name
from .general_config import ALLOWED_SOURCES
from .general_db import ensure_json_columns
from .general_json import write_jsonl
from .layer3_compute_market_metric import compute_market_mart_payload
from .strategic_common import *
from .strategic_constants import CATALOG_DIR, CD_BRAND_COLUMNS, CD_BRAND_JSONL, CD_MARKET_COLUMNS, CD_MARKET_JSONL, OVERRIDE_COLS
from .strategic_dimension_apply import apply_cd_dimension_recode
from .strategic_scope import collapse_same_rows, group_by_source_measure, recompute_market_scoped_metric_history
from .strategic_ubist_channels import UBIST_CHANNEL_CONTRACT_COLUMNS, attach_ubist_channel_totals


def load_catalogs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    cd_market = pd.read_parquet(CATALOG_DIR / "cd_market" / "cd_market.parquet")
    cd_brand = pd.read_parquet(CATALOG_DIR / "cd_brand" / "cd_brand.parquet")
    cd_brand = drop_strict_excluded_rows(cd_brand, "cd_brand")
    cd_filter = pd.read_parquet(CATALOG_DIR / "cd_filter" / "cd_filter.parquet")
    if "general_brand_key" in cd_brand.columns:
        cd_brand["brand_key"] = cd_brand["general_brand_key"].fillna(cd_brand["name"]).map(normalize_brand_name)
    else:
        cd_brand["brand_key"] = cd_brand["name"].map(normalize_brand_name)
    return cd_market, cd_brand, cd_filter


def filter_payload(cd_row: pd.Series, cd_filter: pd.DataFrame) -> dict[str, Any]:
    filter_id = cd_row.get("cd_filter_id")
    match = cd_filter.loc[cd_filter["cd_filter_id"] == filter_id] if filter_id is not None else pd.DataFrame()
    if match.empty:
        return {"cd_filter_id": filter_id}
    row = match.iloc[0]
    return {key: row.get(key) for key in ["cd_filter_id", "name", "atc3", "atc4", "molecule", "class", "nhi", "dosage_form"]}


def validate_market_completeness(cd_row: pd.Series, catalog_rows: pd.DataFrame, selected: list[dict[str, Any]]) -> None:
    expected_pairs = expected_measure_pairs(cd_row.get("data_source"))
    actual_pairs = {(str(row.get("source")), str(row.get("measure"))) for row in selected}
    missing_market_pairs = expected_pairs - actual_pairs
    jw_catalog = catalog_rows.loc[catalog_rows.get("is_jw", False).map(truthy)] if "is_jw" in catalog_rows.columns else pd.DataFrame()
    missing_jw: list[str] = []
    for _, catalog_row in jw_catalog.iterrows():
        join_key = str(catalog_row.get("brand_key") or "")
        present = {(str(row.get("source")), str(row.get("measure"))) for row in selected if row.get("_catalog_join_key") == join_key}
        missing_pairs = expected_pairs - present
        if missing_pairs:
            display = str(catalog_row.get("canonical_name") or catalog_row.get("name") or join_key)
            missing_jw.append(f"{display}:{sorted(missing_pairs)}")
    if missing_market_pairs or missing_jw:
        raise RuntimeError(f"Strategic CD completeness failed for {cd_row.get('cd_id')} market_missing={sorted(missing_market_pairs)} jw_missing={missing_jw}")


def build_cd_rows(cd_row: pd.Series, catalog_rows: pd.DataFrame, cd_filter: pd.DataFrame, general_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    filter_info = filter_payload(cd_row, cd_filter)
    by_key = catalog_by_key(catalog_rows)
    selected: list[dict[str, Any]] = []
    for row in general_rows:
        if (str(row.get("source")), str(row.get("measure"))) not in expected_measure_pairs(cd_row.get("data_source")):
            continue
        overlay = by_key.get(str(row.get("brand_key")))
        if not overlay:
            continue
        allowed = set(parse_json_list(overlay.get("allowed_atc4_codes_json"))) or set(parse_json_list(filter_info.get("atc4")))
        aliases = allowed_atc4_aliases(allowed)
        code = row_atc4_code(row)
        if aliases and code and not (atc4_aliases(code) & aliases):
            continue
        copied = dict(row)
        display = display_brand_name(copied, overlay)
        override_columns = {col: overlay.get(col) for col in OVERRIDE_COLS if pd.notna(overlay.get(col))}
        dim = dict(copied.get("by_dimension") or {})
        dim.update({k: v for k, v in override_columns.items() if k in {"class", "class_1", "class_2"}})
        copied.update(
            {
                "cd_market_id": cd_row["cd_id"],
                "cd_brand_id": overlay.get("brand_id"),
                "brand_key": output_brand_key(copied, overlay, display),
                "brand_name": display,
                "is_jw": truthy(overlay.get("is_jw")) if "is_jw" in overlay else is_jw_name(overlay.get("name")),
                "by_dimension": dim,
                "dimension_data": copied.get("dimension_data") or {},
                "dimension_channel_data": copied.get("dimension_channel_data") or {},
                "_catalog_join_key": str(overlay.get("brand_key") or row.get("brand_key") or ""),
                "cd_overlay": {"filter": filter_info, "override_columns": override_columns, "additional_classes": [v for v in [overlay.get("class"), filter_info.get("class")] if pd.notna(v)]},
                "overlay_data": _cd_overlay(overlay, allowed, aliases, override_columns),
            }
        )
        attach_ubist_channel_totals(copied)
        selected.append(apply_cd_dimension_recode(copied, overlay, market_id=cd_row.get("cd_id")))
    selected = collapse_same_rows(selected, ("cd_market_id", "cd_brand_id"))
    validate_market_completeness(cd_row, catalog_rows, selected)
    for rows in group_by_source_measure(selected).values():
        recompute_market_scoped_metric_history(rows)
    return selected, _build_market_rows(cd_row, selected)


def _cd_overlay(overlay: dict[str, Any], allowed: set[str], aliases: set[str], override_columns: dict[str, Any]) -> dict[str, Any]:
    return {
        "catalog_source": "cd_brand",
        "ml_id": overlay.get("ml_id"),
        "cd_id": overlay.get("cd_id"),
        "canonical_name": overlay.get("canonical_name"),
        "general_brand_key": overlay.get("general_brand_key"),
        "is_target": overlay.get("is_target"),
        "catalog_brand_ids": overlay.get("catalog_brand_ids"),
        "catalog_names": overlay.get("catalog_names"),
        "allowed_atc4_codes": sorted(allowed),
        "allowed_atc4_aliases": sorted(aliases),
        **override_columns,
    }


def _build_market_rows(cd_row: pd.Series, selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for (source, measure), members in group_by_source_measure(selected).items():
        payload = compute_market_mart_payload(members, source=source, measure=measure, view_type="strategic_cd", catalog_market_row=cd_row.to_dict())
        rows.append({"cd_market_id": cd_row["cd_id"], "cd_market_name": cd_row.get("name"), "source": source, "measure": measure, "unit_label": members[0].get("unit_label") if members else "", **payload})
    return rows


def compute_strategic_cd(dry_run: bool, insert: bool, output_dir: Path, cd_market: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not dry_run and not insert:
        raise RuntimeError("Use --dry-run or --insert")
    cd_markets, cd_brand, cd_filter = load_catalogs()
    if cd_market:
        cd_markets = cd_markets.loc[cd_markets["cd_id"] == cd_market]
    atc_filter = _atc_filter_for_smoke(cd_markets, cd_brand, cd_filter) if cd_market else None
    all_general: list[dict[str, Any]] = []
    for source in ALLOWED_SOURCES:
        all_general.extend(load_general_rows(source, atc_filter))
    brand_rows: list[dict[str, Any]] = []
    market_rows: list[dict[str, Any]] = []
    for _, row in cd_markets.iterrows():
        rows, markets = build_cd_rows(row, cd_brand.loc[cd_brand["cd_id"] == row["cd_id"]].copy(), cd_filter, all_general)
        brand_rows.extend(rows)
        market_rows.extend(markets)
    if dry_run:
        write_jsonl(output_dir / CD_BRAND_JSONL, brand_rows)
        write_jsonl(output_dir / CD_MARKET_JSONL, market_rows)
    if insert:
        market_ids = {str(row["cd_id"]) for _, row in cd_markets.iterrows()}
        ensure_json_columns(
            "mart_strategic_cd_brand_metric",
            ("dimension_data", "dimension_channel_data", *UBIST_CHANNEL_CONTRACT_COLUMNS),
        )
        delete_existing_rows("mart_strategic_cd_brand_metric", "cd_market_id", market_ids)
        delete_existing_rows("mart_strategic_cd_market_metric", "cd_market_id", market_ids)
        insert_rows("mart_strategic_cd_brand_metric", CD_BRAND_COLUMNS, brand_rows, {"cd_market_id", "cd_brand_id", "source", "measure"})
        insert_rows("mart_strategic_cd_market_metric", CD_MARKET_COLUMNS, market_rows, {"cd_market_id", "source", "measure"})
    return brand_rows, market_rows, {"brand_rows": len(brand_rows), "market_rows": len(market_rows), "cd_market_count": int(cd_markets["cd_id"].nunique())}


def _atc_filter_for_smoke(cd_markets: pd.DataFrame, cd_brand: pd.DataFrame, cd_filter: pd.DataFrame) -> set[str]:
    codes: set[str] = set()
    for _, row in cd_markets.iterrows():
        info = filter_payload(row, cd_filter)
        for code in parse_json_list(info.get("atc4")):
            codes.update(atc4_aliases(code))
        part = cd_brand.loc[cd_brand["cd_id"] == row["cd_id"]]
        for value in part.get("allowed_atc4_codes_json", []):
            for code in parse_json_list(value):
                codes.update(atc4_aliases(code))
    return codes
