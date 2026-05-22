from __future__ import annotations

import pandas as pd

from pipeline.scripts.etl.layer3_compute_extended import compute_ei, compute_hhi, compute_momentum


def test_strategic_brand_catalog_has_korean_brand_names_for_all_16_markets() -> None:
    strategic_brand = pd.read_parquet("output/catalog/strategic_brand/strategic_brand.parquet")

    for ml_id, part in strategic_brand.groupby("ml_id"):
        non_korean = ~part["name"].astype(str).str.contains(r"[가-힣]", regex=True, na=False)
        molecule_like = part["name"].astype(str).str.fullmatch(r"[A-Z0-9][A-Z0-9 /().+-]*", na=False)
        korean = part["name"].astype(str).str.contains(r"[가-힣]", regex=True, na=False)
        assert not non_korean.any(), f"{ml_id}: non-Korean rows remain"
        assert not molecule_like.any(), f"{ml_id}: molecule-like rows remain"
        assert korean.any(), f"{ml_id}: no Korean brand names"


def test_cd_brand_catalog_is_korean_subset_of_strategic_brand() -> None:
    strategic_brand = pd.read_parquet("output/catalog/strategic_brand/strategic_brand.parquet")
    cd_brand = pd.read_parquet("output/catalog/cd_brand/cd_brand.parquet")

    expected_ids = set(strategic_brand.loc[strategic_brand["cd_id"].notna(), "brand_id"].astype(str))
    actual_ids = set(cd_brand["brand_id"].astype(str))
    assert actual_ids == expected_ids

    molecule_like = cd_brand["name"].astype(str).str.fullmatch(r"[A-Z0-9][A-Z0-9 /().+-]*", na=False)
    assert not molecule_like.any(), "cd_brand still contains molecule-like English brand rows"
    assert cd_brand[cd_brand["ml_id"] == "ml_003"]["name"].astype(str).str.contains(r"[가-힣]", regex=True).all()
    assert cd_brand[cd_brand["ml_id"] == "ml_003"]["brand_id"].nunique() >= 260


def test_hhi_formula_uses_percent_scale() -> None:
    shares_as_decimal = [0.1757, 0.0923, 0.0887]
    expected = sum((share * 100) ** 2 for share in shares_as_decimal)

    assert abs(compute_hhi(shares_as_decimal) - expected) < 0.0001


def test_evolution_index_formula_allows_small_nonzero_market_cagr() -> None:
    value, warning = compute_ei(0.0394, 0.0129)
    assert warning is None
    assert abs(value - 305.4263) < 0.0001

    small_value, small_warning = compute_ei(0.0016, 0.0008)
    assert small_warning is None
    assert small_value == 200

    zero_value, zero_warning = compute_ei(0.0016, 0.0)
    assert zero_value is None
    assert zero_warning == "ei_small_denominator"


def test_momentum_formula_recent_four_quarters() -> None:
    assert abs(compute_momentum([8.75, 8.78, 8.82, 8.87]) - 0.04) < 0.001
