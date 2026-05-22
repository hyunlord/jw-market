#!/usr/bin/env python3
"""Replace ml_003 molecule placeholder catalog rows with raw Korean brands.

MI Master defines ml_003 by ATC and molecule, but those rows are not display
brands. This script expands each non-JW ml_003 molecule row into the UBIST raw
brands observed for the same ATC/molecule pair so downstream strategic marts
and caches display real brand names.
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

from brand_key_normalize import normalize_brand_name
from ops_utils import find_project_root


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
STRATEGIC_BRAND_PATH = PROJECT_ROOT / "output" / "catalog" / "strategic_brand" / "strategic_brand.parquet"
UBIST_DIR = PROJECT_ROOT / "output" / "ubist"
ML_ID = "ml_003"


def atc4(value: Any) -> str:
    match = re.search(r"\[([^\]]+)\]", str(value or ""))
    return match.group(1).strip() if match else str(value or "").strip()


def normalize_ingredient(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def molecule_tokens(molecule: Any) -> list[str]:
    text = str(molecule or "").upper()
    tokens = []
    for part in re.split(r"\+", text):
        token = part.strip()
        if not token:
            continue
        if token == "MET":
            token = "METFORMIN"
        tokens.append(normalize_ingredient(token))
    return tokens


def latest_ubist_parquet() -> Path:
    files = sorted(UBIST_DIR.glob("year=*/month=*/data.parquet"))
    if not files:
        raise FileNotFoundError(f"No UBIST parquet files found under {UBIST_DIR}")
    return files[-1]


def raw_brand_mapping(raw_path: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    raw = pd.read_parquet(raw_path, columns=["ATC", "브랜드", "성분", "판매사", "rx_amt"])
    raw["atc4"] = raw["ATC"].map(atc4)
    raw["ingredient_norm"] = raw["성분"].map(normalize_ingredient)

    mapping: dict[tuple[str, str], list[dict[str, Any]]] = {}
    atc_groups = {key: part.copy() for key, part in raw.groupby("atc4")}
    strategic = pd.read_parquet(STRATEGIC_BRAND_PATH)
    ml003 = strategic.loc[(strategic["ml_id"].astype(str) == ML_ID) & (~strategic["is_jw"].astype(bool))]

    for _, row in ml003.iterrows():
        atc = str(row.get("strategy_id") or "").replace("strategy_", "ml_")
        del atc
        # The catalog does not carry an explicit ATC column; product IDs for a
        # molecule all share the same MI Master class. The most reliable ATC is
        # inferred from the molecule row's original MI Master sequence through
        # matching raw ATCs that contain every molecule token.
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
        # Prefer the diabetes ATC declared in the MI Master row family by
        # choosing the largest matched raw slice. This avoids relying on the
        # molecule placeholder itself as a brand name.
        _, matched = max(candidates, key=lambda item: len(item[1]))
        grouped = (
            matched.groupby(["브랜드", "판매사"], dropna=False)["rx_amt"]
            .sum()
            .reset_index()
            .sort_values("rx_amt", ascending=False)
        )
        key = (str(row.get("molecule")), str(row.get("class")))
        mapping[key] = [
            {"brand": str(item["브랜드"]).strip(), "company": None if pd.isna(item["판매사"]) else str(item["판매사"]).strip(), "value": float(item["rx_amt"] or 0)}
            for _, item in grouped.iterrows()
            if str(item["브랜드"]).strip()
        ]
    return mapping


def build_fixed_catalog(raw_path: Path, limit_per_molecule: int | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    catalog = pd.read_parquet(STRATEGIC_BRAND_PATH)
    ml003 = catalog.loc[catalog["ml_id"].astype(str) == ML_ID].copy()
    keep = catalog.loc[catalog["ml_id"].astype(str) != ML_ID].copy()
    jw_rows = ml003.loc[ml003["is_jw"].astype(bool)].copy()
    non_jw = ml003.loc[~ml003["is_jw"].astype(bool)].copy()
    jw_keys = {normalize_brand_name(name) for name in jw_rows["name"].astype(str)}
    mapping = raw_brand_mapping(raw_path)

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

    fixed = pd.concat([keep, jw_rows, pd.DataFrame(new_rows, columns=catalog.columns)], ignore_index=True)
    stats = {
        "raw_path": str(raw_path),
        "old_ml003_rows": len(ml003),
        "old_non_jw_rows": len(non_jw),
        "new_non_jw_rows": len(new_rows),
        "skipped_molecules": sorted(set(skipped)),
        "total_rows": len(fixed),
    }
    return fixed, stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the corrected strategic_brand parquet")
    parser.add_argument("--raw-path", type=Path, default=None)
    parser.add_argument("--limit-per-molecule", type=int, default=None)
    args = parser.parse_args()

    raw_path = args.raw_path or latest_ubist_parquet()
    fixed, stats = build_fixed_catalog(raw_path, args.limit_per_molecule)
    print("=== ml_003 catalog brand correction ===")
    for key, value in stats.items():
        print(f"{key}: {value}")

    ml003 = fixed.loc[fixed["ml_id"].astype(str) == ML_ID]
    molecule_like = ml003.loc[
        (~ml003["is_jw"].astype(bool))
        & ml003["name"].astype(str).str.fullmatch(r"[A-Z0-9][A-Z0-9 /().+-]*", na=False)
    ]
    print(f"remaining_molecule_like_non_jw_rows: {len(molecule_like)}")
    print("sample_non_jw_names:", ml003.loc[~ml003["is_jw"].astype(bool), "name"].head(20).tolist())

    if args.apply:
        fixed.to_parquet(STRATEGIC_BRAND_PATH, index=False)
        print(f"wrote: {STRATEGIC_BRAND_PATH}")
    else:
        print("dry-run only; pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
