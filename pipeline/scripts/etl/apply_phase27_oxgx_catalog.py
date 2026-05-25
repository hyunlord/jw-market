#!/usr/bin/env python3
"""Apply Phase 27 Ox/Gx catalog enrichment from UBIST Generic values.

PL decision, 2026-05-25:
* Original + 개량신약(Super Generic) => Ox
* Generic + First Generic => Gx
* ml_006/ml_007/ml_008 are source-derived from UBIST Generic
* ml_011 already has Ox/Biosimilar/Gx and must not be overwritten
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.dataset as ds

try:
    import duckdb
except ImportError:  # pragma: no cover - fallback for minimal environments
    duckdb = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from brand_key_normalize import normalize_brand_name
from ops_utils import find_project_root


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
CATALOG_DIR = PROJECT_ROOT / "output" / "catalog"
UBIST_DIR = PROJECT_ROOT / "output" / "ubist"
SOURCE_DERIVED_MARKETS = {"ml_006", "ml_007", "ml_008"}
GENERIC_TO_OX_GX = {
    "Original": "Ox",
    "개량신약(Super Generic)": "Ox",
    "Generic": "Gx",
    "First Generic": "Gx",
}
ML011_EXPECTED_COUNTS = {"Ox": 14, "Biosimilar": 9, "Gx": 3}


def _present(value: Any) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return bool(text) and text.lower() not in {"nan", "none", "null", "<na>"}


def _norm(value: Any) -> str:
    return normalize_brand_name(str(value or ""))


def _generic_to_ox_gx(generic: Any) -> str | None:
    return GENERIC_TO_OX_GX.get(str(generic).strip())


def _source_market_target_keys(strategic_brand: pd.DataFrame) -> set[str]:
    keys: set[str] = set()
    source_rows = strategic_brand.loc[strategic_brand["ml_id"].astype(str).isin(SOURCE_DERIVED_MARKETS)]
    for _, row in source_rows.iterrows():
        keys.update(_catalog_lookup_keys(row))
    return keys


def build_ubist_generic_by_brand(ubist_dir: Path = UBIST_DIR, target_keys: set[str] | None = None) -> dict[str, str]:
    """Build a brand-level Generic lookup without materializing all UBIST rows.

    The UBIST parquet dataset is around 145M rows after full reload. Loading it
    into one pandas frame is fragile, so we scan only the columns needed here and
    aggregate per batch. When target_keys is provided, we keep counts only for
    catalog brands that can be affected by Phase 27.
    """
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    target_keys = target_keys or set()
    target_keys_by_length = sorted(target_keys, key=len, reverse=True)

    def add_count(raw_value: Any, generic: Any, count: int) -> None:
        key = _norm(raw_value)
        if not key:
            return
        if not target_keys:
            counters[key][str(generic)] += int(count)
            return
        if key in target_keys:
            counters[key][str(generic)] += int(count)
            return
        # UBIST often prefixes manufacturer or suffixes strength/form in the
        # product/brand field (for example "오가논 아토젯", "아토젯 정 10/10mg").
        # Prefer the longest catalog key to avoid matching "리바로" before
        # "리바로젯".
        for target_key in target_keys_by_length:
            if target_key and target_key in key:
                counters[target_key][str(generic)] += int(count)
                return

    if duckdb is not None:
        pattern = str((ubist_dir / "year=*" / "month=*" / "data.parquet").resolve()).replace("'", "''")
        values = ", ".join(f"'{value}'" for value in GENERIC_TO_OX_GX)
        query = f"""
            SELECT "제품", "브랜드", "Generic", COUNT(*) AS cnt
            FROM read_parquet('{pattern}', hive_partitioning = true)
            WHERE "Generic" IN ({values})
            GROUP BY 1, 2, 3
        """
        rows = duckdb.connect(database=":memory:").execute(query).fetchall()
        for product, brand, generic, count in rows:
            for value in (brand, product):
                add_count(value, generic, int(count))
        return {key: counter.most_common(1)[0][0] for key, counter in counters.items() if counter}

    remaining = set(target_keys)

    parquet_files = sorted(ubist_dir.glob("year=*/month=*/data.parquet"), reverse=True)
    for parquet_file in parquet_files:
        frame = pd.read_parquet(parquet_file, columns=["제품", "브랜드", "Generic"])
        frame = frame.loc[frame["Generic"].astype(str).isin(GENERIC_TO_OX_GX)]
        if frame.empty:
            continue

        for col in ("브랜드", "제품"):
            sub = frame[[col, "Generic"]].dropna()
            if sub.empty:
                continue
            sub = sub.copy()
            sub["_key"] = sub[col].map(_norm)
            sub = sub.loc[sub["_key"].astype(bool)]
            if remaining:
                sub = sub.loc[sub["_key"].isin(remaining)]
            elif target_keys:
                break
            if sub.empty:
                continue

            grouped = sub.groupby(["_key", "Generic"], dropna=True).size()
            for (key, generic), count in grouped.items():
                add_count(key, generic, int(count))
        if remaining:
            remaining -= set(counters)
            if not remaining:
                break

    if not target_keys:
        dataset = ds.dataset(ubist_dir, format="parquet", partitioning="hive")
        scanner = dataset.scanner(columns=["제품", "브랜드", "Generic"], batch_size=250_000)
        for batch in scanner.to_batches():
            frame = batch.to_pandas()
            frame = frame.loc[frame["Generic"].astype(str).isin(GENERIC_TO_OX_GX)]
            if frame.empty:
                continue
            for col in ("브랜드", "제품"):
                sub = frame[[col, "Generic"]].dropna()
                if sub.empty:
                    continue
                sub = sub.copy()
                sub["_key"] = sub[col].map(_norm)
                sub = sub.loc[sub["_key"].astype(bool)]
                if sub.empty:
                    continue
                grouped = sub.groupby(["_key", "Generic"], dropna=True).size()
                for (key, generic), count in grouped.items():
                    add_count(key, generic, int(count))

    return {key: counter.most_common(1)[0][0] for key, counter in counters.items() if counter}


def _catalog_lookup_keys(row: pd.Series) -> list[str]:
    keys = []
    for col in ("general_brand_key", "name", "merge_name", "canonical_name"):
        key = _norm(row.get(col))
        if key and key not in keys:
            keys.append(key)
    return keys


def _lookup_generic(row: pd.Series, generic_by_brand: dict[str, str]) -> str | None:
    for key in _catalog_lookup_keys(row):
        generic = generic_by_brand.get(key)
        if generic:
            return generic
    return None


def _validate_ml011_preserved(strategic_brand: pd.DataFrame) -> None:
    ml011 = strategic_brand.loc[strategic_brand["ml_id"].astype(str) == "ml_011"]
    counts = ml011["ox_gx"].value_counts(dropna=False).to_dict()
    for label, expected in ML011_EXPECTED_COUNTS.items():
        actual = int(counts.get(label, 0))
        if actual != expected:
            raise RuntimeError(f"ml_011 ox_gx changed for {label}: expected={expected} actual={actual} counts={counts}")


def apply_ox_gx(
    ml_market: pd.DataFrame,
    strategic_brand: pd.DataFrame,
    generic_by_brand: dict[str, str],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    ml_market = ml_market.copy()
    strategic_brand = strategic_brand.copy()
    before_ml011 = strategic_brand.loc[strategic_brand["ml_id"].astype(str) == "ml_011", "ox_gx"].value_counts(dropna=False).to_dict()

    ml_market.loc[ml_market["ml_id"].astype(str).isin(SOURCE_DERIVED_MARKETS), "analyze_ox_gx"] = True

    stats: dict[str, Any] = {"markets": {}, "ml011_before": {str(k): int(v) for k, v in before_ml011.items()}}
    missing: list[dict[str, Any]] = []
    for idx, row in strategic_brand.iterrows():
        ml_id = str(row.get("ml_id"))
        if ml_id not in SOURCE_DERIVED_MARKETS:
            continue

        raw_ox_gx = str(row.get("ox_gx")).strip() if _present(row.get("ox_gx")) else None
        if ml_id == "ml_006" and raw_ox_gx == "PTV Ox":
            strategic_brand.at[idx, "ox_gx"] = "Ox"
            continue

        generic = _lookup_generic(row, generic_by_brand)
        mapped = _generic_to_ox_gx(generic)
        if not mapped:
            missing.append({"ml_id": ml_id, "brand_id": row.get("brand_id"), "name": row.get("name"), "keys": _catalog_lookup_keys(row)})
            strategic_brand.at[idx, "ox_gx"] = "Gx"
            continue
        strategic_brand.at[idx, "ox_gx"] = mapped

    _validate_ml011_preserved(strategic_brand)

    for ml_id in sorted(SOURCE_DERIVED_MARKETS | {"ml_011"}):
        sub = strategic_brand.loc[strategic_brand["ml_id"].astype(str) == ml_id]
        stats["markets"][ml_id] = {
            "rows": int(len(sub)),
            "ox_gx": {str(k): int(v) for k, v in sub["ox_gx"].value_counts(dropna=False).to_dict().items()},
        }
    stats["source_missing_defaulted_gx"] = {
        "rows": len(missing),
        "sample": missing[:25],
    }
    return ml_market, strategic_brand, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write catalog parquet outputs")
    parser.add_argument("--catalog-dir", type=Path, default=CATALOG_DIR)
    parser.add_argument("--ubist-dir", type=Path, default=UBIST_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ml_path = args.catalog_dir / "ml_market" / "ml_market.parquet"
    sb_path = args.catalog_dir / "strategic_brand" / "strategic_brand.parquet"

    ml_market = pd.read_parquet(ml_path)
    strategic_brand = pd.read_parquet(sb_path)
    _validate_ml011_preserved(strategic_brand)
    target_keys = _source_market_target_keys(strategic_brand)
    generic_by_brand = build_ubist_generic_by_brand(args.ubist_dir, target_keys)
    ml_out, sb_out, stats = apply_ox_gx(ml_market, strategic_brand, generic_by_brand)

    print("=== Phase 27 Ox/Gx catalog enrichment ===")
    print(f"generic_lookup_keys: {len(generic_by_brand):,}")
    print(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True))

    if args.apply:
        ml_path.parent.mkdir(parents=True, exist_ok=True)
        sb_path.parent.mkdir(parents=True, exist_ok=True)
        ml_out.to_parquet(ml_path, index=False)
        sb_out.to_parquet(sb_path, index=False)
        print(f"wrote: {ml_path}")
        print(f"wrote: {sb_path}")
    else:
        print("dry-run only; pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
