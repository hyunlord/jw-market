from __future__ import annotations

from dataclasses import dataclass

from pipeline.scripts.api.metadata import BRAND_METADATA
from pipeline.scripts.etl.build_cache_brands import _brand_payload


@dataclass(frozen=True)
class CatalogRows:
    rows: list[dict[str, object]]

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self.rows


def test_brand_payload_uses_ml_market_catalog_for_every_brand() -> None:
    market_ids = sorted({metadata.market_id for metadata in BRAND_METADATA})
    catalog = CatalogRows(
        [
            {
                "ml_id": market_id.replace("strategy_", "ml_"),
                "atc_codes_json": f'["CAT-{market_id[-3:]}"]',
            }
            for market_id in market_ids
        ]
    )

    payload = _brand_payload(catalog)

    assert len(payload) == 25
    assert [item["brand"] for item in payload] == [metadata.brand for metadata in BRAND_METADATA]
    assert [item["rank"] for item in payload] == [metadata.rank for metadata in BRAND_METADATA]
    assert all(
        item["atc_codes"] == [f'CAT-{str(item["market_id"])[-3:]}']
        for item in payload
    )


def test_brand_payload_fails_closed_when_catalog_market_is_missing() -> None:
    catalog = CatalogRows([{"ml_id": "ml_001", "atc_codes_json": '["A2B2"]'}])

    try:
        _brand_payload(catalog)
    except SystemExit as exc:
        assert "missing from ml market catalog" in str(exc)
    else:
        raise AssertionError("missing catalog markets must fail the cache build")
