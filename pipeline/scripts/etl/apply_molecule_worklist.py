#!/usr/bin/env python3
"""Apply Phase G molecule worklist corrections to catalog parquet files.

Updates the ``molecule`` column on ``strategic_brand`` and ``cd_brand`` based on
a worklist CSV with ``UPDATE`` / ``SET_NULL`` actions, and removes three known
discussion-memo brand rows that have ``molecule=NaN``. The script touches only
parquet files; DB tables are not modified.

Defaults align with the production worklist shipped at
``inputs/molecule_v4_worklist.csv``. Pass ``--apply`` to write changes; by
default the script runs in dry-run mode.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_utils import find_project_root


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
DEFAULT_WORKLIST = PROJECT_ROOT / "inputs" / "molecule_v4_worklist.csv"
DEFAULT_SB_PATH = PROJECT_ROOT / "output" / "catalog" / "strategic_brand" / "strategic_brand.parquet"
DEFAULT_CB_PATH = PROJECT_ROOT / "output" / "catalog" / "cd_brand" / "cd_brand.parquet"

COMMENT_BRAND_IDS = {"sb_004_00015", "sb_012_00081", "sb_016_00059"}
EXPECTED_WORKLIST_ROWS = 5437
EXPECTED_SB_ROWS = 3874
EXPECTED_CB_ROWS = 1559
EXPECTED_SB_ACTIONS = (801, 246)  # (UPDATE, SET_NULL)
EXPECTED_CB_ACTIONS = (702, 205)


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worklist", type=Path, default=DEFAULT_WORKLIST,
                        help=f"molecule correction worklist CSV (default: {DEFAULT_WORKLIST})")
    parser.add_argument("--strategic-brand", type=Path, default=DEFAULT_SB_PATH,
                        help=f"strategic_brand parquet (default: {DEFAULT_SB_PATH})")
    parser.add_argument("--cd-brand", type=Path, default=DEFAULT_CB_PATH,
                        help=f"cd_brand parquet (default: {DEFAULT_CB_PATH})")
    parser.add_argument("--apply", action="store_true",
                        help="write changes (default: dry-run; only print summary)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = load_worklist(args.worklist)
    print(f"worklist: {args.worklist} ({len(rows)} rows)")

    df_sb = pd.read_parquet(args.strategic_brand)
    print(f"Before strategic_brand: {len(df_sb)} rows")
    df_sb, update_sb, setnull_sb = apply_level(df_sb, rows, level="ml")
    print(f"After strategic_brand: {len(df_sb)} rows")
    print(f"sb UPDATE: {update_sb}, SET_NULL: {setnull_sb}")
    if len(df_sb) != EXPECTED_SB_ROWS:
        raise RuntimeError(f"strategic_brand row count != {EXPECTED_SB_ROWS}: {len(df_sb)}")
    if (update_sb, setnull_sb) != EXPECTED_SB_ACTIONS:
        raise RuntimeError(f"strategic_brand action counts mismatch: {(update_sb, setnull_sb)}")

    sample_ids = [
        row["brand_id"]
        for row in rows
        if row["level"] == "ml" and row["action"] in {"UPDATE", "SET_NULL"}
    ][:10]
    print("\n=== sb sample after UPDATE ===")
    print(df_sb[df_sb["brand_id"].isin(sample_ids)][["brand_id", "name", "ml_id", "molecule"]].to_string())

    df_cb = pd.read_parquet(args.cd_brand)
    print(f"\nBefore cd_brand: {len(df_cb)} rows")
    df_cb, update_cb, setnull_cb = apply_level(df_cb, rows, level="cd")
    print(f"After cd_brand: {len(df_cb)} rows")
    print(f"cb UPDATE: {update_cb}, SET_NULL: {setnull_cb}")
    if len(df_cb) != EXPECTED_CB_ROWS:
        raise RuntimeError(f"cd_brand row count != {EXPECTED_CB_ROWS}: {len(df_cb)}")
    if (update_cb, setnull_cb) != EXPECTED_CB_ACTIONS:
        raise RuntimeError(f"cd_brand action counts mismatch: {(update_cb, setnull_cb)}")

    if args.apply:
        df_sb.to_parquet(args.strategic_brand, index=False)
        print(f"wrote: {args.strategic_brand}")
        df_cb.to_parquet(args.cd_brand, index=False)
        print(f"wrote: {args.cd_brand}")
    else:
        print("dry-run only; pass --apply to write")

    print("\n=== Phase G molecule apply DONE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
