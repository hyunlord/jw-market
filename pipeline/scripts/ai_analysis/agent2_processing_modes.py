from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from bundle_builder.agent2_density_router import ProcessingMode
from bundle_builder.hash_util import compute_bundle_hash

PROCESSING_MODE_FULL = "full"
PROCESSING_MODE_COMPACT = "compact"
PROCESSING_MODE_RECAP = "recap"


@dataclass(frozen=True, slots=True)
class FormatterModePolicy:
    name: str
    min_bullets: int
    min_body_sentences: int


@dataclass(frozen=True, slots=True)
class EventTrimLimits:
    brand_centric: int
    market_trend: int
    cross_match: int


@dataclass(frozen=True, slots=True)
class CompetitorTrimLimits:
    competitors: int
    events: int


FORMATTER_MODE_POLICIES = {
    PROCESSING_MODE_FULL: FormatterModePolicy(PROCESSING_MODE_FULL, min_bullets=4, min_body_sentences=4),
    PROCESSING_MODE_COMPACT: FormatterModePolicy(PROCESSING_MODE_COMPACT, min_bullets=2, min_body_sentences=2),
    PROCESSING_MODE_RECAP: FormatterModePolicy(PROCESSING_MODE_RECAP, min_bullets=1, min_body_sentences=1),
}


def normalize_processing_mode(mode: str | ProcessingMode) -> str:
    match mode:
        case ProcessingMode.LLM_FULL:
            return PROCESSING_MODE_FULL
        case ProcessingMode.LLM_COMPACT:
            return PROCESSING_MODE_COMPACT
        case ProcessingMode.LLM_RECAP:
            return PROCESSING_MODE_RECAP
        case str() as raw:
            if raw in FORMATTER_MODE_POLICIES:
                return raw
            raise ValueError(f"Unsupported Agent2 processing mode: {raw}")
        case unreachable:
            raise TypeError(f"Unsupported Agent2 processing mode type: {type(unreachable).__name__}")


def formatter_policy_for_mode(mode: str | ProcessingMode) -> FormatterModePolicy:
    return FORMATTER_MODE_POLICIES[normalize_processing_mode(mode)]


def trim_bundle_for_mode(bundle: dict[str, Any], mode: str | ProcessingMode) -> dict[str, Any]:
    """Return a deterministic mode-trimmed bundle after the full bundle is built."""

    mode_name = normalize_processing_mode(mode)
    if mode_name == PROCESSING_MODE_FULL:
        return bundle

    trimmed = copy.deepcopy(bundle)
    match mode_name:
        case "compact":
            _trim_event_bundle(trimmed, EventTrimLimits(brand_centric=3, market_trend=2, cross_match=1))
            _trim_competitor_events(trimmed, CompetitorTrimLimits(competitors=1, events=1))
            _trim_forecast_simulation(trimmed)
        case "recap":
            _trim_event_bundle(trimmed, EventTrimLimits(brand_centric=1, market_trend=1, cross_match=_recap_cross_match_count(trimmed)))
            _trim_competitor_events(trimmed, CompetitorTrimLimits(competitors=0, events=0))
            _trim_forecast_simulation(trimmed)
        case unreachable:
            raise ValueError(f"Unsupported Agent2 processing mode: {unreachable}")

    trimmed.setdefault("bundle_meta", {})["processing_mode"] = mode_name
    trimmed["bundle_meta"]["bundle_hash"] = None
    trimmed["bundle_meta"]["bundle_hash"] = compute_bundle_hash(trimmed)
    return trimmed


def _trim_event_bundle(bundle: dict[str, Any], limits: EventTrimLimits) -> None:
    event_bundle = bundle.setdefault("event_bundle", {})
    event_bundle["events_brand_centric"] = list(event_bundle.get("events_brand_centric", []) or [])[: limits.brand_centric]
    event_bundle["events_market_trend"] = list(event_bundle.get("events_market_trend", []) or [])[: limits.market_trend]
    event_bundle["cross_match_events"] = list(event_bundle.get("cross_match_events", []) or [])[: limits.cross_match]


def _recap_cross_match_count(bundle: dict[str, Any]) -> int:
    events = bundle.get("event_bundle", {}) or {}
    has_direct = bool(events.get("events_brand_centric") or events.get("events_market_trend"))
    return 0 if has_direct else 1


def _trim_competitor_events(bundle: dict[str, Any], limits: CompetitorTrimLimits) -> None:
    competitor_events = bundle.setdefault("competitor_events", {})
    for group_key in ("by_view", "by_source"):
        groups = competitor_events.get(group_key)
        if not isinstance(groups, dict):
            continue
        for payload in groups.values():
            if not isinstance(payload, dict):
                continue
            competitors = []
            for competitor in list(payload.get("competitors", []) or [])[: limits.competitors]:
                if isinstance(competitor, dict):
                    copied = dict(competitor)
                    copied["events"] = list(copied.get("events", []) or [])[: limits.events]
                    competitors.append(copied)
            payload["competitors"] = competitors


def _trim_forecast_simulation(bundle: dict[str, Any]) -> None:
    forecast = bundle.get("forecast_simulation")
    if not isinstance(forecast, dict):
        return
    by_view = forecast.get("by_view")
    if isinstance(by_view, dict) and by_view:
        first_key = next(iter(by_view))
        forecast["by_view"] = {first_key: by_view[first_key]}
