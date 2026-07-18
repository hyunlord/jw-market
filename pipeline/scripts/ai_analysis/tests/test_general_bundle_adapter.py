from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from bundle_builder import general_bundle_adapter


class FakeProvider:
    def __init__(self, *, source, **_kwargs):
        self.source = source

    def get_kpi(self, brand_key):
        if self.source == "iqvia":
            return {"available": False}
        return {
            "available": True,
            "brand_key": brand_key,
            "brand_name": "일반브랜드",
            "source": "ubist",
            "measure": "sales",
            "atc4_codes": ["A01A1"],
            "unit_label": "KRW",
            "target_history": {"2026-05": {"raw_value": 100.0, "ms_pct": 10.0, "rank": 2}},
            "market_size_history": {"2026-05": 1000.0},
            "competitors_top5": [],
            "market_size_recent": 1000.0,
            "market_cagr_5y_pct": 3.0,
            "hhi_recent": 1200.0,
            "direct_competition_count": 5,
            "target_rank": 2,
            "brand_value_recent": 100.0,
            "brand_share_pct": 10.0,
            "hhi_series_5y": [],
        }


def test_general_bundle_uses_distinct_atc4_contract_and_no_forecast(monkeypatch):
    monkeypatch.setattr(
        general_bundle_adapter,
        "build_event_bundle",
        lambda *_args, **_kwargs: {
            "events_brand_centric": [],
            "events_market_trend": [],
            "cross_match_events": [],
        },
    )
    config = SimpleNamespace(config_version="test", builder_version="test")

    bundle = general_bundle_adapter.build_general_brand_bundle(
        "general_key",
        datetime(2026, 7, 12),
        config,
        object(),
        mart_db="jw_mart",
        bridge_db="jw_mart",
        provider_factory=FakeProvider,
    )

    assert bundle["bundle_meta"]["bundle_kind"] == "general_atc4"
    assert bundle["brand_context"]["market_scope"] == "ATC4"
    assert bundle["market_views"][0]["view_id"] == "GENERAL.UBIST.sales"
    assert bundle["market_views"][0]["view"] == "general_view"
    assert bundle["market_views"][0]["target_brand_metric"]["history"]["2026-05"]["raw_value"] == 100.0
    assert bundle["forecast_simulation"]["available"] is False
    assert bundle["competitor_events"] == {"by_source": {}, "by_view": {}}


def test_general_bundle_fails_closed_when_no_source_has_evidence(monkeypatch):
    class EmptyProvider(FakeProvider):
        def get_kpi(self, brand_key):
            return {"available": False, "brand_key": brand_key}

    config = SimpleNamespace(config_version="test", builder_version="test")

    try:
        general_bundle_adapter.build_general_brand_bundle(
            "missing",
            datetime(2026, 7, 12),
            config,
            object(),
            mart_db="jw_mart",
            bridge_db="jw_mart",
            provider_factory=EmptyProvider,
        )
    except ValueError as exc:
        assert "evidence unavailable" in str(exc)
    else:
        raise AssertionError("missing general evidence must fail closed")


def test_market_view_recomputes_latest_rank_from_raw_values():
    kpi = FakeProvider(source="ubist").get_kpi("target")
    kpi["target_history"]["2026-05"]["rank"] = 1
    kpi["target_rank"] = 1
    kpi["competitors_top5"] = [
        {
            "brand_key": "competitor-b",
            "brand_name": "경쟁B",
            "rank_in_market": 2,
            "history": {"2026-05": {"raw_value": 300.0, "ms_pct": 30.0, "rank": 2}},
        },
        {
            "brand_key": "competitor-a",
            "brand_name": "경쟁A",
            "rank_in_market": 3,
            "history": {"2026-05": {"raw_value": 200.0, "ms_pct": 20.0, "rank": 3}},
        },
    ]

    view = general_bundle_adapter._market_view(kpi, datetime(2026, 7, 12))

    assert view["target_brand_metric"]["history"]["2026-05"]["rank"] == 3
    assert view["target_brand_metric"]["kpi_extras"]["target_rank"] == 3
    assert [item["brand_key"] for item in view["competitors_top5"]] == [
        "competitor-b",
        "competitor-a",
    ]
    assert [item["rank_in_market"] for item in view["competitors_top5"]] == [1, 2]
    assert [item["history"]["2026-05"]["rank"] for item in view["competitors_top5"]] == [1, 2]
