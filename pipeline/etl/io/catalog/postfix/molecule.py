from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pandas as pd

from pipeline.etl.io.catalog._lib.expected_counts import expected_int

COMMENT_BRAND_IDS = {"sb_004_00015", "sb_012_00081", "sb_016_00059"}
EXPECTED_WORKLIST_ROWS = expected_int("postfix_molecule.worklist_rows")
EXPECTED_SB_ROWS = expected_int("postfix_molecule.strategic_brand_rows")
EXPECTED_CB_ROWS = expected_int("postfix_molecule.cd_brand_rows")
EXPECTED_SB_ACTIONS = (801, 246)
EXPECTED_CB_ACTIONS = (702, 205)
PROTECT_EXISTING_MOLECULE_MARKETS = {"ml_006", "cd_006"}


def _clean_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def load_worklist(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != EXPECTED_WORKLIST_ROWS:
        raise RuntimeError(f"worklist row count != {EXPECTED_WORKLIST_ROWS}: {len(rows)}")
    return rows


def apply_level(df: pd.DataFrame, rows: list[dict[str, str]], *, level: str) -> tuple[pd.DataFrame, int, int]:
    df = df.copy()
    df = df[~df["brand_id"].isin(COMMENT_BRAND_IDS)].copy()
    index_by_brand_id = {str(brand_id): idx for idx, brand_id in df["brand_id"].items()}
    update_count = 0
    setnull_count = 0
    missing: list[str] = []
    for row in rows:
        if row["level"] != level:
            continue
        action = row["action"]
        if action not in {"UPDATE", "SET_NULL"}:
            continue
        brand_id = row["brand_id"]
        idx = index_by_brand_id.get(brand_id)
        if idx is None:
            missing.append(brand_id)
            continue
        if action == "UPDATE":
            if row.get("market") not in PROTECT_EXISTING_MOLECULE_MARKETS:
                df.at[idx, "molecule"] = _clean_value(row.get("target_value"))
            update_count += 1
        elif action == "SET_NULL":
            df.at[idx, "molecule"] = None
            setnull_count += 1
    if missing:
        unique_missing = sorted(set(missing))
        raise RuntimeError(f"{level}: missing UPDATE/SET_NULL brand_id values: {unique_missing[:20]}")
    return df, update_count, setnull_count


def apply_product_level(df: pd.DataFrame, rows: list[dict[str, str]], *, level: str, brand_df: pd.DataFrame | None = None) -> tuple[pd.DataFrame, int, int]:
    df = df.copy()
    df = df[~df["brand_id"].isin(COMMENT_BRAND_IDS)].copy()
    if "molecule_raw" not in df.columns:
        df["molecule_raw"] = df["molecule"].map(_clean_value)
    else:
        missing_raw = df["molecule_raw"].map(_clean_value).isna()
        df.loc[missing_raw, "molecule_raw"] = df.loc[missing_raw, "molecule"].map(_clean_value)
    if "dosage_form_raw" not in df.columns:
        df["dosage_form_raw"] = df["dosage_form"].map(_clean_value)
    else:
        missing_dosage_raw = df["dosage_form_raw"].map(_clean_value).isna()
        df.loc[missing_dosage_raw, "dosage_form_raw"] = df.loc[missing_dosage_raw, "dosage_form"].map(_clean_value)
    update_count = 0
    setnull_count = 0
    for row in rows:
        if row["level"] != level:
            continue
        action = row["action"]
        if action not in {"UPDATE", "SET_NULL"}:
            continue
        mask = df["brand_id"].astype(str).eq(row["brand_id"])
        if not mask.any():
            continue
        if action == "UPDATE":
            if row.get("market") not in PROTECT_EXISTING_MOLECULE_MARKETS:
                df.loc[mask, "molecule"] = _clean_value(row.get("target_value"))
            update_count += int(mask.sum())
        elif action == "SET_NULL":
            df.loc[mask, "molecule"] = None
            setnull_count += int(mask.sum())
    if brand_df is not None and "brand_id" in brand_df.columns and "molecule" in brand_df.columns:
        molecule_by_brand = brand_df.set_index("brand_id")["molecule"].map(_clean_value)
        mapped_molecule = df["brand_id"].map(molecule_by_brand)
        has_molecule = mapped_molecule.notna()
        df.loc[has_molecule, "molecule"] = mapped_molecule.loc[has_molecule]
        df.loc[~has_molecule, "molecule"] = None
    if brand_df is not None and "brand_id" in brand_df.columns and "dosage_form" in brand_df.columns:
        dosage_by_brand = brand_df.set_index("brand_id")["dosage_form"].map(_clean_value)
        mapped_dosage = df["brand_id"].map(dosage_by_brand)
        has_dosage = mapped_dosage.notna()
        df.loc[has_dosage, "dosage_form"] = mapped_dosage.loc[has_dosage]
    return df, update_count, setnull_count


def apply_molecule_worklist(catalog_dir: Path, worklist_path: Path) -> dict[str, Any]:
    rows = load_worklist(worklist_path)
    sb_path = catalog_dir / "strategic_brand" / "strategic_brand.parquet"
    sp_path = catalog_dir / "strategic_product" / "strategic_product.parquet"
    cb_path = catalog_dir / "cd_brand" / "cd_brand.parquet"
    cp_path = catalog_dir / "cd_product" / "cd_product.parquet"
    df_sb, update_sb, setnull_sb = apply_level(pd.read_parquet(sb_path), rows, level="ml")
    if len(df_sb) != EXPECTED_SB_ROWS:
        raise RuntimeError(f"strategic_brand row count != {EXPECTED_SB_ROWS}: {len(df_sb)}")
    if (update_sb, setnull_sb) != EXPECTED_SB_ACTIONS:
        raise RuntimeError(f"strategic_brand action counts mismatch: {(update_sb, setnull_sb)}")
    df_sp, update_sp, setnull_sp = apply_product_level(pd.read_parquet(sp_path), rows, level="ml", brand_df=df_sb)
    df_cb, update_cb, setnull_cb = apply_level(pd.read_parquet(cb_path), rows, level="cd")
    if len(df_cb) != EXPECTED_CB_ROWS:
        raise RuntimeError(f"cd_brand row count != {EXPECTED_CB_ROWS}: {len(df_cb)}")
    if (update_cb, setnull_cb) != EXPECTED_CB_ACTIONS:
        raise RuntimeError(f"cd_brand action counts mismatch: {(update_cb, setnull_cb)}")
    df_cp, update_cp, setnull_cp = apply_product_level(pd.read_parquet(cp_path), rows, level="cd", brand_df=df_cb)
    df_sb.to_parquet(sb_path, index=False)
    df_sp.to_parquet(sp_path, index=False)
    df_cb.to_parquet(cb_path, index=False)
    df_cp.to_parquet(cp_path, index=False)
    return {
        "worklist_rows": len(rows),
        "strategic_brand_rows": len(df_sb),
        "strategic_brand_actions": [update_sb, setnull_sb],
        "strategic_product_rows": len(df_sp),
        "strategic_product_actions": [update_sp, setnull_sp],
        "cd_brand_rows": len(df_cb),
        "cd_brand_actions": [update_cb, setnull_cb],
        "cd_product_rows": len(df_cp),
        "cd_product_actions": [update_cp, setnull_cp],
    }
