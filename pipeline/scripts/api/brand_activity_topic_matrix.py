from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en
from pipeline.scripts.api.brand_activity_brand_resolver import (
    BrandSetInputError,
    BrandSetResolution,
    resolve_brand_set,
)
from pipeline.scripts.api.brand_activity_csd_shared import BrandMeta
from pipeline.scripts.api.brand_activity_topics import (
    JsonValue,
    _fetch_topic_rows,
    _json_list,
    _json_object,
    _number,
    _text,
)


ALIAS_MAPPING_PATH: Final = Path("docs/design/brand_activity/alias/ALIAS_01_MAPPING.json")


class TopicRequestError(RuntimeError):
    """Raised when a topic matrix request cannot be parsed."""


def get_topic_brand_payload(payload: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
    """Return selected plus competitor brands with stored topic shares."""

    request = _parse_topic_request(payload)
    try:
        brand_set = resolve_brand_set(
            view_name=request["view"],
            market_id=request["market_id"],
            selected_brand=request["selected_brand"],
            filter_payload=_json_object(request.get("filter")),
        )
    except BrandSetInputError as exc:
        raise TopicRequestError(str(exc)) from exc
    if brand_set is None:
        return None
    topic_index = _topic_brand_index()
    aliases = _alias_lookup()
    return {
        "scope": {
            "view": request["view"],
            "market_id": brand_set.market_id,
            "market_name": str(brand_set.market_row.get(brand_set.view.market_name_column) or brand_set.market_id),
            "selected_brand": request["selected_brand"],
            "applied_filter": brand_set.applied_filter,
            "applied_filters": brand_set.applied_filter,
            "resolved_market": _resolved_market_payload(request, brand_set),
            "visit_location": request["visit_location"],
            "specialty": request["specialty"],
            "top_n": request["top_n"],
            "sliced": False,
            "filter_effect": {
                "brand_set": "channel_axis_applied" if brand_set.channel_axis else "base",
                "payload": "precomputed_scope_not_resliced",
            },
        },
        "brands": [
            _topic_brand_item(brand_set, choice_key=choice.brand_key, topic_index=topic_index, aliases=aliases, top_n=int(request["top_n"]))
            for choice in brand_set.choices
        ],
    }


def _parse_topic_request(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Parse the POST topic request into a normalized service dictionary."""
    view = _text(payload.get("view"))
    market_id = _text(payload.get("market_id"))
    selected_brand = _text(payload.get("selected_brand"))
    filter_payload = _filter_payload(payload)
    if view == "general" and not market_id:
        market_id = _first_filter_value(filter_payload, "atc4")
    if not view or not selected_brand or (view == "general" and not market_id):
        raise TopicRequestError("view, market_id or filters.atc4, and selected_brand are required")
    top_n = _integer(payload.get("top_n") or 5)
    return {
        "view": view,
        "market_id": market_id,
        "selected_brand": selected_brand,
        "filter": filter_payload,
        "visit_location": _text(payload.get("visit_location")) or "전체",
        "specialty": _text(payload.get("specialty")) or "전체",
        "top_n": max(1, min(top_n, 10)),
    }


def _topic_brand_index() -> dict[str, dict[str, JsonValue]]:
    """Build a normalized English brand name index from stored topic payloads."""
    index: dict[str, dict[str, JsonValue]] = {}
    for row in _fetch_topic_rows():
        payload = _json_object(row.get("payload"))
        for raw_brand in _json_list(payload.get("brands")):
            brand = _json_object(raw_brand)
            key = normalize_iqvia_en(_text(brand.get("brand")))
            if key:
                index[key] = brand
    return index


def _topic_brand_item(
    brand_set: BrandSetResolution,
    *,
    choice_key: str,
    topic_index: dict[str, dict[str, JsonValue]],
    aliases: dict[str, str],
    top_n: int,
) -> dict[str, JsonValue]:
    """Project one resolved brand with topic shares matched by product code."""
    meta = brand_set.brand_meta[choice_key]
    choice = next(choice for choice in brand_set.choices if choice.brand_key == choice_key)
    stored = _stored_brand_topics(meta, topic_index, aliases)
    event_count = _integer(stored.get("row_count")) if stored is not None else 0
    topic_shares = _ranked_topics(stored, top_n=top_n)
    return {
        "brand_key": choice.brand_key,
        "brand_name": choice.brand_name,
        "is_jw": meta.is_jw,
        "is_selected": choice.is_selected,
        "sales_rank": choice.sales_rank,
        "event_count": event_count,
        "topic_shares": topic_shares,
        "topics": topic_shares,
        "etc_pct": max(0.0, 100.0 - sum(_number(topic.get("share_pct")) for topic in topic_shares)),
        "brand_specific_topics": _brand_specific_topics(stored),
    }


def _stored_brand_topics(
    meta: BrandMeta,
    topic_index: dict[str, dict[str, JsonValue]],
    aliases: dict[str, str],
) -> dict[str, JsonValue] | None:
    """Return stored topics for the first matching IQVIA product code."""
    for code in meta.product_codes:
        normalized = normalize_iqvia_en(code)
        for key in (normalized, aliases.get(normalized, "")):
            if key and key in topic_index:
                return topic_index[key]
    return None


def _ranked_topics(stored: dict[str, JsonValue] | None, *, top_n: int) -> list[dict[str, JsonValue]]:
    """Return top-N stored topic shares with API rank labels."""
    if stored is None:
        return []
    raw_shares = _json_list(stored.get("top5_topic_shares") or stored.get("topic_shares"))
    shares = [_json_object(share) for share in raw_shares]
    shares.sort(key=lambda share: _number(share.get("share_pct")), reverse=True)
    return [
        {
            "rank": index,
            "topic_id": _text(share.get("topic_id")),
            "label": _text(share.get("label")),
            "share_pct": _number(share.get("share_pct")),
        }
        for index, share in enumerate(shares[:top_n], start=1)
    ]


def _brand_specific_topics(stored: dict[str, JsonValue] | None) -> list[dict[str, JsonValue]]:
    if stored is None:
        return []
    raw_topics = _json_list(stored.get("brand_specific_topics"))
    topics: list[dict[str, JsonValue]] = []
    for raw_topic in raw_topics:
        topic = _json_object(raw_topic)
        topics.append(
            {
                "topic_id": _text(topic.get("topic_id")),
                "label": _text(topic.get("label")),
                "definition": _text(topic.get("definition")),
                "share_pct": _number(topic.get("share_pct")),
                "row_count": _integer(topic.get("row_count")),
            }
        )
    return topics


def _filter_payload(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    filters = payload.get("filters")
    legacy_filter = payload.get("filter")
    if isinstance(filters, dict) and filters:
        return filters
    return legacy_filter if isinstance(legacy_filter, dict) else {}


def _first_filter_value(filter_payload: dict[str, JsonValue], key: str) -> str:
    value = filter_payload.get(key)
    if isinstance(value, list):
        return _text(value[0]) if value else ""
    return _text(value)


def _resolved_market_payload(request: dict[str, JsonValue], brand_set: BrandSetResolution) -> dict[str, JsonValue]:
    market_id = brand_set.market_id
    return {
        "type": request["view"],
        "market_id": market_id,
        "market_label": str(brand_set.market_row.get(brand_set.view.market_name_column) or market_id),
        "source": "filters" if request["view"] == "general" else f"brand:{request['selected_brand']}",
    }


def _alias_lookup() -> dict[str, str]:
    """Return normalized variant-to-anchor aliases from the review mapping."""
    if not ALIAS_MAPPING_PATH.exists():
        return {}
    try:
        payload = json.loads(ALIAS_MAPPING_PATH.read_text())
    except json.JSONDecodeError:
        return {}
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return {}
    aliases: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        anchor = normalize_iqvia_en(str(record.get("iqvia_en") or ""))
        variants = record.get("variants")
        if anchor and isinstance(variants, list):
            for variant in variants:
                aliases[normalize_iqvia_en(str(variant))] = anchor
    return aliases


def _integer(value: JsonValue) -> int:
    """Return a numeric JSON scalar as int."""
    return int(value) if isinstance(value, int | float) else 0
