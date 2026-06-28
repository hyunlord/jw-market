#!/usr/bin/env python3
"""Shared read-only helpers for Phase B-1.0+ cutoff analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[4]
CORPUS_ROOT = Path("/Users/rexxa/Downloads/jw_gcp_crawling_20260522_011635/crawling")
STRATEGIC_BRAND_PATH = PROJECT_ROOT / "output/catalog/strategic_brand/strategic_brand.parquet"
CD_BRAND_PATH = PROJECT_ROOT / "output/catalog/cd_brand/cd_brand.parquet"
ML_MARKET_PATH = PROJECT_ROOT / "output/catalog/ml_market/ml_market.parquet"
STRATEGIC_PRODUCT_PATH = PROJECT_ROOT / "output/catalog/strategic_product/strategic_product.parquet"
AUDIT_DIR = PROJECT_ROOT / "docs/audit/phase_b1_0_cutoff_investigation"
SAMPLES_DIR = AUDIT_DIR / "samples"

RECENT_CUTOFF = date(2025, 5, 22)
ANALYSIS_END = date(2026, 5, 22)
UBIST_MONTHS = pd.period_range("2025-06", "2026-05", freq="M").astype(str).tolist()
IQVIA_QUARTERS = ["2025Q3", "2025Q4", "2026Q1", "2026Q2"]


@dataclass(frozen=True)
class BrandInfo:
    brand_canonical: str | None
    brand_id: str | None
    ml_id: str | None
    cd_id: str | None
    is_jw: bool
    class_name: str | None
    molecule: str | None


def processed_files(corpus: Path = CORPUS_ROOT) -> list[Path]:
    return sorted(corpus.glob("*_processed/*.json"))


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value)[:10]
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def score_to_tier(score: int) -> str:
    if score < 10:
        return "0-9"
    if score < 20:
        return "10-19"
    if score < 30:
        return "20-29"
    if score < 40:
        return "30-39"
    if score < 50:
        return "40-49"
    if score < 60:
        return "50-59"
    if score < 70:
        return "60-69"
    if score < 80:
        return "70-79"
    if score < 90:
        return "80-89"
    if score < 95:
        return "90-94"
    return "95-100"


def period_ubist(value: date | None) -> str | None:
    return value.strftime("%Y-%m") if value else None


def period_iqvia(value: date | None) -> str | None:
    if value is None:
        return None
    quarter = (value.month - 1) // 3 + 1
    return f"{value.year}Q{quarter}"


def clean(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def load_brand_catalog() -> tuple[pd.DataFrame, dict[str, BrandInfo]]:
    frames = [pd.read_parquet(STRATEGIC_BRAND_PATH)]
    if CD_BRAND_PATH.exists():
        frames.append(pd.read_parquet(CD_BRAND_PATH))
    df = pd.concat(frames, ignore_index=True)
    lookup: dict[str, BrandInfo] = {}
    for _, row in df.iterrows():
        info = BrandInfo(
            brand_canonical=clean(row.get("name")),
            brand_id=clean(row.get("brand_id")),
            ml_id=clean(row.get("ml_id")),
            cd_id=clean(row.get("cd_id")),
            is_jw=bool(row.get("is_jw")),
            class_name=clean(row.get("class")),
            molecule=clean(row.get("molecule")),
        )
        for column in ["name", "merge_name", "canonical_name", "general_brand_key"]:
            key = clean(row.get(column))
            if key and key not in lookup:
                lookup[key] = info
    return df, lookup


def load_market_catalog() -> pd.DataFrame:
    return pd.read_parquet(ML_MARKET_PATH)


def media_name(path: Path) -> str:
    return path.parent.name.replace("news_5years_", "").replace("_processed", "")


def load_matches() -> pd.DataFrame:
    _, brand_lookup = load_brand_catalog()
    rows: list[dict[str, Any]] = []
    for path in processed_files():
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            rows.append({"file_path": str(path), "load_error": str(exc)})
            continue
        published = parse_date(item.get("date"))
        article_id = path.stem
        matches = item.get("matches") or []
        for idx, match in enumerate(matches):
            if not isinstance(match, dict):
                continue
            brand = clean(match.get("drug"))
            try:
                score = int(match.get("score") or 0)
            except Exception:
                score = 0
            score = max(0, min(100, score))
            info = brand_lookup.get(brand or "")
            rows.append(
                {
                    "article_id": article_id,
                    "file_path": str(path),
                    "media": media_name(path),
                    "title": item.get("title"),
                    "date": published,
                    "year": str(published.year) if published else None,
                    "period_ubist": period_ubist(published),
                    "period_iqvia": period_iqvia(published),
                    "tag": item.get("tag") or "기타",
                    "summary": item.get("summary"),
                    "content": item.get("content"),
                    "search_keyword": item.get("search_keyword"),
                    "match_index": idx,
                    "brand": brand,
                    "score": score,
                    "score_tier": score_to_tier(score),
                    "reason": match.get("reason"),
                    "brand_canonical": info.brand_canonical if info else None,
                    "brand_id": info.brand_id if info else None,
                    "ml_id": info.ml_id if info else None,
                    "cd_id": info.cd_id if info else None,
                    "is_jw": bool(info.is_jw) if info else False,
                    "brand_group": "jw" if info and info.is_jw else ("in_catalog" if info else "unknown"),
                    "class_name": info.class_name if info else None,
                    "molecule": info.molecule if info else None,
                }
            )
    return pd.DataFrame(rows)


def ensure_dirs() -> None:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    (SAMPLES_DIR / "plots").mkdir(parents=True, exist_ok=True)


def write_df(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def describe_counts(values: pd.Series) -> dict[str, float | int]:
    if values.empty:
        return {"n_brands": 0, "mean": 0, "min": 0, "p25": 0, "median": 0, "p75": 0, "max": 0}
    return {
        "n_brands": int(values.shape[0]),
        "mean": round(float(values.mean()), 2),
        "min": int(values.min()),
        "p25": round(float(values.quantile(0.25)), 2),
        "median": round(float(values.median()), 2),
        "p75": round(float(values.quantile(0.75)), 2),
        "max": int(values.max()),
    }


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path)
