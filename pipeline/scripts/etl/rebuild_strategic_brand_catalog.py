#!/usr/bin/env python3
"""Validate and clean the expanded strategic brand catalog.

The Phase 12 catalog ground truth is already represented in
``strategic_brand.parquet`` at Korean brand grain.  This script makes that
rebuild step explicit and removes non-brand memo rows that cannot join to raw
brand facts.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from brand_key_normalize import extract_brand_base_name, normalize_brand_name
from ops_utils import find_project_root


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
STRATEGIC_BRAND_PATH = PROJECT_ROOT / "output" / "catalog" / "strategic_brand" / "strategic_brand.parquet"
MOLECULE_NAME_RE = re.compile(r"^[A-Z0-9][A-Z0-9 /().+-]*$")


def _has_korean(value: Any) -> bool:
    return bool(re.search(r"[가-힣]", str(value or "")))


def clean_strategic_brand(catalog: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    names = catalog["name"].fillna("").astype(str)
    remove_mask = ~names.map(_has_korean)
    cleaned = aggregate_to_brand_grain(catalog.loc[~remove_mask].copy()).reset_index(drop=True)
    removed = catalog.loc[remove_mask].copy()
    return cleaned[catalog.columns], removed


def _first_present(values: pd.Series) -> Any:
    for value in values:
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        text = str(value).strip()
        if text and text.lower() not in {"nan", "none", "null"}:
            return value
    return None


def _join_unique(values: pd.Series) -> str | None:
    seen: list[str] = []
    for value in values:
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        text = str(value).strip()
        if not text or text.lower() in {"nan", "none", "null"}:
            continue
        if text not in seen:
            seen.append(text)
    if not seen:
        return None
    return " | ".join(seen)


def _join_key_for_base_name(value: Any) -> str:
    text = str(value or "").replace("A+", "에이플러스").replace("a+", "에이플러스")
    return normalize_brand_name(text)


def aggregate_to_brand_grain(catalog: pd.DataFrame) -> pd.DataFrame:
    """Collapse product/SKU catalog rows to true brand rows per ML market."""

    if catalog.empty:
        return catalog

    working = catalog.copy()
    working["_base_name"] = working["name"].map(extract_brand_base_name)
    working.loc[working["_base_name"] == "", "_base_name"] = working.loc[working["_base_name"] == "", "name"]
    working["_base_key"] = working["_base_name"].map(_join_key_for_base_name)
    working["_is_jw_sort"] = working.get("is_jw", False).astype(bool).astype(int)
    working["_is_target_sort"] = working.get("is_target", False).astype(bool).astype(int)
    working = working.sort_values(["ml_id", "_base_key", "_is_jw_sort", "_is_target_sort", "brand_id"], ascending=[True, True, False, False, True])

    merged_rows: list[dict[str, Any]] = []
    for (_, _), part in working.groupby(["ml_id", "_base_key"], dropna=False, sort=False):
        first = part.iloc[0].to_dict()
        base_name = str(first["_base_name"] or first["name"])
        base_key = _join_key_for_base_name(base_name)
        row = {col: first.get(col) for col in catalog.columns}
        row["name"] = base_name
        row["merge_name"] = base_name
        row["canonical_name"] = base_name
        row["general_brand_key"] = base_key
        row["is_jw"] = bool(part["is_jw"].astype(bool).any()) if "is_jw" in part else False
        row["is_target"] = bool(part["is_target"].astype(bool).any()) if "is_target" in part else False
        for col in ("cd_id", "class", "molecule", "dosage_form", "strength_pack", "nhi_type", "ox_gx", "fish_oil", "판매사", "제조사"):
            if col in catalog.columns:
                row[col] = _join_unique(part[col]) if col in {"molecule", "dosage_form", "strength_pack", "nhi_type", "ox_gx", "fish_oil"} else _first_present(part[col])
        merged_rows.append(row)

    return pd.DataFrame(merged_rows, columns=catalog.columns)


def validate_strategic_brand(catalog: pd.DataFrame) -> dict[str, Any]:
    names = catalog["name"].fillna("").astype(str)
    molecule_like = catalog.loc[names.map(lambda value: bool(MOLECULE_NAME_RE.fullmatch(value)))]
    if not molecule_like.empty:
        sample = molecule_like[["ml_id", "brand_id", "name", "molecule"]].head(20).to_dict("records")
        raise ValueError(f"strategic_brand still contains molecule-like English rows: {sample}")

    non_korean = catalog.loc[~names.map(_has_korean)]
    if not non_korean.empty:
        sample = non_korean[["ml_id", "brand_id", "name"]].head(20).to_dict("records")
        raise ValueError(f"strategic_brand contains non-Korean brand names: {sample}")

    if catalog["brand_id"].duplicated().any():
        dupes = catalog.loc[catalog["brand_id"].duplicated(), "brand_id"].head(20).tolist()
        raise ValueError(f"strategic_brand.brand_id must be unique, duplicate sample={dupes}")

    counts = catalog.groupby("ml_id")["brand_id"].nunique().sort_index().to_dict()
    if len(counts) != 16:
        raise ValueError(f"expected 16 ml markets, found {len(counts)}")

    jw_count = int(catalog["is_jw"].astype(bool).sum()) if "is_jw" in catalog.columns else 0
    if jw_count != 25:
        raise ValueError(f"expected 25 JW canonical rows, found {jw_count}")

    return {
        "rows": int(len(catalog)),
        "ml_count": len(counts),
        "canonical_rows": jw_count,
        "counts_by_ml": {str(key): int(value) for key, value in counts.items()},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write output/catalog/strategic_brand/strategic_brand.parquet")
    parser.add_argument("--input", type=Path, default=STRATEGIC_BRAND_PATH)
    parser.add_argument("--output", type=Path, default=STRATEGIC_BRAND_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    catalog = pd.read_parquet(args.input)
    cleaned, removed = clean_strategic_brand(catalog)
    stats = validate_strategic_brand(cleaned)

    print("=== Phase 12 strategic_brand rebuild ===")
    print(f"input: {args.input}")
    print(f"output: {args.output}")
    print(f"rows_before: {len(catalog)}")
    print(f"rows_after: {stats['rows']}")
    print(f"removed_non_brand_rows: {len(removed)}")
    if not removed.empty:
        print(removed[["ml_id", "cd_id", "brand_id", "name"]].to_string(index=False))
    print(f"ml_count: {stats['ml_count']}")
    print(f"canonical_rows: {stats['canonical_rows']}")
    print("counts_by_ml:")
    for ml_id, count in stats["counts_by_ml"].items():
        print(f"  {ml_id}: {count}")

    if args.apply:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        cleaned.to_parquet(args.output, index=False)
        print(f"wrote: {args.output}")
    else:
        print("dry-run only; pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
