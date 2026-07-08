from __future__ import annotations

from pipeline.scripts.api import market_definition_display
from pipeline.scripts.api.market_definition_display import apply_cd_market_definition, cd_display_for_id


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
    assert display.full == "corrected explicit lookup clean(class_2) == 'Statin/ARB/CCB'"
    assert display.atc_codes == ["Statin/ARB/CCB"]


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
    assert cd_payload["market_meta"]["market_definition_full"] == "corrected explicit lookup clean(class_2) == 'Statin/ARB'"
    assert cd_payload["market_meta"]["atc_codes"] == ["Statin/ARB"]
    assert cd_payload["market_meta"]["atc_count"] == 1
    assert ml_payload["market_meta"]["market_definition_label"] == "1 ATC"
    assert ml_payload["market_meta"]["market_definition_full"] == "C10A1"
