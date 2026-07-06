from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.scripts.etl.cache_deep_analysis_refresh_validate import (
    CacheRefreshValidationError,
    api_like_payload,
    payload_contract_errors,
    quote_ident,
    stable_json_hash,
)


def _payload(events: list[dict]) -> dict:
    return {
        "available_combos": [],
        "brand": "엔커버",
        "brand_name": "엔커버",
        "data": {
            "ai_analysis": {},
            "events": events,
            "forecast": {"by_combo": {}},
            "simulation": {"by_combo": {}},
        },
        "generated_at": "2026-07-06 12:00:00",
        "market_id": "strategy_001",
        "market_meta": {},
        "market_name": "테스트",
    }


def _event() -> dict:
    return {
        "body_full": "본문",
        "category": "policy",
        "category_label": "정책",
        "date": "2026-07-01",
        "id": "evt-1",
        "impact_score": 60,
        "on_chart": True,
        "on_list": True,
        "period_map": {"IQVIA": [], "UBIST": []},
        "related_coverage_count": 1,
        "related_sources": [],
        "related_titles": [],
        "related_urls": [],
        "source": "히트뉴스",
        "source_url": "https://example.invalid/a",
        "summary": "요약",
        "title": "제목",
        "url": "https://example.invalid/a",
    }


def test_payload_contract_accepts_stable_keys_and_event_schema() -> None:
    errors = payload_contract_errors(_payload([_event()]), "엔커버::strategy_001")

    assert errors == []


def test_payload_contract_rejects_missing_event_contract_key() -> None:
    event = _event()
    del event["on_chart"]

    errors = payload_contract_errors(_payload([event]), "엔커버::strategy_001")

    assert "엔커버::strategy_001:event[0] missing on_chart" in errors


def test_api_like_payload_excludes_serving_only_brand_strength() -> None:
    payload = _payload([])
    payload["data"]["brand_strength"] = {"available": True}

    result = api_like_payload(payload, "2026-07-06 12:00:00", {"ok": True})

    assert result["data"]["ai_analysis"] == {"ok": True}
    assert "brand_strength" not in result["data"]


def test_dynamic_simulation_keys_are_not_part_of_contract_hash() -> None:
    left = _payload([])
    right = _payload([])
    left["data"]["simulation"] = {"by_combo": {"UBIST": {"sales": {"by_brand": {"A": {"x": 1}}}}}}
    right["data"]["simulation"] = {"by_combo": {"UBIST": {"sales": {"by_brand": {"B": {"x": 2}}}}}}

    assert payload_contract_errors(left, "left") == []
    assert payload_contract_errors(right, "right") == []
    assert stable_json_hash(left["data"]["simulation"]) != stable_json_hash(right["data"]["simulation"])


def test_quote_ident_rejects_unsafe_table_name() -> None:
    with pytest.raises(CacheRefreshValidationError):
        quote_ident("cache_deep_analysis;DROP")
