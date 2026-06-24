from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd

from pipeline.etl.io.catalog.postfix.text import normalize_brand_name

ML_ID = "ml_003"


def atc4(value: Any) -> str:
    match = re.search(r"\[([^\]]+)\]", str(value or ""))
    return match.group(1).strip() if match else str(value or "").strip()


def normalize_ingredient(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def molecule_tokens(molecule: Any) -> list[str]:
    tokens: list[str] = []
    for part in re.split(r"\+", str(molecule or "").upper()):
        token = part.strip()
        if not token:
            continue
        if token == "MET":
            token = "METFORMIN"
        tokens.append(normalize_ingredient(token))
    return tokens


def latest_ubist_parquet(ubist_dir: Path) -> Path:
    files = sorted(ubist_dir.glob("year=*/month=*/data.parquet"))
    if not files:
        raise FileNotFoundError(f"No UBIST parquet files found under {ubist_dir}")
    return files[-1]


def raw_brand_mapping(raw_path: Path, strategic_brand_path: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    raw = pd.read_parquet(raw_path, columns=["ATC", "브랜드", "성분", "판매사", "rx_amt"])
    raw["atc4"] = raw["ATC"].map(atc4)
    raw["ingredient_norm"] = raw["성분"].map(normalize_ingredient)
    atc_groups = {key: part.copy() for key, part in raw.groupby("atc4")}
    strategic = pd.read_parquet(strategic_brand_path)
    ml003 = strategic.loc[(strategic["ml_id"].astype(str) == ML_ID) & (~strategic["is_jw"].astype(bool))]
    mapping: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for _, row in ml003.iterrows():
        tokens = molecule_tokens(row.get("molecule"))
        if not tokens:
            continue
        candidates: list[tuple[str, pd.DataFrame]] = []
        for raw_atc, part in atc_groups.items():
            matched = part
            for token in tokens:
                matched = matched.loc[matched["ingredient_norm"].str.contains(token, na=False)]
            if not matched.empty:
                candidates.append((str(raw_atc), matched))
        if not candidates:
            continue
        _, matched = max(candidates, key=lambda item: len(item[1]))
        grouped = matched.groupby(["브랜드", "판매사"], dropna=False)["rx_amt"].sum().reset_index().sort_values("rx_amt", ascending=False)
        key = (str(row.get("molecule")), str(row.get("class")))
        mapping[key] = [
            {"brand": str(item["브랜드"]).strip(), "company": None if pd.isna(item["판매사"]) else str(item["판매사"]).strip(), "value": float(item["rx_amt"] or 0)}
            for _, item in grouped.iterrows()
            if str(item["브랜드"]).strip()
        ]
    return mapping


def build_fixed_catalog(strategic_brand_path: Path, raw_path: Path, limit_per_molecule: int | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    catalog = pd.read_parquet(strategic_brand_path)
    ml003 = catalog.loc[catalog["ml_id"].astype(str) == ML_ID].copy()
    keep = catalog.loc[catalog["ml_id"].astype(str) != ML_ID].copy()
    jw_rows = ml003.loc[ml003["is_jw"].astype(bool)].copy()
    non_jw = ml003.loc[~ml003["is_jw"].astype(bool)].copy()
    atc4_expanded_rows = non_jw.loc[non_jw["brand_id"].astype(str).str.contains("_atc4_", na=False)].copy()
    jw_keys = {normalize_brand_name(name) for name in jw_rows["name"].astype(str)}
    mapping = raw_brand_mapping(raw_path, strategic_brand_path)
    new_rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    seen: set[tuple[str, str]] = set()
    for _, row in non_jw.iterrows():
        key = (str(row.get("molecule")), str(row.get("class")))
        brands = mapping.get(key, [])
        if limit_per_molecule:
            brands = brands[:limit_per_molecule]
        if not brands:
            skipped.append(str(row.get("molecule")))
            continue
        for item in brands:
            brand_name = item["brand"]
            brand_key = normalize_brand_name(brand_name)
            if brand_key in jw_keys:
                continue
            dedupe_key = (brand_key, str(row.get("molecule")))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            record = row.to_dict()
            record["brand_id"] = f"{row.get('brand_id')}_raw_{len(new_rows) + 1:05d}"
            record["name"] = brand_name
            record["merge_name"] = brand_name
            record["canonical_name"] = ""
            record["general_brand_key"] = brand_key
            record["판매사"] = item.get("company")
            record["is_jw"] = False
            record["is_target"] = False
            if str(record.get("molecule")).upper() == "TIRZEPATIDE":
                record["class"] = "GLP-1RA"
            new_rows.append(record)
    fixed = pd.concat([keep, jw_rows, pd.DataFrame(new_rows, columns=catalog.columns), atc4_expanded_rows], ignore_index=True)
    stats = {
        "raw_path": str(raw_path),
        "old_ml003_rows": len(ml003),
        "old_non_jw_rows": len(non_jw),
        "new_non_jw_rows": len(new_rows),
        "preserved_atc4_expanded_rows": len(atc4_expanded_rows),
        "skipped_molecules": sorted(set(skipped)),
        "total_rows": len(fixed),
    }
    return fixed, stats


def apply_ml003(catalog_dir: Path, ubist_dir: Path) -> dict[str, Any]:
    strategic_brand_path = catalog_dir / "strategic_brand" / "strategic_brand.parquet"
    fixed, stats = build_fixed_catalog(strategic_brand_path, latest_ubist_parquet(ubist_dir))
    fixed.to_parquet(strategic_brand_path, index=False)
    return stats
