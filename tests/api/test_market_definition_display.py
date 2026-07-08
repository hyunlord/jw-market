from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from pipeline.scripts.api import services
from pipeline.scripts.api import market_definition_display
from pipeline.scripts.api.market_definition_display import (
    cd_display_for_brand,
    cd_display_for_id,
)


def test_cd_display_for_split_pairs_uses_cd_definition_values() -> None:
    expected = {
        "cd_008": "Statin/ARB/CCB",
        "cd_009": "Statin/ARB",
        "cd_010": "G4C2",
        "cd_011": "G4C3",
        "cd_012": "L03A1",
        "cd_013": "L03A9",
    }

    actual = {cd_id: cd_display_for_id(cd_id).label for cd_id in expected}

    assert actual == expected
    assert cd_display_for_id("cd_008").atc_codes != cd_display_for_id("cd_009").atc_codes
    assert cd_display_for_id("cd_010").atc_codes != cd_display_for_id("cd_011").atc_codes
    assert cd_display_for_id("cd_012").atc_codes != cd_display_for_id("cd_013").atc_codes


def test_cd_display_for_brand_resolves_strategic_brand_cd_id() -> None:
    assert cd_display_for_brand("리바로하이", "ml_008").label == "Statin/ARB/CCB"
    assert cd_display_for_brand("리바로브이", "ml_008").label == "Statin/ARB"


def test_cd_display_falls_back_to_specs_without_parquet(tmp_path, monkeypatch) -> None:
    market_definition_display._cd_dim_by_id.cache_clear()
    monkeypatch.setattr(market_definition_display, "CD_DIM_PATHS", (tmp_path / "missing.parquet",))

    try:
        display = cd_display_for_id("cd_008")
    finally:
        market_definition_display._cd_dim_by_id.cache_clear()

    assert display.label == "Statin/ARB/CCB"
    assert display.full == "corrected explicit lookup clean(class_2) == 'Statin/ARB/CCB'"


def test_market_status_card_uses_cd_display_for_split_competitive_cards() -> None:
    meta = SimpleNamespace(
        brand="리바로하이",
        market_id="strategy_008",
        sources=["UBIST"],
        is_dual_source=False,
        market_name="리바로하이/리바로브이",
        market_name_short="리바로하이",
        market_label_kor="고혈압/복합",
        mkt_team="MKT 1팀",
        atc_desc="심혈관 다중요법 복합제 + 고혈압",
        rank=1,
        is_jw=True,
        is_target=True,
    )

    with (
        patch.object(services, "resolve_brand", side_effect=services.HTTPException(status_code=404)),
        patch.object(services, "latest_period", return_value="2026-04"),
    ):
        card = services._build_market_status_card(meta, market_context_cache={})

    assert card["back_extended"]["market_definition_label"] == "Statin/ARB/CCB"
    assert card["back_extended"]["market_definition_full"] == "corrected explicit lookup clean(class_2) == 'Statin/ARB/CCB'"
    assert card["back_extended"]["atc_count"] == 1
    assert card["atc_codes"] == ["Statin/ARB/CCB"]
