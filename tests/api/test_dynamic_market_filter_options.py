from __future__ import annotations

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market.filter_options import (
    DimensionOptionRow,
    build_filter_option_payload,
)


def test_build_filter_option_payload_groups_dimensions_and_atc_levels() -> None:
    payload = build_filter_option_payload(
        view="general",
        source="ubist",
        market_id="A10",
        dimensions=(
            DimensionOptionRow("seller", "엘지화학", "엘지화학", 3),
            DimensionOptionRow("seller", "JW중외제약", "jw중외제약", 1),
            DimensionOptionRow("route", "경구", "경구", 2),
        ),
        atc_rows=(
            {"atc4_code": "A10N1", "atc4_desc": "GLP-1"},
            {"atc4_code": "A10S0", "atc4_desc": "SGLT2"},
        ),
    )

    assert payload["view"] == "general"
    assert [item["dimension_type"] for item in payload["dimensions"]] == [
        "seller",
        "molecule_strength",
        "form",
        "route",
        "reimbursement",
    ]
    assert payload["dimensions"][0]["values"][0]["value"] == "JW중외제약"
    assert payload["atc"]["atc1"][0]["value"] == "A"
    assert payload["atc"]["atc3"][0]["value"] == "A10N"
    assert payload["atc"]["selectable_levels"] == ["atc3", "atc4"]
