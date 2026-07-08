from __future__ import annotations

import re

from pipeline.scripts.api import market_definition_display
from pipeline.scripts.api.market_definition_display import apply_cd_market_definition, cd_display_for_id


INTERNAL_MARKET_DEFINITION_PATTERN = re.compile(
    r"Q-\d+|option B|clean\(|corrected explicit lookup|=="
)


def test_cd_display_falls_back_to_spec_rows_without_parquet(tmp_path, monkeypatch) -> None:
    # Given: the API image has no generated parquet catalog files.
    market_definition_display._cd_dim_by_id.cache_clear()
    monkeypatch.setattr(market_definition_display, "CD_DIM_PATHS", (tmp_path / "missing.parquet",))

    # When: a competitive-dynamics market definition is resolved.
    try:
        display = cd_display_for_id("cd_008")
    finally:
        market_definition_display._cd_dim_by_id.cache_clear()

    # Then: the helper uses the dependency-neutral spec fallback.
    assert display is not None
    assert display.label == "Statin/ARB/CCB"
    assert display.full == "[C11A1] 심혈관 질환 다중요법 목적의 복합제제 (단일 투약 형태) - Statin/ARB/CCB"
    assert display.atc_codes == ["Statin/ARB/CCB"]
    assert INTERNAL_MARKET_DEFINITION_PATTERN.search(display.label) is None
    assert INTERNAL_MARKET_DEFINITION_PATTERN.search(display.full) is None


def test_cd_display_uses_formal_raw_definition_before_filter_expression(tmp_path, monkeypatch) -> None:
    # Given: the API image has no generated parquet catalog files and relies on fallback rows.
    market_definition_display._cd_dim_by_id.cache_clear()
    monkeypatch.setattr(market_definition_display, "CD_DIM_PATHS", (tmp_path / "missing.parquet",))

    # When: a CD market whose fallback spec still has an internal filter expression is resolved.
    try:
        display = cd_display_for_id("cd_005")
    finally:
        market_definition_display._cd_dim_by_id.cache_clear()

    # Then: the portal-facing definition uses the formal MI text, not the diagnostic expression.
    assert display is not None
    assert display.label == "[C1D] 관상동맥 치료제"
    assert display.full == "[C1D] 관상동맥 치료제"
    assert INTERNAL_MARKET_DEFINITION_PATTERN.search(display.label) is None
    assert INTERNAL_MARKET_DEFINITION_PATTERN.search(display.full) is None


def test_cd_display_has_no_internal_market_definition_text_for_fallback_rows(tmp_path, monkeypatch) -> None:
    # Given: the API image has no generated parquet catalog files and relies on fallback rows.
    market_definition_display._cd_dim_by_id.cache_clear()
    monkeypatch.setattr(market_definition_display, "CD_DIM_PATHS", (tmp_path / "missing.parquet",))

    # When: every fallback CD market definition is resolved.
    try:
        displays = [cd_display_for_id(f"cd_{index:03d}") for index in range(1, 20)]
    finally:
        market_definition_display._cd_dim_by_id.cache_clear()

    # Then: no user-facing definition field contains QA/debug filter syntax.
    for display in displays:
        assert display is not None
        assert INTERNAL_MARKET_DEFINITION_PATTERN.search(display.label) is None
        assert INTERNAL_MARKET_DEFINITION_PATTERN.search(display.full) is None


def test_apply_cd_market_definition_updates_only_cd_payloads() -> None:
    # Given: one competitive-dynamics payload and one market-landscape payload.
    cd_payload = {
        "market_meta": {
            "view_source_id": "cd_009",
            "market_definition_label": "old",
            "market_definition_full": "old",
            "atc_codes": [],
            "atc_count": 0,
        }
    }
    ml_payload = {
        "market_meta": {
            "view_source_id": "ml_006",
            "market_definition_label": "1 ATC",
            "market_definition_full": "C10A1",
            "atc_codes": ["C10A1"],
            "atc_count": 1,
        }
    }

    # When: both payloads pass through the CD definition overlay.
    apply_cd_market_definition(cd_payload)
    apply_cd_market_definition(ml_payload)

    # Then: only the CD payload is narrowed to the CD display definition.
    assert cd_payload["market_meta"]["market_definition_label"] == "Statin/ARB"
    assert cd_payload["market_meta"]["market_definition_full"] == "[C11A1] 심혈관 질환 다중요법 목적의 복합제제 (단일 투약 형태) - Statin/ARB"
    assert cd_payload["market_meta"]["atc_codes"] == ["Statin/ARB"]
    assert cd_payload["market_meta"]["atc_count"] == 1
    assert ml_payload["market_meta"]["market_definition_label"] == "1 ATC"
    assert ml_payload["market_meta"]["market_definition_full"] == "C10A1"
