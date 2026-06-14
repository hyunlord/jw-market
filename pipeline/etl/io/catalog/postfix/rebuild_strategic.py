from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from pipeline.etl.io.catalog.postfix.text import extract_brand_base_name, normalize_brand_name

MOLECULE_NAME_RE = re.compile(r"^[A-Z0-9][A-Z0-9 /().+-]*$")


def _has_korean(value: Any) -> bool:
    return bool(re.search(r"[가-힣]", str(value or "")))


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
    return " | ".join(seen) if seen else None


def _json_array_union(values: pd.Series) -> str | None:
    merged: set[str] = set()
    for value in values:
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        if isinstance(value, list):
            merged.update(str(item).strip().upper() for item in value if str(item).strip())
            continue
        text = str(value or "").strip()
        if not text or text.lower() in {"nan", "none", "null", "<na>"}:
            continue
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError(f"allowed_atc4_codes_json must be a JSON array: {text!r}")
        merged.update(str(item).strip().upper() for item in parsed if str(item).strip())
    return json.dumps(sorted(merged), ensure_ascii=False) if merged else None


def _join_key_for_base_name(value: Any) -> str:
    text = str(value or "").replace("A+", "에이플러스").replace("a+", "에이플러스")
    return normalize_brand_name(text)


def aggregate_to_brand_grain(catalog: pd.DataFrame) -> pd.DataFrame:
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
        row = {col: first.get(col) for col in catalog.columns}
        row["name"] = base_name
        row["merge_name"] = base_name
        row["canonical_name"] = base_name
        row["general_brand_key"] = _join_key_for_base_name(base_name)
        row["is_jw"] = bool(part["is_jw"].astype(bool).any()) if "is_jw" in part else False
        row["is_target"] = bool(part["is_target"].astype(bool).any()) if "is_target" in part else False
        for col in ("cd_id", "class", "class_1", "class_2", "molecule", "dosage_form", "strength_pack", "nhi_type", "ox_gx", "fish_oil", "판매사", "제조사"):
            if col in catalog.columns:
                row[col] = _join_unique(part[col]) if col in {"molecule", "dosage_form", "strength_pack", "nhi_type", "ox_gx", "fish_oil"} else _first_present(part[col])
        if "allowed_atc4_codes_json" in catalog.columns:
            row["allowed_atc4_codes_json"] = _json_array_union(part["allowed_atc4_codes_json"])
        if "is_class_excluded" in catalog.columns:
            row["is_class_excluded"] = bool(part["is_class_excluded"].astype(bool).any())
        merged_rows.append(row)
    return pd.DataFrame(merged_rows, columns=catalog.columns)


def clean_strategic_brand(catalog: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    names = catalog["name"].fillna("").astype(str)
    remove_mask = ~names.map(_has_korean)
    cleaned = aggregate_to_brand_grain(catalog.loc[~remove_mask].copy()).reset_index(drop=True)
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
    return {"rows": int(len(catalog)), "ml_count": len(counts), "canonical_rows": jw_count, "counts_by_ml": {str(key): int(value) for key, value in counts.items()}}


def rebuild_strategic_brand(path: Path) -> dict[str, Any]:
    catalog = pd.read_parquet(path)
    cleaned, removed = clean_strategic_brand(catalog)
    stats = validate_strategic_brand(cleaned)
    cleaned.to_parquet(path, index=False)
    stats["rows_before"] = int(len(catalog))
    stats["removed_non_brand_rows"] = int(len(removed))
    return stats
