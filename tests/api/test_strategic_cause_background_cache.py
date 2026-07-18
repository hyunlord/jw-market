from __future__ import annotations

from pathlib import Path
import sys
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market import strategic_cause
from pipeline.scripts.api.models.dynamic_market import DynamicMarketAnalysisLevelFilters
from pipeline.scripts.api.dynamic_market.types import PeriodRange


def test_strategic_cause_forwards_persistence_scheduler(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeCache:
        def get_or_build(self, request, builder, *, persistence_scheduler=None):
            captured["request"] = request
            captured["scheduler"] = persistence_scheduler
            return builder()

    def fake_build_strategic_payload(**_kwargs: object) -> dict[str, object]:
        return {"data": {"value": 1}}

    monkeypatch.setattr(strategic_cause, "build_strategic_payload", fake_build_strategic_payload)
    scheduler = lambda task: None

    payload = strategic_cause.get_strategic_payload(
        cache=FakeCache(),
        mart_db="mart",
        ml_id="ml_003",
        cd_market_id=None,
        focus_brand_key="가드메트",
        source="UBIST",
        measure="sales",
        analysis_level=DynamicMarketAnalysisLevelFilters(),
        persistence_scheduler=scheduler,
    )

    assert payload == {"data": {"value": 1}}
    assert captured["scheduler"] is scheduler


def test_strategic_cause_cache_identity_and_builder_include_period_range(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeCache:
        def get_or_build(self, request, builder, *, persistence_scheduler=None):
            captured["request"] = request
            return builder()

    def fake_build_strategic_payload(**kwargs: object) -> dict[str, object]:
        captured["builder"] = kwargs
        return {"data": {"value": 1}}

    monkeypatch.setattr(strategic_cause, "build_strategic_payload", fake_build_strategic_payload)
    period_range = PeriodRange("2025-01", "2025-12")

    strategic_cause.get_strategic_payload(
        cache=FakeCache(),
        mart_db="mart",
        ml_id="ml_003",
        cd_market_id=None,
        focus_brand_key="가드메트",
        source="UBIST",
        measure="sales",
        analysis_level=DynamicMarketAnalysisLevelFilters(),
        period_range=period_range,
    )

    assert captured["request"]["period_range"] == {"start": "2025-01", "end": "2025-12"}
    assert captured["builder"]["period_range"] == period_range


def test_unbounded_strategic_cache_identity_remains_backward_compatible() -> None:
    request = strategic_cause.strategic_cache_request(
        ml_id="ml_003",
        cd_market_id=None,
        focus_brand_key="가드메트",
        source="UBIST",
        measure="sales",
        analysis_level=DynamicMarketAnalysisLevelFilters(),
    )

    assert "period_range" not in request
