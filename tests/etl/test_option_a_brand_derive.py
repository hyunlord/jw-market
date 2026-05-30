"""Option A: derive UBIST general brand names from product base names."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline" / "scripts" / "etl"))

from pipeline.scripts.etl.brand_key_normalize import (
    best_name,
    extract_brand_base_name,
    normalize_brand_name,
)


def test_extract_removes_seller_prefix_from_product_name():
    """Product base extraction yields catalog-matchable brand keys."""
    assert normalize_brand_name(extract_brand_base_name("리피토정10mg")) == normalize_brand_name("리피토")
    assert normalize_brand_name(extract_brand_base_name("아토젯정10/10mg")) == normalize_brand_name("아토젯")


def test_product_derived_name_wins_over_polluted_brand_field():
    """Polluted UBIST brand field should not outrank product-derived base name."""
    result = best_name(
        extract_brand_base_name("리피토정10mg"),
        "비아트리스 리피토",
        "code123",
    )
    assert normalize_brand_name(result) == normalize_brand_name("리피토")


def test_fallback_when_product_base_is_empty():
    """General mart keeps a fallback when a product name cannot provide a base."""
    result = best_name(
        extract_brand_base_name(""),
        "어떤브랜드",
        "code",
    )
    assert result != ""
