from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tier2_catalog import MetricBrandRow, brands_for_weekday, select_tier2_brands, stable_weekday_slice
from tier2_match_score import Tier2Brand


def test_selects_sales_threshold_and_recent_new_brands() -> None:
    rows = [
        MetricBrandRow(
            brand_key="high",
            brand_name="하이브랜드",
            source="ubist",
            atc4_code="A10C1",
            raw_value_history={"2026-01": 1_600_000_000, "2026-02": 1_500_000_000},
        ),
        MetricBrandRow(
            brand_key="new",
            brand_name="신규브랜드",
            source="ubist",
            atc4_code="B1A1",
            raw_value_history={"2025-09": 0, "2026-01": 10_000, "2026-02": 20_000},
        ),
        MetricBrandRow(
            brand_key="jw",
            brand_name="리바로",
            source="ubist",
            atc4_code="C10A1",
            raw_value_history={"2026-01": 9_000_000_000, "2026-02": 9_000_000_000},
        ),
    ]

    selected = select_tier2_brands(
        rows,
        sales_threshold_krw=3_000_000_000,
        recent_new_months=6,
        recent_new_min_sales_krw=0,
        jw_brand_names={"리바로"},
    )

    assert [brand.brand_name for brand in selected] == ["하이브랜드", "신규브랜드"]
    assert selected[0].reason == "sales_ge_3000000000"
    assert selected[1].reason == "first_nonzero_recent_6m"


def test_weekday_filter_keeps_mod7_default_and_allows_backfill_chunks() -> None:
    brands = [
        Tier2Brand(brand_name=f"브랜드{i}", brand_key=f"brand-{i}", source="ubist")
        for i in range(40)
    ]

    weekday = 3
    mod7 = brands_for_weekday(brands, weekday)
    mod28 = brands_for_weekday(brands, weekday, modulo=28)

    assert mod7 == [brand for brand in brands if stable_weekday_slice(brand.brand_key) == weekday]
    assert mod28 == [
        brand for brand in brands if stable_weekday_slice(brand.brand_key, modulo=28) == weekday
    ]
    assert all(stable_weekday_slice(brand.brand_key) == weekday for brand in mod7)
    assert all(stable_weekday_slice(brand.brand_key, modulo=28) == weekday for brand in mod28)
