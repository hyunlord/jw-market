from __future__ import annotations

from pipeline.scripts.api.routes import cause as cause_route


def test_cause_returns_explicit_empty_state_for_unmapped_mart_brand(monkeypatch) -> None:
    monkeypatch.setattr(cause_route, "_fetch_cause_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cause_route, "_brand_exists", lambda _brand: True)

    payload = cause_route.cause(
        "비JW브랜드",
        view="market_landscape",
        source="UBIST",
        measure="sales",
        market_id=None,
    )

    assert payload["brand"] == "비JW브랜드"
    assert payload["data"] is None
    assert payload["reason"] == "brand_not_in_source"
    assert payload["markets"] == []


def test_cause_route_adds_cd_market_definition(monkeypatch) -> None:
    # Given: mart-direct assembly returns a competitive-dynamics payload with generic metadata.
    monkeypatch.setattr(
        cause_route,
        "_fetch_cause_rows",
        lambda *_args, **_kwargs: [
            {
                "market_id": "strategy_008",
                "response_json": {
                    "market_meta": {
                        "view_source_id": "cd_008",
                        "market_definition_label": "old",
                        "market_definition_full": "old",
                        "atc_codes": [],
                        "atc_count": 0,
                    }
                },
            }
        ],
    )
    monkeypatch.setattr(
        cause_route,
        "choose_primary_market",
        lambda rows, **_kwargs: (rows[0], [{"market_id": "strategy_008", "is_primary": True}]),
    )
    # When: the portal-facing cause route serves the payload.
    payload = cause_route.cause(
        "리바로하이",
        view="competitive_dynamics",
        source="UBIST",
        measure="sales",
        market_id=None,
    )

    # Then: the response includes the CD-narrowed market definition fields.
    assert payload["market_meta"]["market_definition_label"] == "Statin/ARB/CCB"
    assert payload["market_meta"]["market_definition_full"] == (
        "[C11A1] 심혈관 질환 다중요법 목적의 복합제제 (단일 투약 형태) - Statin/ARB/CCB"
    )
    assert payload["market_meta"]["atc_codes"] == ["Statin/ARB/CCB"]
    assert payload["market_meta"]["atc_count"] == 1
