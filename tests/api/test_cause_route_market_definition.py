from __future__ import annotations

from pipeline.scripts.api.routes import cause as cause_route


def test_cause_route_adds_cd_market_definition(monkeypatch) -> None:
    # Given: cache_cause returns a competitive-dynamics payload with stale generic metadata.
    monkeypatch.setattr(
        cause_route,
        "_fetch_cause_rows",
        lambda *_args, **_kwargs: [{"market_id": "strategy_008", "response_json": "{}"}],
    )
    monkeypatch.setattr(
        cause_route,
        "choose_primary_market",
        lambda rows, **_kwargs: (rows[0], [{"market_id": "strategy_008", "is_primary": True}]),
    )
    monkeypatch.setattr(
        cause_route,
        "compose_cached_json",
        lambda *_args, **_kwargs: {
            "market_meta": {
                "view_source_id": "cd_008",
                "market_definition_label": "old",
                "market_definition_full": "old",
                "atc_codes": [],
                "atc_count": 0,
            }
        },
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
    assert payload["market_meta"]["market_definition_full"] == "corrected explicit lookup clean(class_2) == 'Statin/ARB/CCB'"
    assert payload["market_meta"]["atc_codes"] == ["Statin/ARB/CCB"]
    assert payload["market_meta"]["atc_count"] == 1
