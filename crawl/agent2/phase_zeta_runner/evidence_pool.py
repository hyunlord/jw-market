from __future__ import annotations

from decimal import Decimal
from typing import Any


STAGES = ("phenomenon", "cause", "prediction", "recommendation")
MIN_EVIDENCE_POOL_ITEMS = 8
TARGET_EVIDENCE_POOL_ITEMS = 12


def source_evidence_count(parsed_output: dict[str, Any]) -> int:
    """Count evidence items emitted by the model before deterministic supplementing."""

    return len(_existing_top_level_evidence(parsed_output)) + sum(
        len(_stage_evidence(parsed_output, stage)) for stage in STAGES
    )


def build_evidence_pool(
    parsed_output: dict[str, Any],
    bundle: dict[str, Any],
    *,
    min_items: int = TARGET_EVIDENCE_POOL_ITEMS,
) -> list[dict[str, Any]]:
    """Build the production evidence_pool from stage evidence plus bundle-backed facts.

    The LLM may choose which facts to cite, but every supplemental item here is copied
    from the bundle that fed the narrative. No new evidence is invented.
    """

    pool: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    for item in _existing_top_level_evidence(parsed_output):
        _append_unique(pool, seen, item)
    for stage in STAGES:
        for item in _stage_evidence(parsed_output, stage):
            normalized = _normalize_evidence(item, stage=stage)
            _append_unique(pool, seen, normalized)

    for item in _bundle_event_evidence(bundle):
        if len(pool) >= min_items:
            break
        _append_unique(pool, seen, item)

    for item in _bundle_metric_evidence(bundle):
        if len(pool) >= min_items:
            break
        _append_unique(pool, seen, item)

    return pool


def _append_unique(
    pool: list[dict[str, Any]],
    seen: set[tuple[str, str, str, str]],
    item: dict[str, Any],
) -> None:
    normalized = _normalize_evidence(item)
    if not _is_complete(normalized):
        return
    key = (
        str(normalized.get("stage") or ""),
        str(normalized.get("title") or ""),
        str(normalized.get("source") or ""),
        str(normalized.get("basis") or ""),
    )
    if key in seen:
        return
    seen.add(key)
    pool.append(normalized)


def _normalize_evidence(item: dict[str, Any], *, stage: str | None = None) -> dict[str, Any]:
    title = str(item.get("title") or item.get("label") or "").strip()
    source = str(item.get("source") or item.get("view_label") or "").strip()
    basis = str(item.get("basis") or item.get("summary") or item.get("description") or "").strip()
    normalized: dict[str, Any] = {
        "stage": str(stage or item.get("stage") or "common"),
        "title": title,
    }
    if source:
        normalized["source"] = source
    if basis:
        normalized["basis"] = basis
    published_date = item.get("published_date") or item.get("date")
    if published_date:
        normalized["published_date"] = str(published_date)
    return normalized


def _is_complete(item: dict[str, Any]) -> bool:
    return bool(str(item.get("title") or "").strip()) and bool(
        str(item.get("source") or "").strip() or str(item.get("basis") or "").strip()
    )


def _existing_top_level_evidence(parsed_output: dict[str, Any]) -> list[dict[str, Any]]:
    raw = parsed_output.get("evidence_pool") or []
    return [item for item in raw if isinstance(item, dict)]


def _stage_evidence(parsed_output: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    stage_payload = parsed_output.get(stage) or {}
    raw = stage_payload.get("evidence") or []
    return [item for item in raw if isinstance(item, dict)]


def _bundle_event_evidence(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    event_bundle = bundle.get("event_bundle") or {}
    for key in ("events_brand_centric", "events_market_trend", "cross_match_events"):
        events.extend(item for item in event_bundle.get(key, []) or [] if isinstance(item, dict))

    competitor_events = bundle.get("competitor_events") or {}
    for group_key in ("by_source", "by_view"):
        for payload in (competitor_events.get(group_key) or {}).values():
            if not isinstance(payload, dict):
                continue
            for competitor in payload.get("competitors", []) or []:
                if isinstance(competitor, dict):
                    events.extend(item for item in competitor.get("events", []) or [] if isinstance(item, dict))

    out: list[dict[str, Any]] = []
    for event in events:
        title = str(event.get("title") or "").strip()
        if not title:
            continue
        source = str(event.get("source") or "뉴스").strip()
        basis = str(event.get("summary") or event.get("description") or "").strip()
        item: dict[str, Any] = {"stage": "cause", "title": title, "source": source}
        if basis:
            item["basis"] = basis
        if event.get("published_date"):
            item["published_date"] = str(event["published_date"])
        out.append(item)
    return out


def _bundle_metric_evidence(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for view in bundle.get("market_views", []) or []:
        if not isinstance(view, dict):
            continue
        label = _view_label(view)
        metric = view.get("target_brand_metric") or {}
        history = metric.get("history") or {}
        if isinstance(history, dict) and history:
            period = sorted(history)[-1]
            point = history.get(period) or {}
            for field in ("raw_value", "ms_pct", "rank", "yoy_pct", "qoq_pct"):
                if field in point and point.get(field) is not None:
                    out.append(
                        {
                            "stage": "phenomenon",
                            "title": f"{label} {period} {field}",
                            "basis": f"{_format_number(point.get(field))}({label}·{period})",
                        }
                    )

    forecast = bundle.get("forecast_simulation") or {}
    for view_key, by_horizon in (forecast.get("by_view") or {}).items():
        if not isinstance(by_horizon, dict):
            continue
        for horizon in ("horizon_1y", "horizon_3y", "horizon_5y"):
            point = by_horizon.get(horizon) or {}
            if point.get("base") is None:
                continue
            period = point.get("period") or horizon
            out.append(
                {
                    "stage": "prediction",
                    "title": f"{view_key} {period} forecast",
                    "basis": f"{_format_number(point.get('base'))}({view_key}·{period})",
                }
            )
    return out


def _view_label(view: dict[str, Any]) -> str:
    view_id = str(view.get("view_id") or "").strip()
    if view_id:
        return view_id
    view_name = str(view.get("view") or "").strip()
    source = str(view.get("source") or "").strip()
    if view_name and source:
        return f"{view_name}.{source}"
    return "market_view"


def _format_number(value: Any) -> str:
    if isinstance(value, Decimal):
        value = int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)
