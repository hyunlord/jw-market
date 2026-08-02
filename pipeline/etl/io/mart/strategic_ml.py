from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .brand_key_normalize import normalize_brand_name
from .general_db import ensure_json_columns
from .general_json import write_jsonl
from .layer3_compute_market_metric import compute_market_mart_payload
from .strategic_common import (
    allowed_atc4_aliases,
    allowed_atc4_codes,
    atc4_aliases,
    catalog_by_key,
    delete_existing_rows,
    display_brand_name,
    drop_strict_excluded_rows,
    expected_measure_pairs,
    insert_rows,
    is_jw_name,
    load_general_rows,
    output_brand_key,
    parse_json_list,
    required_sources,
    row_atc4_code,
    truthy,
)
from .strategic_constants import ML_BRAND_COLUMNS, ML_BRAND_JSONL, ML_MARKET_COLUMNS, ML_MARKET_JSONL, catalog_file
from .strategic_dimension_apply import enhance_strategic_dimensions
from .strategic_dimensions import catalog_single_dimension_by_brand, load_ubist_dimension_context
from .strategic_scope import collapse_same_rows, group_by_source_measure, recompute_market_scoped_metric_history
from .strategic_ubist_channels import UBIST_CHANNEL_CONTRACT_COLUMNS, attach_ubist_channel_totals


def load_catalogs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    ml_market = pd.read_parquet(catalog_file("ml_market"))
    strategic_brand = pd.read_parquet(catalog_file("strategic_brand"))
    strategic_product = pd.read_parquet(catalog_file("strategic_product"))
    strategic_brand = drop_strict_excluded_rows(strategic_brand, "strategic_brand")
    strategic_product = drop_strict_excluded_rows(strategic_product, "strategic_product")
    if "general_brand_key" in strategic_brand.columns:
        strategic_brand["brand_key"] = strategic_brand["general_brand_key"].fillna(strategic_brand["name"]).map(normalize_brand_name)
    else:
        strategic_brand["brand_key"] = strategic_brand["name"].map(normalize_brand_name)
    return ml_market, strategic_brand, strategic_product


def validate_market_completeness(ml_row: pd.Series, catalog_rows: pd.DataFrame, selected: list[dict[str, Any]]) -> None:
    expected_pairs = expected_measure_pairs(ml_row.get("data_source"))
    actual_pairs = {(str(row.get("source")), str(row.get("measure"))) for row in selected}
    missing_market_pairs = expected_pairs - actual_pairs
    jw_catalog = catalog_rows.loc[catalog_rows.get("is_jw", False).map(truthy)] if "is_jw" in catalog_rows.columns else pd.DataFrame()
    missing_jw: list[str] = []
    for _, catalog_row in jw_catalog.iterrows():
        join_key = str(catalog_row.get("brand_key") or "")
        display = str(catalog_row.get("canonical_name") or catalog_row.get("name") or join_key)
        present = {(str(row.get("source")), str(row.get("measure"))) for row in selected if row.get("_catalog_join_key") == join_key}
        missing_pairs = expected_pairs - present
        if missing_pairs:
            missing_jw.append(f"{display}:{sorted(missing_pairs)}")
    if missing_market_pairs or missing_jw:
        raise RuntimeError(f"Strategic ML completeness failed for {ml_row.get('ml_id')} market_missing={sorted(missing_market_pairs)} jw_missing={missing_jw}")


def build_ml_rows(ml_row: pd.Series, catalog_rows: pd.DataFrame, general_rows: list[dict[str, Any]], dimension_context: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_key = catalog_by_key(catalog_rows)
    expected_pairs = expected_measure_pairs(ml_row.get("data_source"))
    selected: list[dict[str, Any]] = []
    for row in general_rows:
        if (str(row.get("source")), str(row.get("measure"))) not in expected_pairs:
            continue
        overlay = by_key.get(str(row.get("brand_key")))
        if not overlay:
            continue
        allowed = allowed_atc4_codes(overlay, ml_row)
        aliases = allowed_atc4_aliases(allowed)
        row_code = row_atc4_code(row)
        if aliases and row_code and not (atc4_aliases(row_code) & aliases):
            continue
        copied = dict(row)
        display = display_brand_name(copied, overlay)
        dim = dict(copied.get("by_dimension") or {})
        for key in ("class", "class_1", "class_2"):
            dim[key] = overlay.get(key)
        copied.update(
            {
                "ml_id": ml_row["ml_id"],
                "brand_id": overlay.get("brand_id"),
                "brand_key": output_brand_key(copied, overlay, display),
                "brand_name": display,
                "is_jw": truthy(overlay.get("is_jw")) if "is_jw" in overlay else is_jw_name(overlay.get("name")),
                "by_dimension": dim,
                "dimension_data": copied.get("dimension_data") or {},
                "dimension_channel_data": copied.get("dimension_channel_data") or {},
                "dimension_specialty_data": copied.get("dimension_specialty_data") or {},
                "_catalog_join_key": str(overlay.get("brand_key") or row.get("brand_key") or ""),
                "overlay_data": _ml_overlay(ml_row, overlay, allowed, aliases),
            }
        )
        attach_ubist_channel_totals(copied)
        selected.append(enhance_strategic_dimensions(copied, dimension_context, market_id=ml_row.get("ml_id")))
    selected = collapse_same_rows(selected, ("ml_id", "brand_id"))
    validate_market_completeness(ml_row, catalog_rows, selected)
    for rows in group_by_source_measure(selected).values():
        recompute_market_scoped_metric_history(rows)
    return selected, _build_market_rows(ml_row, selected)


def _ml_overlay(ml_row: pd.Series, overlay: dict[str, Any], allowed: set[str], aliases: set[str]) -> dict[str, Any]:
    return {
        "catalog_source": "strategic_brand",
        "ml_id": ml_row["ml_id"],
        "canonical_name": overlay.get("canonical_name"),
        "general_brand_key": overlay.get("general_brand_key"),
        "is_target": overlay.get("is_target"),
        "catalog_brand_ids": overlay.get("catalog_brand_ids"),
        "catalog_names": overlay.get("catalog_names"),
        "allowed_atc4_codes": sorted(allowed),
        "allowed_atc4_aliases": sorted(aliases),
        "is_class_excluded": truthy(overlay.get("is_class_excluded")),
        **{field: overlay.get(field) for field in ("class", "class_1", "class_2", "molecule", "dosage_form", "strength_pack", "nhi_type", "ox_gx", "fish_oil")},
    }


def _build_market_rows(ml_row: pd.Series, selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for (source, measure), members in group_by_source_measure(selected).items():
        payload = compute_market_mart_payload(members, source=source, measure=measure, view_type="strategic_ml", catalog_market_row=ml_row.to_dict())
        rows.append({"ml_id": ml_row["ml_id"], "ml_name": ml_row.get("name"), "source": source, "measure": measure, "unit_label": members[0].get("unit_label") if members else "", **payload})
    return rows


def compute_strategic_ml(dry_run: bool, insert: bool, output_dir: Path, ml: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not dry_run and not insert:
        raise RuntimeError("Use --dry-run or --insert")
    ml_market, strategic_brand, strategic_product = load_catalogs()
    if ml:
        ml_market = ml_market.loc[ml_market["ml_id"] == ml]
    brand_rows: list[dict[str, Any]] = []
    market_rows: list[dict[str, Any]] = []
    for _, row in ml_market.iterrows():
        catalog_rows = strategic_brand.loc[strategic_brand["ml_id"] == row["ml_id"]].copy()
        product_rows = strategic_product.loc[strategic_product["ml_id"] == row["ml_id"]].copy()
        atc_filter = _atc_filter_for_smoke(row.to_frame().T, catalog_rows)
        if not atc_filter:
            raise RuntimeError(f"Strategic ML ATC4 scope is empty for {row.get('ml_id')}")
        general_rows: list[dict[str, Any]] = []
        for source in required_sources(row.get("data_source")):
            general_rows.extend(load_general_rows(source, atc_filter))
        ubist_context = load_ubist_dimension_context(str(row["ml_id"]), product_rows)
        context = {**ubist_context, "brand_single_dimensions": catalog_single_dimension_by_brand(catalog_rows, product_rows)}
        rows, markets = build_ml_rows(row, catalog_rows, general_rows, context)
        brand_rows.extend(rows)
        market_rows.extend(markets)
    if dry_run:
        write_jsonl(output_dir / ML_BRAND_JSONL, brand_rows)
        write_jsonl(output_dir / ML_MARKET_JSONL, market_rows)
    if insert:
        market_ids = {str(row["ml_id"]) for _, row in ml_market.iterrows()}
        ensure_json_columns(
            "mart_strategic_ml_brand_metric",
            ("dimension_data", "dimension_channel_data", "dimension_specialty_data", *UBIST_CHANNEL_CONTRACT_COLUMNS),
        )
        delete_existing_rows("mart_strategic_ml_brand_metric", "ml_id", market_ids)
        delete_existing_rows("mart_strategic_ml_market_metric", "ml_id", market_ids)
        insert_rows("mart_strategic_ml_brand_metric", ML_BRAND_COLUMNS, brand_rows, {"ml_id", "brand_id", "source", "measure"})
        insert_rows("mart_strategic_ml_market_metric", ML_MARKET_COLUMNS, market_rows, {"ml_id", "source", "measure"})
    return brand_rows, market_rows, {"brand_rows": len(brand_rows), "market_rows": len(market_rows), "ml_count": int(ml_market["ml_id"].nunique())}


def _atc_filter_for_smoke(ml_market: pd.DataFrame, strategic_brand: pd.DataFrame) -> set[str]:
    codes: set[str] = set()
    for _, row in ml_market.iterrows():
        for code in parse_json_list(row.get("atc_codes_json")):
            codes.update(atc4_aliases(code))
        part = strategic_brand.loc[strategic_brand["ml_id"] == row["ml_id"]]
        for value in part.get("allowed_atc4_codes_json", []):
            for code in parse_json_list(value):
                codes.update(atc4_aliases(code))
    return codes
