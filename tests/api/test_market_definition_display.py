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
    assert display.atc_count == 1
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


def test_ml_equals_cd_display_uses_parent_ml_definition(monkeypatch) -> None:
    # Given: a CD market is marked as exactly equal to its parent ML market.
    market_definition_display._cd_dim_by_id.cache_clear()
    market_definition_display._ml_atc_codes.cache_clear()
    market_definition_display._ml_market_names.cache_clear()
    monkeypatch.setattr(
        market_definition_display,
        "_cd_dim_by_id",
        lambda: {
            "cd_006": {
                "competitive_dynamics_id": "cd_006",
                "parent_market_landscape_id": "ml_006",
                "cd_definition_type": "ml_equals_cd_exact",
                "cd_filter_raw_json": (
                    '[{"value":"[C10C] 지질조절제 복합제제"},'
                    '{"value":"[C10A1] 스타틴류 (HMG-CoA 환원효소 억제제)"}]'
                ),
                "cd_definition_brand_class": "default",
            }
        },
    )
    monkeypatch.setattr(
        market_definition_display,
        "_ml_atc_codes",
        lambda: {"ml_006": ["C10A1", "C10C"]},
    )
    monkeypatch.setattr(
        market_definition_display,
        "_ml_market_names",
        lambda: {"ml_006": "리바로 리바로젯"},
    )

    # When: the portal-facing CD display definition is resolved.
    display = cd_display_for_id("cd_006")

    # Then: it mirrors the parent ML ATC definition, not the first raw CD label.
    assert display is not None
    assert display.label == "2 ATC 통합"
    assert display.full == "리바로 리바로젯 경쟁 시장 (C10A1, C10C)"
    assert display.atc_codes == ["C10A1", "C10C"]
    assert display.atc_count == 2


def test_apply_ml_equals_cd_definition_preserves_payload_atc_codes(monkeypatch) -> None:
    # Given: the cache payload already carries the parent ML ATC list.
    monkeypatch.setattr(
        market_definition_display,
        "_cd_dim_by_id",
        lambda: {
            "cd_006": {
                "competitive_dynamics_id": "cd_006",
                "parent_market_landscape_id": "ml_006",
                "cd_definition_type": "ml_equals_cd_exact",
                "cd_filter_raw_json": (
                    '[{"value":"[C10C] 지질조절제 복합제제"},'
                    '{"value":"[C10A1] 스타틴류 (HMG-CoA 환원효소 억제제)"}]'
                ),
                "cd_definition_brand_class": "default",
            }
        },
    )
    monkeypatch.setattr(market_definition_display, "_ml_atc_codes", lambda: {})
    monkeypatch.setattr(market_definition_display, "_ml_market_names", lambda: {})
    payload = {
        "market_meta": {
            "view_source_id": "cd_006",
            "market_name": "리바로 리바로젯",
            "market_definition_label": "old",
            "market_definition_full": "old",
            "atc_codes": ["C10A1", "C10C"],
            "atc_count": 2,
        }
    }

    # When: the CD market definition overlay is applied.
    apply_cd_market_definition(payload)

    # Then: raw MI label text is not exposed as an ATC code.
    assert payload["market_meta"]["market_definition_label"] == "2 ATC 통합"
    assert payload["market_meta"]["market_definition_full"] == "리바로 리바로젯 경쟁 시장 (C10A1, C10C)"
    assert payload["market_meta"]["atc_codes"] == ["C10A1", "C10C"]
    assert payload["market_meta"]["atc_count"] == 2


def test_apply_ml_equals_cd_definition_uses_parent_catalog_when_payload_has_placeholder(monkeypatch) -> None:
    # Given: a mart-direct CD payload has only a placeholder ATC value.
    monkeypatch.setattr(
        market_definition_display,
        "_cd_dim_by_id",
        lambda: {
            "cd_014": {
                "competitive_dynamics_id": "cd_014",
                "parent_market_landscape_id": "ml_011",
                "cd_definition_type": "ml_equals_cd_by_empty",
                "cd_filter_raw_json": '[{"value": null}]',
                "cd_definition_brand_class": "default_sheet_all",
            }
        },
    )
    monkeypatch.setattr(
        market_definition_display,
        "_ml_atc_codes",
        lambda: {"ml_011": ["L01G1", "L04B0", "L04D0", "M01C0"]},
    )
    monkeypatch.setattr(market_definition_display, "_ml_market_names", lambda: {"ml_011": "악템라"})
    payload = {
        "brand": "악템라",
        "source": "IQVIA",
        "measure": "sales",
        "market_meta": {
            "view_source_id": "cd_014",
            "market_name": "악템라",
            "market_definition_label": "old",
            "market_definition_full": "old",
            "atc_codes": ["default_sheet_all"],
            "atc_count": 1,
        },
    }

    # When: the CD market definition overlay is applied.
    apply_cd_market_definition(payload)

    # Then: placeholder text is replaced by the parent ML definition.
    assert payload["market_meta"]["market_definition_label"] == "4 ATC 통합"
    assert payload["market_meta"]["market_definition_full"] == "악템라 경쟁 시장 (L01G1, L04B0, L04D0, M01C0)"
    assert payload["market_meta"]["atc_codes"] == ["L01G1", "L04B0", "L04D0", "M01C0"]
    assert payload["market_meta"]["atc_count"] == 4


def test_apply_cd_market_definition_updates_only_cd_payloads() -> None:
    # Given: one competitive-dynamics payload and one market-landscape payload.
    cd_payload = {
        "market_meta": {
            "view_source_id": "cd_009",
            "market_definition_label": "old",
            "market_definition_full": "old",
            "atc_codes": ["C11A1"],
            "atc_count": 1,
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


def test_apply_cd_market_definition_preserves_actual_atc_codes_and_display_label(monkeypatch) -> None:
    # Given: the mart payload carries the exact CD ATC4 universe for Actemra.
    monkeypatch.setattr(
        market_definition_display,
        "_cd_dim_by_id",
        lambda: {
            "cd_014": {
                "competitive_dynamics_id": "cd_014",
                "parent_market_landscape_id": "ml_011",
                "cd_definition_type": "class_narrowing",
                "cd_filter_raw_json": '[{"value":"악템라"}]',
                "cd_definition_brand_class": "악템라",
            }
        },
    )
    payload = {
        "market_meta": {
            "view_source_id": "cd_014",
            "market_name": "악템라",
            "market_definition_label": "악템라",
            "market_definition_full": "악템라",
            "atc_codes": ["L01G1", "L04B0", "L04D0", "M01C0"],
            "atc_count": 4,
        }
    }

    # When: the CD display overlay is applied.
    apply_cd_market_definition(payload, preserve_existing_actual_atcs=True)

    # Then: label fields remain descriptive while atc_codes remain actual ATC4 values.
    assert payload["market_meta"]["market_definition_label"] == "악템라"
    assert payload["market_meta"]["market_definition_full"] == "악템라"
    assert payload["market_meta"]["atc_codes"] == ["L01G1", "L04B0", "L04D0", "M01C0"]
    assert payload["market_meta"]["atc_count"] == 4
