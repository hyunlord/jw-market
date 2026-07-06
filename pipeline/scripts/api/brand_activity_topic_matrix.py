from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Final, Sequence

from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en
from pipeline.scripts.api import db
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
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.types import quote_identifier


ALIAS_MAPPING_PATH: Final = Path("docs/design/brand_activity/alias/ALIAS_01_MAPPING.json")
KEYWORD_FILTER_COLUMNS: Final = {
    "visit_location": "visit_location",
    "specialty": "specialty",
    "interest": "interest",
    "prescription_evolution": "prescription_evolution",
}


class TopicRequestError(RuntimeError):
    """Raised when a topic matrix request cannot be parsed."""


def get_topic_brand_payload(payload: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
    """Return selected plus competitor brands with stored topic shares."""

    request = _parse_topic_request(payload)
    _validate_topic_filter_domains(request)
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
    topic_rows = _fetch_topic_rows()
    topic_index = _topic_brand_index(topic_rows)
    aliases = _alias_lookup()
    topic_scope = _topic_scope(brand_set=brand_set, topic_rows=topic_rows, aliases=aliases)
    is_sliced = _is_sliced_request(request)
    return {
        "scope": {
            "view": request["view"],
            "market_id": brand_set.market_id,
            "market_name": str(brand_set.market_row.get(brand_set.view.market_name_column) or brand_set.market_id),
            "selected_brand": request["selected_brand"],
            "applied_filter": brand_set.applied_filter,
            "applied_filters": brand_set.applied_filter,
            "resolved_market": _resolved_market_payload(request, brand_set),
            "visit_location": _display_filter_value(request["visit_location"]),
            "specialty": _display_filter_value(request["specialty"]),
            "interest": _display_filter_value(request["interest"]),
            "prescription_evolution": _display_filter_value(request["prescription_evolution"]),
            "period_start": request["period_start"],
            "period_end": request["period_end"],
            "top_n": request["top_n"],
            "sliced": is_sliced,
            "applied_topic_filters": _applied_topic_filters(request),
            "topic_set_version": topic_scope.get("topic_set_version"),
            "filter_effect": {
                "brand_set": "channel_axis_applied" if brand_set.channel_axis else "base",
                "payload": "row_topic_assignment_filtered" if is_sliced else "precomputed_scope_not_resliced",
            },
        },
        "brands": [
            (
                _sliced_topic_brand_item(
                    brand_set,
                    choice_key=choice.brand_key,
                    topic_scope=topic_scope,
                    topic_index=topic_index,
                    request=request,
                    aliases=aliases,
                    top_n=int(request["top_n"]),
                )
                if is_sliced and topic_scope
                else _topic_brand_item(brand_set, choice_key=choice.brand_key, topic_index=topic_index, aliases=aliases, top_n=int(request["top_n"]))
            )
            for choice in brand_set.choices
        ],
    }


def _parse_topic_request(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Parse the POST topic request into a normalized service dictionary."""
    view = _text(payload.get("view"))
    selected_brand = _text(payload.get("selected_brand"))
    filter_payload = _filter_payload(payload)
    market_id = _first_filter_value(filter_payload, "atc4") if view == "general" else ""
    if not view or not selected_brand or (view == "general" and not market_id):
        raise TopicRequestError("view, filters.atc4, and selected_brand are required")
    top_n = _integer(payload.get("top_n") or 5)
    return {
        "view": view,
        "market_id": market_id,
        "selected_brand": selected_brand,
        "filter": filter_payload,
        "visit_location": _filter_values(_payload_or_filter_value(payload, filter_payload, "visit_location")),
        "specialty": _filter_values(_payload_or_filter_value(payload, filter_payload, "specialty")),
        "interest": _filter_values(_payload_or_filter_value(payload, filter_payload, "interest")),
        "prescription_evolution": _filter_values(_payload_or_filter_value(payload, filter_payload, "prescription_evolution")),
        "period_start": _text(payload.get("period_start") or filter_payload.get("period_start")),
        "period_end": _text(payload.get("period_end") or filter_payload.get("period_end")),
        "top_n": max(1, min(top_n, 10)),
    }


def _topic_brand_index(topic_rows: Sequence[dict[str, JsonValue]] | None = None) -> dict[str, dict[str, JsonValue]]:
    """Build a normalized English brand name index from stored topic payloads."""
    index: dict[str, dict[str, JsonValue]] = {}
    for row in topic_rows if topic_rows is not None else _fetch_topic_rows():
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


def _sliced_topic_brand_item(
    brand_set: BrandSetResolution,
    *,
    choice_key: str,
    topic_scope: dict[str, JsonValue],
    topic_index: dict[str, dict[str, JsonValue]],
    request: dict[str, JsonValue],
    aliases: dict[str, str],
    top_n: int,
) -> dict[str, JsonValue]:
    """Project one brand from row-topic assignments under keyword filters."""
    meta = brand_set.brand_meta[choice_key]
    choice = next(choice for choice in brand_set.choices if choice.brand_key == choice_key)
    rows = _fetch_sliced_topic_rows(
        scope_id=_text(topic_scope.get("scope_id")),
        topic_set_version=_text(topic_scope.get("topic_set_version")),
        product_codes=_brand_product_codes(meta, aliases),
        visit_locations=_filter_tuple(request.get("visit_location")),
        specialties=_filter_tuple(request.get("specialty")),
        interests=_filter_tuple(request.get("interest")),
        prescription_evolutions=_filter_tuple(request.get("prescription_evolution")),
        period_start=_text(request.get("period_start")),
        period_end=_text(request.get("period_end")),
    )
    stored = _stored_brand_topics(meta, topic_index, aliases)
    axis_labels = _axis_topic_label_index(topic_scope)
    brand_labels = _brand_topic_label_index(stored)
    axis_topics: list[dict[str, JsonValue]] = []
    brand_topics: list[dict[str, JsonValue]] = []
    event_count = 0
    for row in rows:
        event_count = max(event_count, _integer(row.get("brand_total_rows")))
        topic_id = _text(row.get("topic_id"))
        labels = brand_labels if topic_id.startswith("B") else axis_labels
        topic = {
            "topic_id": topic_id,
            "label": _text(labels.get(topic_id, {}).get("label")),
            "share_pct": _numeric(row.get("share_pct")),
            "row_count": _integer(row.get("affected_row_count")),
        }
        if topic_id.startswith("B"):
            brand_specific = {
                **topic,
                "definition": _text(labels.get(topic_id, {}).get("definition")),
            }
            brand_topics.append(brand_specific)
        else:
            axis_topics.append(topic)
    axis_topics.sort(key=lambda topic: _number(topic.get("share_pct")), reverse=True)
    ranked_topics = [
        {
            "rank": index,
            "topic_id": _text(topic.get("topic_id")),
            "label": _text(topic.get("label")),
            "share_pct": _number(topic.get("share_pct")),
            "row_count": _integer(topic.get("row_count")),
        }
        for index, topic in enumerate(axis_topics[:top_n], start=1)
    ]
    brand_topics.sort(key=lambda topic: _number(topic.get("share_pct")), reverse=True)
    return {
        "brand_key": choice.brand_key,
        "brand_name": choice.brand_name,
        "is_jw": meta.is_jw,
        "is_selected": choice.is_selected,
        "sales_rank": choice.sales_rank,
        "event_count": event_count,
        "topic_shares": ranked_topics,
        "topics": ranked_topics,
        "etc_pct": max(0.0, 100.0 - sum(_number(topic.get("share_pct")) for topic in ranked_topics)),
        "brand_specific_topics": brand_topics,
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


def _is_sliced_request(request: dict[str, JsonValue]) -> bool:
    """Return whether keyword filters require row-topic aggregation."""
    return any(
        (
            bool(_filter_tuple(request.get("visit_location"))),
            bool(_filter_tuple(request.get("specialty"))),
            bool(_filter_tuple(request.get("interest"))),
            bool(_filter_tuple(request.get("prescription_evolution"))),
            bool(_text(request.get("period_start"))),
            bool(_text(request.get("period_end"))),
        )
    )


def _topic_scope(
    *,
    brand_set: BrandSetResolution,
    topic_rows: Sequence[dict[str, JsonValue]],
    aliases: dict[str, str],
) -> dict[str, JsonValue]:
    """Resolve the stored topic scope that backs one brand-set request."""
    direct_scope_id = f"atc4:{brand_set.market_id}"
    for row in topic_rows:
        if _text(row.get("scope_id")) == direct_scope_id:
            return _scope_catalog_row(row)
    selected_meta = brand_set.brand_meta.get(brand_set.selected_brand)
    if selected_meta is None:
        return {}
    selected_codes = set(_brand_product_codes(selected_meta, aliases))
    for row in topic_rows:
        payload = _json_object(row.get("payload"))
        for raw_brand in _json_list(payload.get("brands")):
            brand = _json_object(raw_brand)
            if normalize_iqvia_en(_text(brand.get("brand"))) in selected_codes:
                return _scope_catalog_row(row)
    return {}


def _scope_catalog_row(row: dict[str, JsonValue]) -> dict[str, JsonValue]:
    payload = _json_object(row.get("payload"))
    scope = _json_object(payload.get("scope"))
    return {
        "scope_id": _text(row.get("scope_id") or scope.get("scope_id")),
        "topic_set_version": _text(row.get("run_id") or payload.get("run_id") or payload.get("tag")),
        "payload": payload,
    }


def _axis_topic_label_index(topic_scope: dict[str, JsonValue]) -> dict[str, dict[str, JsonValue]]:
    """Return labels and definitions for shared market-axis topic ids."""
    payload = _json_object(topic_scope.get("payload"))
    axis = _json_object(payload.get("axis"))
    labels: dict[str, dict[str, JsonValue]] = {}
    for raw_topic in _json_list(axis.get("topics")):
        topic = _json_object(raw_topic)
        topic_id = _text(topic.get("topic_id"))
        if topic_id:
            labels[topic_id] = {
                "label": _text(topic.get("label")),
                "definition": _text(topic.get("definition")),
            }
    return labels


def _brand_topic_label_index(stored: dict[str, JsonValue] | None) -> dict[str, dict[str, JsonValue]]:
    """Return labels and definitions for one brand's B* topic ids."""
    labels: dict[str, dict[str, JsonValue]] = {}
    if stored is None:
        return labels
    for raw_topic in _json_list(stored.get("brand_specific_topics")):
        topic = _json_object(raw_topic)
        topic_id = _text(topic.get("topic_id"))
        if topic_id:
            labels[topic_id] = {
                "label": _text(topic.get("label")),
                "definition": _text(topic.get("definition")),
            }
    return labels


def _brand_product_codes(meta: BrandMeta, aliases: dict[str, str]) -> tuple[str, ...]:
    """Return normalized product codes and aliases used by topic row brands."""
    codes: list[str] = []
    for code in meta.product_codes:
        normalized = normalize_iqvia_en(code)
        if normalized:
            codes.append(normalized)
        alias = aliases.get(normalized)
        if alias:
            codes.append(alias)
    return tuple(dict.fromkeys(codes))


def _fetch_sliced_topic_rows(
    *,
    scope_id: str,
    topic_set_version: str,
    product_codes: Sequence[str],
    visit_locations: Sequence[str],
    specialties: Sequence[str],
    interests: Sequence[str],
    prescription_evolutions: Sequence[str],
    period_start: str,
    period_end: str,
) -> list[dict[str, JsonValue]]:
    """Aggregate row-topic assignments under keyword row filters."""
    if not scope_id or not topic_set_version or not product_codes:
        return []
    placeholders = ", ".join(["%s"] * len(product_codes))
    filters = [
        "topic_scope.scope_id = %s",
        f"k.product_name IN ({placeholders})",
    ]
    params: list[object] = [scope_id, *product_codes]
    _append_in_filter(filters, params, "k.visit_location", visit_locations)
    _append_in_filter(filters, params, "k.specialty", specialties)
    _append_in_filter(filters, params, "k.interest", interests)
    _append_in_filter(filters, params, "k.prescription_evolution", prescription_evolutions)
    if period_start:
        filters.append("k.period_ym >= %s")
        params.append(period_start)
    if period_end:
        filters.append("k.period_ym <= %s")
        params.append(period_end)
    where_clause = " AND ".join(filters)
    schema = quote_identifier(config.brand_activity_db_name)
    sql = f"""
        WITH scoped_rows AS (
            SELECT DISTINCT k.id AS row_id
            FROM {schema}.`km_keyword_event_stage` k
            JOIN {schema}.`mart_brand_activity_topics` topic_scope
              ON JSON_CONTAINS(topic_scope.atc4_values, JSON_QUOTE(k.therapeutic_class), '$')
            WHERE {where_clause}
        ),
        denominator AS (
            SELECT COUNT(DISTINCT row_id) AS brand_total_rows
            FROM scoped_rows
        )
        SELECT a.topic_id AS topic_id,
               COUNT(DISTINCT a.row_id) AS affected_row_count,
               denominator.brand_total_rows AS brand_total_rows,
               ROUND(COUNT(DISTINCT a.row_id) * 100.0 / NULLIF(denominator.brand_total_rows, 0), 2) AS share_pct
        FROM {schema}.`row_topic_assignment` a
        JOIN scoped_rows ON scoped_rows.row_id = a.row_id
        JOIN denominator
        WHERE a.scope_id = %s
          AND a.topic_set_version = %s
        GROUP BY a.topic_id, denominator.brand_total_rows
        HAVING denominator.brand_total_rows > 0
        ORDER BY share_pct DESC, topic_id
    """
    return db.fetch_all(sql, (*params, scope_id, topic_set_version))


def _append_in_filter(filters: list[str], params: list[object], column: str, values: Sequence[str]) -> None:
    """Append a parameterized IN predicate for one active keyword dimension."""
    if not values:
        return
    placeholders = ", ".join(["%s"] * len(values))
    filters.append(f"{column} IN ({placeholders})")
    params.extend(values)


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
            "row_count": _topic_row_count(share),
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
                "row_count": _topic_row_count(topic),
            }
        )
    return topics


def _topic_row_count(topic: dict[str, JsonValue]) -> int:
    """Return affected rows for one topic; topic totals are independent, not brand-event totals."""
    return _integer(topic.get("row_count") or topic.get("affected_row_count"))


def _filter_values(value: JsonValue) -> tuple[str, ...]:
    """Normalize scalar-or-list keyword filters into a deduplicated tuple."""
    if isinstance(value, list):
        values = [_text(item).strip() for item in value]
    else:
        values = [_text(value).strip()]
    return tuple(dict.fromkeys(item for item in values if item and item != "전체"))


def _payload_or_filter_value(payload: dict[str, JsonValue], filter_payload: dict[str, JsonValue], key: str) -> JsonValue:
    """Prefer top-level topic filters while accepting the shared `filters` envelope."""
    value = payload.get(key)
    return filter_payload.get(key) if value is None else value


def _filter_tuple(value: JsonValue) -> tuple[str, ...]:
    """Return an internal tuple stored in a parsed request."""
    if isinstance(value, tuple) and all(isinstance(item, str) for item in value):
        return value
    return _filter_values(value)


def _display_filter_value(value: JsonValue) -> str | list[str]:
    """Return legacy scalar display for one value and list display for multi-select."""
    values = _filter_tuple(value)
    if not values:
        return "전체"
    if len(values) == 1:
        return values[0]
    return list(values)


def _applied_topic_filters(request: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Echo active row-topic filters in list form for portal verification."""
    applied: dict[str, JsonValue] = {}
    for key in KEYWORD_FILTER_COLUMNS:
        values = _filter_tuple(request.get(key))
        if values:
            applied[key] = list(values)
    period_start = _text(request.get("period_start"))
    period_end = _text(request.get("period_end"))
    if period_start:
        applied["period_start"] = period_start
    if period_end:
        applied["period_end"] = period_end
    return applied


def _validate_topic_filter_domains(request: dict[str, JsonValue]) -> None:
    """Reject unknown row-topic filter values instead of silently returning empty slices."""
    for key, column in KEYWORD_FILTER_COLUMNS.items():
        values = _filter_tuple(request.get(key))
        if not values:
            continue
        allowed = _keyword_filter_domain(column)
        unknown = [value for value in values if value not in allowed]
        if unknown:
            joined = ", ".join(unknown)
            raise TopicRequestError(f"unsupported {key} filter value: {joined}")


def _keyword_filter_domain(column: str) -> frozenset[str]:
    """Return the live keyword domain for a row-topic filter column."""
    schema = quote_identifier(config.brand_activity_db_name)
    rows = db.fetch_all(
        f"""
        SELECT DISTINCT k.{column} AS value
        FROM {schema}.`km_keyword_event_stage` k
        JOIN {schema}.`row_topic_assignment` a ON a.row_id = k.id
        WHERE k.{column} IS NOT NULL
          AND k.{column} <> ''
        ORDER BY k.{column}
        """
    )
    return frozenset(_text(row.get("value")) for row in rows if _text(row.get("value")))


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
    return int(_numeric(value))


def _numeric(value: JsonValue) -> float:
    """Return DB or JSON numeric values as float."""
    if isinstance(value, int | float | Decimal):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0
