#!/usr/bin/env python3
"""Apply Phase G molecule worklist to catalog parquet files.

This script updates only local parquet catalog files. It does not touch DBs.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd


WORKLIST_PATH = Path("/tmp/molecule_v4_worklist.csv")
SB_PATH = Path("output/catalog/strategic_brand/strategic_brand.parquet")
CB_PATH = Path("output/catalog/cd_brand/cd_brand.parquet")
COMMENT_BRAND_IDS = {"sb_004_00015", "sb_012_00081", "sb_016_00059"}


def _clean_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def _load_worklist() -> list[dict[str, str]]:
    with WORKLIST_PATH.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != 5437:
        raise RuntimeError(f"worklist row count != 5437: {len(rows)}")
    return rows


def _apply_level(df: pd.DataFrame, rows: list[dict[str, str]], *, level: str) -> tuple[pd.DataFrame, int, int]:
    df = df.copy()
    before = len(df)
    df = df[~df["brand_id"].isin(COMMENT_BRAND_IDS)].copy()
    print(f"{level}: comment rows removed: {before - len(df)}")

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
            df.at[idx, "molecule"] = _clean_value(row.get("target_value"))
            update_count += 1
        elif action == "SET_NULL":
            df.at[idx, "molecule"] = None
            setnull_count += 1

    if missing:
        unique_missing = sorted(set(missing))
        raise RuntimeError(f"{level}: missing UPDATE/SET_NULL brand_id values: {unique_missing[:20]}")

    return df, update_count, setnull_count


def main() -> None:
    rows = _load_worklist()

    df_sb = pd.read_parquet(SB_PATH)
    print(f"Before strategic_brand: {len(df_sb)} rows")
    df_sb, update_sb, setnull_sb = _apply_level(df_sb, rows, level="ml")
    print(f"After strategic_brand: {len(df_sb)} rows")
    print(f"sb UPDATE: {update_sb}, SET_NULL: {setnull_sb}")
    if len(df_sb) != 3874:
        raise RuntimeError(f"strategic_brand row count != 3874: {len(df_sb)}")
    if (update_sb, setnull_sb) != (801, 246):
        raise RuntimeError(f"strategic_brand action counts mismatch: {(update_sb, setnull_sb)}")

    sample_ids = [
        row["brand_id"]
        for row in rows
        if row["level"] == "ml" and row["action"] in {"UPDATE", "SET_NULL"}
    ][:10]
    print("\n=== sb sample after UPDATE ===")
    print(df_sb[df_sb["brand_id"].isin(sample_ids)][["brand_id", "name", "ml_id", "molecule"]].to_string())
    df_sb.to_parquet(SB_PATH, index=False)

    df_cb = pd.read_parquet(CB_PATH)
    print(f"\nBefore cd_brand: {len(df_cb)} rows")
    df_cb, update_cb, setnull_cb = _apply_level(df_cb, rows, level="cd")
    print(f"After cd_brand: {len(df_cb)} rows")
    print(f"cb UPDATE: {update_cb}, SET_NULL: {setnull_cb}")
    if len(df_cb) != 1559:
        raise RuntimeError(f"cd_brand row count != 1559: {len(df_cb)}")
    if (update_cb, setnull_cb) != (702, 205):
        raise RuntimeError(f"cd_brand action counts mismatch: {(update_cb, setnull_cb)}")
    df_cb.to_parquet(CB_PATH, index=False)

    print("\n=== Phase G-2 DONE ===")


if __name__ == "__main__":
    main()
