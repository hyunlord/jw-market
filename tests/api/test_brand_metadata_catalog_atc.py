from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pipeline.scripts.api.metadata import BRAND_METADATA, build_brand_metadata_payload


def test_brand_metadata_response_uses_catalog_atc_codes_without_changing_other_fields() -> None:
    metadata = next(item for item in BRAND_METADATA if item.brand == "리바로")

    payload = metadata.to_response(atc_codes=["C10A1", "C10C"])

    assert payload["atc_codes"] == ["C10A1", "C10C"]
    assert payload["brand"] == "리바로"
    assert payload["market_id"] == "strategy_006"
    assert payload["sources"] == list(metadata.sources)
    assert payload["rank"] == 1
    assert list(payload).index("atc_codes") == list(payload).index("sources") + 1


def test_brand_metadata_contains_no_static_atc_code_arguments() -> None:
    source_path = Path("pipeline/scripts/api/metadata/ml_market_meta.py")
    module = ast.parse(source_path.read_text(encoding="utf-8"))

    static_arguments = [
        keyword
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "BrandMetadata"
        for keyword in node.keywords
        if keyword.arg == "atc_codes"
    ]

    assert static_arguments == []


def test_brand_metadata_payload_fails_closed_for_empty_catalog_atc_codes() -> None:
    atc_codes_by_market = {
        metadata.market_id: [f"CAT-{metadata.market_id[-3:]}"]
        for metadata in BRAND_METADATA
    }
    atc_codes_by_market["strategy_006"] = []

    with pytest.raises(ValueError, match="empty ATC codes"):
        build_brand_metadata_payload(atc_codes_by_market)
