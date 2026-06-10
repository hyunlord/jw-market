#!/usr/bin/env python3
"""Apply Phase G molecule worklist corrections to catalog parquet files.

Updates the ``molecule`` column on ``strategic_brand`` and ``cd_brand`` based on
a worklist CSV with ``UPDATE`` / ``SET_NULL`` actions, and removes three known
discussion-memo brand rows that have ``molecule=NaN``. The script touches only
parquet files; DB tables are not modified.

Defaults align with the production worklist shipped at
``inputs/molecule_v4_worklist.csv``. Pass ``--apply`` to write changes; by
default the script runs in dry-run mode.

한글 운용 메모:
- 무엇: worklist는 과거 배치에서 빠진 dual-source molecule 표시를 보정하는
  후처리다.
- 왜: 4/22 기준 worklist를 5/18 catalog 위에 그대로 덮으면 ml_006/cd_006
  리바로젯 Molecule을 실제 성분 코드가 아니라 Statin/Statin-EZE class
  라벨로 재오염시킨다.
- 근거: 5/18 MI Master에서는 리바로젯 Class와 Molecule의 위치/의미가
  정정됐고, 제외 46/51은 catalog is_excluded가 담당한다.
- 기각: worklist 전체 삭제는 다른 시장의 보정 기능까지 없애므로, 이미
  권위 있는 molecule이 materialize된 시장만 보호한다.
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
DEFAULT_SP_PATH = PROJECT_ROOT / "output" / "catalog" / "strategic_product" / "strategic_product.parquet"
DEFAULT_CP_PATH = PROJECT_ROOT / "output" / "catalog" / "cd_product" / "cd_product.parquet"

COMMENT_BRAND_IDS = {"sb_004_00015", "sb_012_00081", "sb_016_00059"}
EXPECTED_WORKLIST_ROWS = 5437
EXPECTED_SB_ROWS = 3874
EXPECTED_CB_ROWS = 1559
EXPECTED_SB_ACTIONS = (801, 246)  # (UPDATE, SET_NULL)
EXPECTED_CB_ACTIONS = (702, 205)
# 260518 리바로/리바로젯은 MI Master에서 molecule이 이미 올바른 성분 코드로
# materialize된다. historical worklist가 이 값을 class 라벨로 덮는 경로만
# 막고, 다른 시장의 worklist 보정은 그대로 둔다.
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
            # 보호 시장은 catalog가 이미 최신 MI Master를 읽어 molecule을 정한다.
            # UPDATE를 건너뛰어 4/22 worklist가 5/18 molecule truth를 되돌리지
            # 못하게 한다. row를 삭제하는 대안은 worklist audit trail을 잃는다.
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


def apply_product_level(
    df: pd.DataFrame,
    rows: list[dict[str, str]],
    *,
    level: str,
    brand_df: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, int, int]:
    df = df.copy()
    before = len(df)
    df = df[~df["brand_id"].isin(COMMENT_BRAND_IDS)].copy()
    print(f"{level}_product: comment rows removed: {before - len(df)}")

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

    removed_orphans = 0
    if brand_df is not None and "brand_id" in brand_df.columns:
        valid_brand_ids = set(brand_df["brand_id"].astype(str))
        removed_orphans = int((~df["brand_id"].astype(str).isin(valid_brand_ids)).sum())
        print(f"{level}_product: orphan rows retained with null display molecule: {removed_orphans}")

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
            # product 단위도 brand 단위와 같은 보호 규칙을 적용한다.
            # SKU 행은 유지하되 molecule 덮어쓰기만 생략해야 strength/nhi
            # granularity가 유지된다.
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worklist", type=Path, default=DEFAULT_WORKLIST,
                        help=f"molecule correction worklist CSV (default: {DEFAULT_WORKLIST})")
    parser.add_argument("--strategic-brand", type=Path, default=DEFAULT_SB_PATH,
                        help=f"strategic_brand parquet (default: {DEFAULT_SB_PATH})")
    parser.add_argument("--cd-brand", type=Path, default=DEFAULT_CB_PATH,
                        help=f"cd_brand parquet (default: {DEFAULT_CB_PATH})")
    parser.add_argument("--strategic-product", type=Path, default=DEFAULT_SP_PATH,
                        help=f"strategic_product parquet (default: {DEFAULT_SP_PATH})")
    parser.add_argument("--cd-product", type=Path, default=DEFAULT_CP_PATH,
                        help=f"cd_product parquet (default: {DEFAULT_CP_PATH})")
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

    df_sp = pd.read_parquet(args.strategic_product)
    print(f"\nBefore strategic_product: {len(df_sp)} rows")
    df_sp, update_sp, setnull_sp = apply_product_level(df_sp, rows, level="ml", brand_df=df_sb)
    print(f"After strategic_product: {len(df_sp)} rows")
    print(f"sp product rows UPDATE: {update_sp}, SET_NULL: {setnull_sp}")

    df_cb = pd.read_parquet(args.cd_brand)
    print(f"\nBefore cd_brand: {len(df_cb)} rows")
    df_cb, update_cb, setnull_cb = apply_level(df_cb, rows, level="cd")
    print(f"After cd_brand: {len(df_cb)} rows")
    print(f"cb UPDATE: {update_cb}, SET_NULL: {setnull_cb}")
    if len(df_cb) != EXPECTED_CB_ROWS:
        raise RuntimeError(f"cd_brand row count != {EXPECTED_CB_ROWS}: {len(df_cb)}")
    if (update_cb, setnull_cb) != EXPECTED_CB_ACTIONS:
        raise RuntimeError(f"cd_brand action counts mismatch: {(update_cb, setnull_cb)}")

    df_cp = pd.read_parquet(args.cd_product)
    print(f"\nBefore cd_product: {len(df_cp)} rows")
    df_cp, update_cp, setnull_cp = apply_product_level(df_cp, rows, level="cd", brand_df=df_cb)
    print(f"After cd_product: {len(df_cp)} rows")
    print(f"cp product rows UPDATE: {update_cp}, SET_NULL: {setnull_cp}")

    if args.apply:
        df_sb.to_parquet(args.strategic_brand, index=False)
        print(f"wrote: {args.strategic_brand}")
        df_sp.to_parquet(args.strategic_product, index=False)
        print(f"wrote: {args.strategic_product}")
        df_cb.to_parquet(args.cd_brand, index=False)
        print(f"wrote: {args.cd_brand}")
        df_cp.to_parquet(args.cd_product, index=False)
        print(f"wrote: {args.cd_product}")
    else:
        print("dry-run only; pass --apply to write")

    print("\n=== Phase G molecule apply DONE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
