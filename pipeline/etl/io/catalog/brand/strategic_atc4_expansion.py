from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json

import pandas as pd
from pipeline.etl.io.catalog.brand.strategic_brand_logic import clean_text, extract_atc_code
from pipeline.etl.io.catalog.postfix.text import extract_brand_base_name, normalize_brand_name
from pipeline.etl.io.mart.strategic_common import atc4_aliases


@dataclass
class RawAtc4Brand:
    name: str
    atc4_codes: set[str] = field(default_factory=set)
    source_views: set[str] = field(default_factory=set)


def parse_json_array(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip().upper() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "null", "<na>"}:
        return []
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError(f"expected JSON array, found={text!r}")
    return [str(item).strip().upper() for item in parsed if str(item).strip()]


def normalize_raw_brand_name(value: Any) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    base = extract_brand_base_name(text) or text
    if not base or not normalize_brand_name(base):
        return None
    return base


def _source_enabled(data_source: Any, source_view: str) -> bool:
    value = str(data_source or "").strip().lower()
    if source_view == "UBIST":
        return value in {"ubist", "both", "dual"}
    if source_view == "IQVIA":
        return value in {"iqvia", "iqvia_nsa", "both", "dual"}
    raise ValueError(f"unknown source_view={source_view!r}")


def _market_alias_index(ml_rows: list[dict[str, Any]], source_view: str) -> dict[str, list[str]]:
    alias_to_ml: dict[str, list[str]] = defaultdict(list)
    for row in ml_rows:
        if not _source_enabled(row.get("data_source"), source_view):
            continue
        ml_id = str(row["ml_id"])
        for code in parse_json_array(row.get("atc_codes_json")):
            for alias in atc4_aliases(code):
                if ml_id not in alias_to_ml[alias]:
                    alias_to_ml[alias].append(ml_id)
    return alias_to_ml


def _add_brand(
    result: dict[str, dict[str, RawAtc4Brand]],
    *,
    ml_id: str,
    atc4_code: str,
    source_view: str,
    brand_name: str,
) -> None:
    key = normalize_brand_name(brand_name)
    if not key:
        return
    entry = result[ml_id].setdefault(key, RawAtc4Brand(name=brand_name))
    if len(brand_name) < len(entry.name):
        entry.name = brand_name
    entry.atc4_codes.add(atc4_code.upper())
    entry.source_views.add(source_view)


def _load_ubist_brands(
    result: dict[str, dict[str, RawAtc4Brand]],
    ml_rows: list[dict[str, Any]],
    ubist_dir: Path,
) -> dict[str, Any]:
    paths = sorted(ubist_dir.glob("year=*/month=*/data.parquet"))
    if not paths:
        raise FileNotFoundError(f"no UBIST parquet partitions under {ubist_dir}")
    alias_to_ml = _market_alias_index(ml_rows, "UBIST")
    if not alias_to_ml:
        return {"paths": [str(path) for path in paths], "partitions": len(paths), "rows": 0, "matched_rows": 0}
    rows = 0
    matched = 0
    seen_rows: set[tuple[str, str, str]] = set()
    relevant_aliases = set(alias_to_ml)
    for path in paths:
        frame = pd.read_parquet(path, columns=["ATC", "브랜드", "제품"])
        rows += int(len(frame))
        frame["atc4_code"] = frame["ATC"].map(extract_atc_code).str.upper()
        frame = frame.loc[frame["atc4_code"].isin(relevant_aliases), ["atc4_code", "브랜드", "제품"]].drop_duplicates()
        for row in frame.itertuples(index=False):
            atc4_code = str(getattr(row, "atc4_code", "") or "").strip().upper()
            raw_brand = str(getattr(row, "브랜드", "") or "").strip()
            raw_product = str(getattr(row, "제품", "") or "").strip()
            seen_key = (atc4_code, raw_brand, raw_product)
            if seen_key in seen_rows:
                continue
            seen_rows.add(seen_key)
            ml_ids = alias_to_ml.get(atc4_code) or []
            brand_name = normalize_raw_brand_name(raw_brand) or normalize_raw_brand_name(raw_product)
            if brand_name is None:
                continue
            matched += 1
            for ml_id in ml_ids:
                _add_brand(result, ml_id=ml_id, atc4_code=atc4_code, source_view="UBIST", brand_name=brand_name)
    return {"paths": [str(path) for path in paths], "partitions": len(paths), "rows": rows, "matched_rows": matched}


def _load_iqvia_brands(
    result: dict[str, dict[str, RawAtc4Brand]],
    ml_rows: list[dict[str, Any]],
    iqvia_nsa_dir: Path,
) -> dict[str, Any]:
    paths = sorted(iqvia_nsa_dir.glob("*.parquet"))
    if not paths:
        raise FileNotFoundError(f"no IQVIA NSA parquet partitions under {iqvia_nsa_dir}")
    alias_to_ml = _market_alias_index(ml_rows, "IQVIA")
    if not alias_to_ml:
        return {"paths": [str(path) for path in paths], "partitions": len(paths), "rows": 0, "matched_rows": 0}
    rows = 0
    matched = 0
    seen_rows: set[tuple[str, str, str]] = set()
    relevant_aliases = set(alias_to_ml)
    for path in paths:
        frame = pd.read_parquet(path, columns=["atc4_code", "product_name_kor", "product_name"])
        rows += int(len(frame))
        frame["atc4_norm"] = frame["atc4_code"].map(extract_atc_code).str.upper()
        frame = frame.loc[frame["atc4_norm"].isin(relevant_aliases), ["atc4_norm", "product_name_kor", "product_name"]].drop_duplicates()
        for row in frame.itertuples(index=False):
            atc4_code = str(getattr(row, "atc4_norm", "") or "").strip().upper()
            raw_brand = str(getattr(row, "product_name_kor", "") or "").strip()
            raw_product = str(getattr(row, "product_name", "") or "").strip()
            seen_key = (atc4_code, raw_brand, raw_product)
            if seen_key in seen_rows:
                continue
            seen_rows.add(seen_key)
            ml_ids = alias_to_ml.get(atc4_code) or []
            brand_name = normalize_raw_brand_name(raw_brand) or normalize_raw_brand_name(raw_product)
            if brand_name is None:
                continue
            matched += 1
            for ml_id in ml_ids:
                _add_brand(result, ml_id=ml_id, atc4_code=atc4_code, source_view="IQVIA", brand_name=brand_name)
    return {"paths": [str(path) for path in paths], "partitions": len(paths), "rows": rows, "matched_rows": matched}


def load_raw_atc4_brands(
    ml_rows: list[dict[str, Any]],
    *,
    ubist_dir: Path,
    iqvia_nsa_dir: Path,
) -> tuple[dict[str, dict[str, RawAtc4Brand]], dict[str, Any]]:
    result: dict[str, dict[str, RawAtc4Brand]] = defaultdict(dict)
    stats = {
        "ubist": _load_ubist_brands(result, ml_rows, ubist_dir),
        "iqvia": _load_iqvia_brands(result, ml_rows, iqvia_nsa_dir),
    }
    stats["markets"] = {
        ml_id: {
            "raw_brand_keys": len(brands),
            "raw_atc4_codes": len({code for brand in brands.values() for code in brand.atc4_codes}),
        }
        for ml_id, brands in sorted(result.items())
    }
    return result, stats
