from pipeline.etl.io.catalog.brand.strategic_product_records import (
    build_brand_identity_maps,
)


def test_legacy_brand_ids_receive_deterministic_noncolliding_identity() -> None:
    rows = [
        {"brand_id": "sb_001_00042", "ml_id": "ml_001"},
        {"brand_id": "sb_001_라베칸", "ml_id": "ml_001"},
        {"brand_id": "sb_canonical_가드렛", "ml_id": "ml_002"},
    ]

    source_rows, market_indexes = build_brand_identity_maps(rows)

    assert source_rows["sb_001_00042"] == 42
    assert source_rows["sb_001_라베칸"] > 42
    assert source_rows["sb_canonical_가드렛"] > 42
    assert len(set(source_rows.values())) == 3
    assert market_indexes == {
        "sb_001_00042": 1,
        "sb_001_라베칸": 1,
        "sb_canonical_가드렛": 2,
    }
