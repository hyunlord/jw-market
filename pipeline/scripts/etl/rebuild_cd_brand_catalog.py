#!/usr/bin/env python3
"""Rebuild ``cd_brand`` from the expanded strategic brand catalog.

Phase 12 keeps ``catalog_ml_market`` and ``catalog_cd_market`` as immutable
market-definition inputs.  The strategic brand catalog is already expanded to
raw Korean brand names at the ``(atc, molecule)`` grain; this script materializes
the competitive-dynamics subset by selecting rows whose ``cd_id`` is present.
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
CATALOG_DIR = PROJECT_ROOT / "output" / "catalog"
STRATEGIC_BRAND_PATH = CATALOG_DIR / "strategic_brand" / "strategic_brand.parquet"
CD_BRAND_PATH = CATALOG_DIR / "cd_brand" / "cd_brand.parquet"
CD_MARKET_PATH = CATALOG_DIR / "cd_market" / "cd_market.parquet"
MOLECULE_NAME_RE = re.compile(r"^[A-Z0-9][A-Z0-9 /().+-]*$")


def _present(value: Any) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return bool(text) and text.lower() not in {"nan", "none", "null"}


def _has_korean(value: Any) -> bool:
    return bool(re.search(r"[가-힣]", str(value or "")))


def build_cd_brand(strategic_brand: pd.DataFrame, cd_market: pd.DataFrame) -> pd.DataFrame:
    """Return the competitive-dynamics catalog at Korean brand grain."""

    if "cd_id" not in strategic_brand.columns:
        raise ValueError("strategic_brand is missing cd_id")
    cd_ids = set(cd_market["cd_id"].dropna().astype(str))
    cd_brand = strategic_brand.loc[strategic_brand["cd_id"].map(_present)].copy()
    cd_brand = cd_brand.loc[cd_brand["cd_id"].astype(str).isin(cd_ids)].copy()
    cd_brand = cd_brand.loc[cd_brand["name"].map(_has_korean)].copy()
    cd_brand = cd_brand.sort_values(["cd_id", "is_jw", "brand_id"], ascending=[True, False, True]).reset_index(drop=True)
    return cd_brand[strategic_brand.columns]


def validate_cd_brand(cd_brand: pd.DataFrame, cd_market: pd.DataFrame) -> dict[str, Any]:
    cd_ids = set(cd_market["cd_id"].dropna().astype(str))
    bad_cd = sorted(set(cd_brand["cd_id"].dropna().astype(str)) - cd_ids)
    if bad_cd:
        raise ValueError(f"cd_brand contains unknown cd_id values: {bad_cd}")

    if cd_brand["brand_id"].duplicated().any():
        dupes = cd_brand.loc[cd_brand["brand_id"].duplicated(), "brand_id"].head(20).tolist()
        raise ValueError(f"cd_brand.brand_id must be unique, duplicate sample={dupes}")

    names = cd_brand["name"].fillna("").astype(str)
    molecule_like = cd_brand.loc[names.map(lambda value: bool(MOLECULE_NAME_RE.fullmatch(value)))]
    if not molecule_like.empty:
        sample = molecule_like[["cd_id", "brand_id", "name", "molecule"]].head(20).to_dict("records")
        raise ValueError(f"cd_brand still contains molecule-like English brand rows: {sample}")

    non_korean = cd_brand.loc[~names.map(_has_korean)]
    if not non_korean.empty:
        sample = non_korean[["cd_id", "brand_id", "name"]].head(20).to_dict("records")
        raise ValueError(f"cd_brand contains non-Korean brand names: {sample}")

    counts = cd_brand.groupby("cd_id")["brand_id"].nunique().sort_index().to_dict()
    missing_cd = sorted(cd_ids - set(counts))
    if missing_cd:
        raise ValueError(f"cd_brand has no rows for cd_id values: {missing_cd}")

    return {
        "rows": int(len(cd_brand)),
        "cd_count": int(cd_brand["cd_id"].nunique()),
        "canonical_rows": int(cd_brand["is_jw"].astype(bool).sum()) if "is_jw" in cd_brand.columns else 0,
        "counts_by_cd": {str(key): int(value) for key, value in counts.items()},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write output/catalog/cd_brand/cd_brand.parquet")
    parser.add_argument("--strategic-brand", type=Path, default=STRATEGIC_BRAND_PATH)
    parser.add_argument("--cd-market", type=Path, default=CD_MARKET_PATH)
    parser.add_argument("--output", type=Path, default=CD_BRAND_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    strategic_brand = pd.read_parquet(args.strategic_brand)
    cd_market = pd.read_parquet(args.cd_market)

    cd_brand = build_cd_brand(strategic_brand, cd_market)
    stats = validate_cd_brand(cd_brand, cd_market)

    print("=== Phase 12 cd_brand rebuild ===")
    print(f"source_strategic_brand: {args.strategic_brand}")
    print(f"output_cd_brand: {args.output}")
    print(f"rows: {stats['rows']}")
    print(f"cd_count: {stats['cd_count']}")
    print(f"canonical_rows: {stats['canonical_rows']}")
    print("counts_by_cd:")
    for cd_id, count in stats["counts_by_cd"].items():
        print(f"  {cd_id}: {count}")

    if args.apply:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        cd_brand.to_parquet(args.output, index=False)
        print(f"wrote: {args.output}")
    else:
        print("dry-run only; pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
