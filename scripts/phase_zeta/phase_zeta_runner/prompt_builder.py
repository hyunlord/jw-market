from __future__ import annotations

import json
from typing import Any

from .config import RunnerConfig


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _competitor_event_count(competitor_events: dict[str, Any]) -> int:
    return sum(
        len(comp.get("events", []) or [])
        for source_payload in (competitor_events.get("by_source") or {}).values()
        for comp in source_payload.get("competitors", []) or []
    )


def build_question_string(bundle: dict[str, Any], config: RunnerConfig | None = None) -> str:
    """Build the single `question` string consumed by GenOS workflow 217."""

    brand_context = bundle.get("brand_context", {}) or {}
    event_bundle = bundle.get("event_bundle", {}) or {}
    forecast = bundle.get("forecast_simulation", {}) or {}
    competitor_events = bundle.get("competitor_events", {}) or {}

    brand_name = bundle.get("bundle_meta", {}).get("brand") or brand_context.get("name")
    snapshot_at = bundle.get("bundle_meta", {}).get("snapshot_at", "")
    events_brand_centric = event_bundle.get("events_brand_centric", []) or []
    events_market_trend = event_bundle.get("events_market_trend", []) or []
    cross_match_events = event_bundle.get("cross_match_events", []) or []
    runner_config = config.config_version if config else "phase_zeta_runner_genos_v1"

    return f"""[분석 대상]
brand: {brand_name}
snapshot: {snapshot_at}
mkt_team: {brand_context.get("mkt_team")}
runner_config: {runner_config}

[brand 메타]
{_dump(brand_context)}

[시장 view 데이터 — 총 {len(bundle.get("market_views", []) or [])} view]
{_dump(bundle.get("market_views", []) or [])}

[brand 직접 events — {len(events_brand_centric)} 건]
{_dump(events_brand_centric)}

[시장 동향 events — {len(events_market_trend)} 건]
{_dump(events_market_trend)}

[cross_match events — {len(cross_match_events)} 건]
{_dump(cross_match_events)}

[경쟁사 events (source 별) — {_competitor_event_count(competitor_events)} 건]
{_dump(competitor_events)}

[forecast/simulation]
available: {bool(forecast.get("available", False))}
{_dump(forecast)}

위 데이터를 활용해서 phenomenon, cause, prediction, recommendation 4단 분석을 한 번에 JSON 으로 생성하세요.
"""
