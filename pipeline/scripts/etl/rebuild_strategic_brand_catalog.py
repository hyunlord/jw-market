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

from ops_utils import find_project_root


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
STRATEGIC_BRAND_PATH = PROJECT_ROOT / "output" / "catalog" / "strategic_brand" / "strategic_brand.parquet"
MOLECULE_NAME_RE = re.compile(r"^[A-Z0-9][A-Z0-9 /().+-]*$")


def _has_korean(value: Any) -> bool:
    return bool(re.search(r"[가-힣]", str(value or "")))


def clean_strategic_brand(catalog: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    names = catalog["name"].fillna("").astype(str)
    remove_mask = ~names.map(_has_korean)
    cleaned = catalog.loc[~remove_mask].copy().reset_index(drop=True)
    removed = catalog.loc[remove_mask].copy()
    return cleaned[catalog.columns], removed


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
