from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd

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
    return {"rows": int(len(cd_brand)), "cd_count": int(cd_brand["cd_id"].nunique()), "canonical_rows": int(cd_brand["is_jw"].astype(bool).sum()) if "is_jw" in cd_brand.columns else 0, "counts_by_cd": {str(key): int(value) for key, value in counts.items()}}


def rebuild_cd_brand(catalog_dir: Path) -> dict[str, Any]:
    strategic_brand_path = catalog_dir / "strategic_brand" / "strategic_brand.parquet"
    cd_market_path = catalog_dir / "cd_market" / "cd_market.parquet"
    output_path = catalog_dir / "cd_brand" / "cd_brand.parquet"
    strategic_brand = pd.read_parquet(strategic_brand_path)
    cd_market = pd.read_parquet(cd_market_path)
    cd_brand = build_cd_brand(strategic_brand, cd_market)
    stats = validate_cd_brand(cd_brand, cd_market)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cd_brand.to_parquet(output_path, index=False)
    return stats
