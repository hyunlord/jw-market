"""Canonical strategic catalog grain checks."""

from __future__ import annotations

import pandas as pd


EXPECTED_CANONICAL_BRANDS = {
    "라베칸",
    "라베칸듀오",
    "제이클",
    "가드렛",
    "가드메트",
    "타발리스",
    "시그마트",
    "리바로",
    "리바로젯",
    "리바로페노",
    "리바로하이",
    "리바로브이",
    "트루패스",
    "피나스타",
    "제이다트",
    "뉴트로진",
    "모빌리아",
    "악템라",
    "페린젝트",
    "베노훼럼",
    "헴리브라",
    "위너프",
    "위너프A+",
    "엔커버",
    "플라주오피",
}


def _load_catalog(name: str) -> pd.DataFrame:
    return pd.read_parquet(f"output/catalog/{name}/{name}.parquet")


def test_strategic_brand_has_25_canonical_jw_brands() -> None:
    catalog = _load_catalog("strategic_brand")

    assert "is_jw" in catalog.columns
    canonical = catalog.loc[catalog["is_jw"] == True]  # noqa: E712 - pandas mask

    assert len(canonical) == 25
    assert set(canonical["name"].astype(str)) == EXPECTED_CANONICAL_BRANDS
    assert canonical["ml_id"].nunique() == 16


def test_cd_brand_has_same_25_canonical_jw_brands() -> None:
    catalog = _load_catalog("cd_brand")

    assert "is_jw" in catalog.columns
    canonical = catalog.loc[catalog["is_jw"] == True]  # noqa: E712 - pandas mask

    assert len(canonical) == 25
    assert set(canonical["name"].astype(str)) == EXPECTED_CANONICAL_BRANDS
    assert canonical["cd_id"].nunique() == 19


def test_aliases_are_canonicalized_but_join_keys_remain_available() -> None:
    catalog = _load_catalog("strategic_brand")
    canonical = catalog.loc[catalog["is_jw"] == True]  # noqa: E712 - pandas mask

    assert "리바로젯2" not in set(canonical["name"])
    assert "리바로젯4" not in set(canonical["name"])
    assert "리바로페노2" not in set(canonical["name"])
    assert "위너프에이플러스" not in set(canonical["name"])

    winnerf_aplus = canonical.loc[canonical["name"] == "위너프A+"].iloc[0]
    assert winnerf_aplus["general_brand_key"] == "위너프에이플러스"
