from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline.scripts.api import services


def test_fallback_market_status_uses_exclusive_three_year_brand_slot(monkeypatch) -> None:
    meta = SimpleNamespace(
        brand="악템라",
        market_id="strategy_001",
        sources=("IQVIA",),
        is_dual_source=False,
        rank=1,
        is_jw=True,
        is_target=True,
        market_name="테스트 시장",
        market_name_short="테스트",
        market_label_kor="테스트 시장",
        mkt_team="테스트팀",
        atc_desc="",
    )
    resolved = SimpleNamespace(
        brand_id=1,
        period_yyyymm="2026-Q1",
        snapshot={
            "raw_value": 1.0,
            "market_share": 0.1,
            "cagr_5y": None,
            "cagr_3y": -0.039362,
            "market_cagr_5y": 0.0937,
        },
    )
    monkeypatch.setattr(services, "resolve_brand", lambda _brand: resolved)
    monkeypatch.setattr(services, "_catalog_atc_codes_for_ml", lambda _ml_id: [])
    monkeypatch.setattr(services, "_mat_growth_pct", lambda *_args: None)
    monkeypatch.setattr(services, "_ym_growth_pct", lambda *_args: None)
    monkeypatch.setattr(services, "_ms_change_yoy_pct", lambda *_args: None)
    monkeypatch.setattr(services, "_first_period_snapshot", lambda *_args: None)
    monkeypatch.setattr(services, "_market_context_snapshot", lambda *_args: {})

    card = services._build_market_status_card(meta, market_context_cache={})

    assert card["back_extended"]["brand_cagr_5y_pct"] is None
    assert card["back_extended"]["brand_cagr_3y_pct"] == pytest.approx(-3.9362)
    assert card["back_extended"]["market_cagr_5y_pct"] == pytest.approx(9.37)
