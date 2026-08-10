from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Final, Sequence

import pymysql

from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en
from pipeline.scripts.api import db
from pipeline.scripts.api.brand_activity_brand_resolver import (
    BrandSetInputError,
    BrandSetResolution,
    resolve_brand_set,
)
from pipeline.scripts.api.brand_activity_csd_presence import iqvia_product_codes_by_brand
from pipeline.scripts.api.brand_activity_csd_shared import BrandMeta, canonical_brand_activity_source
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
from pipeline.scripts.api.market_filter_atc_options import canonical_atc4_values


ALIAS_MAPPING_PATH: Final = Path("docs/design/brand_activity/alias/ALIAS_01_MAPPING.json")
KEYWORD_FILTER_COLUMNS: Final = {
    "visit_location": "visit_location",
    "specialty": "specialty",
    "interest": "interest",
    "prescription_evolution": "prescription_evolution",
}
LOGGER = logging.getLogger(__name__)


class TopicRequestError(RuntimeError):
    """Raised when a topic matrix request cannot be parsed."""


def get_topic_period_bounds() -> dict[str, str]:
    """Return the available monthly range from the indexed keyword source."""
    schema = quote_identifier(config.brand_activity_db_name)
    row = db.fetch_one(
        f"""
        SELECT MIN(period_ym) AS available_start,
               MAX(period_ym) AS available_end
        FROM {schema}.`km_keyword_event_stage`
        """
    ) or {}
    return {
        "available_start": _text(row.get("available_start")),
        "available_end": _text(row.get("available_end")),
    }


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
            source=_text(request.get("source")),
        )
    except BrandSetInputError as exc:
        raise TopicRequestError(str(exc)) from exc
    if brand_set is None:
        return None
    topic_query_failed = False
    try:
        topic_rows = _fetch_topic_rows()
    except pymysql.MySQLError:
        LOGGER.exception("brand activity topic scope query failed")
        topic_rows = []
        topic_query_failed = True
    aliases = _alias_lookup()
    topic_scope = _topic_scope(brand_set=brand_set, topic_rows=topic_rows)
    topic_index = _topic_brand_index([topic_scope]) if topic_scope else {}
    is_sliced = _is_sliced_request(request)
    product_codes_by_brand = (
        _topic_product_codes_by_brand(brand_set=brand_set, topic_scope=topic_scope, aliases=aliases)
        if topic_scope
        else {}
    )
    company_names = _company_names_by_brand(brand_set, aliases)
    payload_source = "row_topic_assignment_filtered" if is_sliced else "mart_brand_activity_topics_unfiltered"
    result: dict[str, JsonValue] = {
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
                "payload": payload_source,
                "period": "applied_to_row_filter" if is_sliced else "not_applied_to_unfiltered_payload",
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
                    product_codes=product_codes_by_brand.get(choice.brand_key, ()),
                    top_n=int(request["top_n"]),
                    company_name=company_names.get(choice.brand_key),
                    query_failed=topic_query_failed,
                )
                if topic_scope and is_sliced
                else _stored_topic_brand_item(
                    brand_set,
                    choice_key=choice.brand_key,
                    topic_index=topic_index,
                    aliases=aliases,
                    product_codes=product_codes_by_brand.get(choice.brand_key, ()),
                    top_n=int(request["top_n"]),
                    company_name=company_names.get(choice.brand_key),
                )
                if topic_scope
                else _empty_topic_brand_item(
                    brand_set,
                    choice_key=choice.brand_key,
                    company_name=company_names.get(choice.brand_key),
                    query_failed=topic_query_failed,
                )
            )
            for choice in brand_set.choices
        ],
    }
    if not topic_scope:
        reason = _topic_scope_failure_reason(brand_set=brand_set, topic_rows=topic_rows)
        result["reason"] = reason
        LOGGER.warning(
            "brand activity topic scope unavailable: reason=%s view=%s market_id=%s",
            reason,
            brand_set.view_name,
            brand_set.market_id,
        )
    return result


def _stored_topic_brand_item(
    brand_set: BrandSetResolution,
    *,
    choice_key: str,
    topic_index: dict[str, dict[str, JsonValue]],
    aliases: dict[str, str],
    product_codes: Sequence[str],
    top_n: int,
    company_name: str | None = None,
) -> dict[str, JsonValue]:
    """Project one unfiltered brand directly from the stored mart payload."""
    meta = brand_set.brand_meta[choice_key]
    choice = next(choice for choice in brand_set.choices if choice.brand_key == choice_key)
    stored = _stored_brand_topics(product_codes, topic_index, aliases)
    raw_topics = _json_list(
        stored.get("topic_shares") or stored.get("top5_topic_shares")
        if stored
        else []
    )
    topics = [_json_object(value) for value in raw_topics]
    topics.sort(key=lambda topic: _numeric(topic.get("share_pct")), reverse=True)
    ranked_topics = [
        {
            "rank": rank,
            "topic_id": _text(topic.get("topic_id")),
            "label": _text(topic.get("label")),
            "share_pct": _numeric(topic.get("share_pct")),
            "row_count": _integer(topic.get("row_count") or topic.get("affected_row_count")),
        }
        for rank, topic in enumerate(topics[:top_n], start=1)
        if _text(topic.get("topic_id"))
    ]
    brand_topics = [
        {
            "topic_id": _text(topic.get("topic_id")),
            "label": _text(topic.get("label")),
            "share_pct": _numeric(topic.get("share_pct")),
            "row_count": _integer(topic.get("row_count") or topic.get("affected_row_count")),
            "definition": _text(topic.get("definition")),
        }
        for topic in (
            _json_object(value)
            for value in _json_list(stored.get("brand_specific_topics") if stored else [])
        )
        if _text(topic.get("topic_id"))
    ]
    brand_topics.sort(key=lambda topic: _numeric(topic.get("share_pct")), reverse=True)
    event_count = _integer(stored.get("row_count") or stored.get("source_row_count")) if stored else 0
    return {
        "brand_key": choice.brand_key,
        "brand_name": choice.brand_name,
        "company_name": company_name,
        "is_jw": meta.is_jw,
        "is_selected": choice.is_selected,
        "sales_rank": choice.sales_rank,
        "event_count": event_count,
        "data_status": (
            {"code": "available", "label": None}
            if stored
            else {"code": "source_absent", "label": "데이터 없음"}
        ),
        "topic_shares": ranked_topics,
        "topics": ranked_topics,
        "etc_pct": _numeric(stored.get("etc_pct")) if stored else 100.0,
        "brand_specific_topics": brand_topics,
    }


def _parse_topic_request(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Parse the POST topic request into a normalized service dictionary."""
    view = _text(payload.get("view"))
    selected_brand = _text(payload.get("selected_brand"))
    filter_payload = _filter_payload(payload)
    market_id = _first_filter_value(filter_payload, "atc4") if view == "general" else _text(payload.get("market_id"))
    if not view or not selected_brand or (view == "general" and not market_id and not _has_market_scope(filter_payload)):
        raise TopicRequestError("view, filters.atc4, and selected_brand are required")
    top_n = _integer(payload.get("top_n") or 5)
    return {
        "view": view,
        "market_id": market_id,
        "selected_brand": selected_brand,
        "source": canonical_brand_activity_source(payload.get("source")),
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


def _empty_topic_brand_item(
    brand_set: BrandSetResolution,
    *,
    choice_key: str,
    company_name: str | None = None,
    query_failed: bool = False,
) -> dict[str, JsonValue]:
    """Project an empty topic payload when no assignment scope is available."""
    choice = next(choice for choice in brand_set.choices if choice.brand_key == choice_key)
    return {
        "brand_key": choice.brand_key,
        "brand_name": choice.brand_name,
        "company_name": company_name,
        "is_jw": brand_set.brand_meta[choice_key].is_jw,
        "is_selected": choice.is_selected,
        "sales_rank": choice.sales_rank,
        "event_count": 0,
        "data_status": topic_data_status(
            event_count=0,
            has_mapping=True,
            source_row_count=0,
            classified_row_count=0,
            guard_valid_row_count=0,
            query_failed=query_failed,
        ),
        "topic_shares": [],
        "topics": [],
        "etc_pct": 100.0,
        "brand_specific_topics": [],
    }


def _sliced_topic_brand_item(
    brand_set: BrandSetResolution,
    *,
    choice_key: str,
    topic_scope: dict[str, JsonValue],
    topic_index: dict[str, dict[str, JsonValue]],
    request: dict[str, JsonValue],
    aliases: dict[str, str],
    product_codes: Sequence[str],
    top_n: int,
    company_name: str | None = None,
    query_failed: bool = False,
) -> dict[str, JsonValue]:
    """Project one brand from row-topic assignments under keyword filters."""
    meta = brand_set.brand_meta[choice_key]
    choice = next(choice for choice in brand_set.choices if choice.brand_key == choice_key)
    try:
        rows = _fetch_sliced_topic_rows(
            scope_id=_text(topic_scope.get("scope_id")),
            topic_set_version=_text(topic_scope.get("topic_set_version")),
            product_codes=product_codes,
            visit_locations=_filter_tuple(request.get("visit_location")),
            specialties=_filter_tuple(request.get("specialty")),
            interests=_filter_tuple(request.get("interest")),
            prescription_evolutions=_filter_tuple(request.get("prescription_evolution")),
            period_start=_text(request.get("period_start")),
            period_end=_text(request.get("period_end")),
        )
    except pymysql.MySQLError:
        LOGGER.exception("brand activity topic assignment query failed: brand=%s", choice_key)
        rows = []
        query_failed = True
    stored = _stored_brand_topics(product_codes, topic_index, aliases)
    axis_labels = _axis_topic_label_index(topic_scope)
    brand_labels = _brand_topic_label_index(stored)
    axis_topics: list[dict[str, JsonValue]] = []
    brand_topics: list[dict[str, JsonValue]] = []
    event_count = 0
    source_row_count = 0
    classified_row_count = 0
    guard_valid_row_count = 0
    for row in rows:
        source_row_count = max(
            source_row_count,
            _integer(row.get("source_row_count") or row.get("brand_total_rows")),
        )
        classified_row_count = max(classified_row_count, _integer(row.get("classified_row_count")))
        guard_valid_row_count = max(guard_valid_row_count, _integer(row.get("guard_valid_row_count")))
        topic_id = _text(row.get("topic_id"))
        if not topic_id:
            continue
        event_count = max(event_count, _integer(row.get("brand_total_rows")))
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
    data_status = topic_data_status(
        event_count=event_count,
        has_mapping=bool(product_codes),
        source_row_count=source_row_count,
        classified_row_count=classified_row_count,
        guard_valid_row_count=guard_valid_row_count,
        query_failed=query_failed,
    )
    if data_status["code"] == "identity_mismatch":
        data_status = {**data_status, "label": "필터 적용 불가"}
        LOGGER.warning(
            "brand activity topic identity mismatch: brand=%s source_rows=%d classified_rows=%d guard_valid_rows=%d",
            choice.brand_name,
            source_row_count,
            classified_row_count,
            guard_valid_row_count,
        )
    return {
        "brand_key": choice.brand_key,
        "brand_name": choice.brand_name,
        "company_name": company_name,
        "is_jw": meta.is_jw,
        "is_selected": choice.is_selected,
        "sales_rank": choice.sales_rank,
        "event_count": event_count,
        "data_status": data_status,
        "topic_shares": ranked_topics,
        "topics": ranked_topics,
        "etc_pct": max(0.0, 100.0 - sum(_number(topic.get("share_pct")) for topic in ranked_topics)),
        "brand_specific_topics": brand_topics,
    }


def topic_data_status(
    *,
    event_count: int,
    has_mapping: bool,
    source_row_count: int,
    classified_row_count: int,
    guard_valid_row_count: int,
    query_failed: bool,
) -> dict[str, JsonValue]:
    """Describe keyword availability without collapsing distinct failures."""

    if query_failed:
        return {"code": "unknown", "label": "모름"}
    if not has_mapping:
        return {"code": "mapping_failure", "label": "매핑 실패"}
    if event_count > 0:
        return {"code": "available", "label": None}
    if source_row_count > 0 and classified_row_count > 0 and guard_valid_row_count == 0:
        return {
            "code": "identity_mismatch",
            "label": "재분류 필요",
            "source_row_count": source_row_count,
            "classified_row_count": classified_row_count,
            "guard_valid_row_count": guard_valid_row_count,
        }
    if source_row_count == 0:
        return {"code": "source_absent", "label": "데이터 없음"}
    return {"code": "zero", "label": "0"}


def _stored_brand_topics(
    product_codes: Sequence[str],
    topic_index: dict[str, dict[str, JsonValue]],
    aliases: dict[str, str],
) -> dict[str, JsonValue] | None:
    """Return stored topics for the first matching IQVIA product code."""
    for code in product_codes:
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
        )
    )


def _topic_scope(
    *,
    brand_set: BrandSetResolution,
    topic_rows: Sequence[dict[str, JsonValue]],
) -> dict[str, JsonValue]:
    """Resolve one stored scope from the request market catalog."""
    catalog_codes = set(_catalog_atc4_values(brand_set))
    if not catalog_codes:
        return {}
    if brand_set.view_name == "general":
        # A general-view ATC selection identifies competitors, while topics are
        # stored under reusable group scopes. Prefer the tightest containing
        # group so topic output remains independent of the requested view.
        containing_groups: list[tuple[int, str, dict[str, JsonValue]]] = []
        for row in topic_rows:
            scope_id = _text(row.get("scope_id"))
            row_codes = set(_atc4_values(row.get("atc4_values")))
            if scope_id.startswith("group:") and row_codes and catalog_codes <= row_codes:
                containing_groups.append((len(row_codes), scope_id, row))
        if containing_groups:
            _size, _scope_id, winner = min(
                containing_groups,
                key=lambda candidate: (candidate[0], candidate[1]),
            )
            return _scope_catalog_row(winner)

        if len(catalog_codes) == 1:
            market_codes = _atc4_values(brand_set.market_id)
            direct_scope_id = f"atc4:{market_codes[0]}" if len(market_codes) == 1 else ""
            for row in topic_rows:
                if direct_scope_id and _text(row.get("scope_id")) == direct_scope_id:
                    return _scope_catalog_row(row)
    candidates: list[tuple[int, dict[str, JsonValue]]] = []
    for row in topic_rows:
        row_codes = set(_atc4_values(row.get("atc4_values")))
        if row_codes and row_codes <= catalog_codes:
            candidates.append((len(row_codes), row))
    if not candidates:
        return {}
    largest = max(size for size, _row in candidates)
    winners = [row for size, row in candidates if size == largest]
    if len(winners) == 1:
        return _scope_catalog_row(winners[0])
    return {}


def _topic_scope_failure_reason(
    *,
    brand_set: BrandSetResolution,
    topic_rows: Sequence[dict[str, JsonValue]],
) -> str:
    """Explain which input prevented a stored topic scope from resolving."""
    if not topic_rows:
        return "no_topic_scope:stored_scopes_missing"
    if not _catalog_atc4_values(brand_set):
        return "no_topic_scope:selected_atc4_missing"
    return "no_topic_scope:no_reachable_scope"


def _catalog_atc4_values(brand_set: BrandSetResolution) -> tuple[str, ...]:
    if brand_set.view_name == "general":
        applied_codes = _atc4_values(
            _json_object(brand_set.applied_filter).get("atc4")
        )
        if applied_codes:
            return applied_codes
        return _atc4_values(brand_set.market_id)
    schema = quote_identifier(config.db_name)
    if brand_set.view_name == "strategic_ml":
        row = db.fetch_one(
            f"SELECT atc_codes_json FROM {schema}.`catalog_ml_market` WHERE ml_id = %s LIMIT 1",
            (brand_set.market_id,),
        ) or {}
    elif brand_set.view_name == "strategic_cd":
        row = db.fetch_one(
            f"SELECT ml.atc_codes_json FROM {schema}.`catalog_cd_market` cd "
            f"JOIN {schema}.`catalog_ml_market` ml ON ml.ml_id = cd.ml_id "
            "WHERE cd.cd_id = %s LIMIT 1",
            (brand_set.market_id,),
        ) or {}
    else:
        return ()
    return _atc4_values(row.get("atc_codes_json"))


def _atc4_values(value: JsonValue) -> tuple[str, ...]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            values = text.split(",")
        else:
            values = decoded if isinstance(decoded, list) else [decoded]
    elif isinstance(value, list):
        values = value
    else:
        values = [value] if value is not None else []
    return canonical_atc4_values(item for item in values if str(item).strip())


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
    return _normalized_topic_product_codes(meta.product_codes, aliases)


def _normalized_topic_product_codes(product_codes: Sequence[str], aliases: dict[str, str]) -> tuple[str, ...]:
    """Normalize source-independent product codes for keyword row matching."""
    codes: list[str] = []
    for code in product_codes:
        normalized = normalize_iqvia_en(code)
        if normalized:
            codes.append(normalized)
        alias = aliases.get(normalized)
        if alias:
            codes.append(alias)
    return tuple(dict.fromkeys(codes))


def _topic_product_codes_by_brand(
    *,
    brand_set: BrandSetResolution,
    topic_scope: dict[str, JsonValue],
    aliases: dict[str, str],
) -> dict[str, tuple[str, ...]]:
    """Resolve topic product codes without depending on the requested sales source."""
    scope_index = _topic_brand_index([topic_scope])
    resolved: dict[str, tuple[str, ...]] = {}
    unresolved: dict[str, str] = {}
    for choice in brand_set.choices:
        meta = brand_set.brand_meta[choice.brand_key]
        direct = _brand_product_codes(meta, aliases)
        resolved[choice.brand_key] = direct
        if not direct or _stored_brand_topics(direct, scope_index, aliases) is None:
            unresolved[choice.brand_key] = choice.brand_name
    if not unresolved:
        return resolved
    fallback_codes = iqvia_product_codes_by_brand(unresolved)
    for brand_key, raw_codes in fallback_codes.items():
        fallback = _normalized_topic_product_codes(raw_codes, aliases)
        resolved[brand_key] = tuple(dict.fromkeys((*resolved.get(brand_key, ()), *fallback)))
    return resolved


def _company_names_by_brand(
    brand_set: BrandSetResolution,
    aliases: dict[str, str],
) -> dict[str, str | None]:
    """Return the ", "-joined MANUFACTURER (제조사, MFR NAME KOR) per brand, or None when unmapped.

    Source = iqvia_nsa_quarterly_raw MFR NAME KOR (PL-confirmed). This is the brand's
    MANUFACTURER — deliberately different from interest-timeseries `companies`, which stays
    representing_company (판매사, the keyword-row aggregation unit). The two EPs complement:
    topics shows who makes the brand, interest shows who promotes it.

    The resolver already loaded ``mart_general_brand_metric.by_dimension`` for these brands,
    and that mart field is derived from IQVIA ``MFR NAME KOR``. Reuse it instead of rebuilding
    a product/manufacturer map from every raw IQVIA row on the first request to each pod.
    Multiple rows remain deterministic and preserve CMO/import-repackaging multiplicity.
    """

    del aliases  # kept for signature stability
    manufacturers: dict[str, set[str]] = {}
    for row in brand_set.brand_rows:
        brand_key = _text(row.get("brand_key"))
        dimensions = _json_object(row.get("by_dimension"))
        manufacturer = _text(dimensions.get("manufacturer") or dimensions.get("raw_company")).strip()
        if brand_key and manufacturer:
            manufacturers.setdefault(brand_key, set()).add(manufacturer)

    result: dict[str, str | None] = {}
    for choice in brand_set.choices:
        names = sorted(manufacturers.get(choice.brand_key, ()))
        result[choice.brand_key] = ", ".join(names) if names else None
    return result


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
            SELECT DISTINCT k.id AS row_id,
                            k.stage_row_sha256 AS stage_row_sha256
            FROM {schema}.`km_keyword_event_stage` k
            JOIN {schema}.`mart_brand_activity_topics` topic_scope
              ON JSON_CONTAINS(topic_scope.atc4_values, JSON_QUOTE(k.therapeutic_class), '$')
            WHERE {where_clause}
        ),
        denominator AS (
            SELECT COUNT(DISTINCT row_id) AS brand_total_rows
            FROM scoped_rows
        ),
        status_summary AS (
            SELECT COUNT(DISTINCT CASE WHEN status.status = 'classified' THEN scoped_rows.row_id END)
                       AS classified_row_count,
                   COUNT(DISTINCT CASE
                       WHEN status.status = 'classified'
                        AND status.stage_row_sha256 = scoped_rows.stage_row_sha256
                       THEN scoped_rows.row_id
                   END) AS guard_valid_row_count
            FROM scoped_rows
            LEFT JOIN {schema}.`row_topic_assignment_status` status
              ON status.topic_set_version = %s
             AND status.scope_id = %s
             AND status.row_id = scoped_rows.row_id
        ),
        topic_totals AS (
            SELECT a.topic_id AS topic_id,
                   COUNT(DISTINCT a.row_id) AS affected_row_count,
                   denominator.brand_total_rows AS brand_total_rows,
                   ROUND(COUNT(DISTINCT a.row_id) * 100.0 / NULLIF(denominator.brand_total_rows, 0), 2)
                       AS share_pct
            FROM {schema}.`row_topic_assignment` a
            JOIN scoped_rows ON scoped_rows.row_id = a.row_id
            JOIN {schema}.`row_topic_assignment_status` status
              ON status.topic_set_version = a.topic_set_version
             AND status.scope_id = a.scope_id
             AND status.row_id = a.row_id
            JOIN denominator
            WHERE a.scope_id = %s
              AND a.topic_set_version = %s
              AND status.status = 'classified'
              AND status.stage_row_sha256 = scoped_rows.stage_row_sha256
            GROUP BY a.topic_id, denominator.brand_total_rows
        )
        SELECT topic_totals.topic_id AS topic_id,
               COALESCE(topic_totals.affected_row_count, 0) AS affected_row_count,
               denominator.brand_total_rows AS brand_total_rows,
               topic_totals.share_pct AS share_pct,
               denominator.brand_total_rows AS source_row_count,
               status_summary.classified_row_count AS classified_row_count,
               status_summary.guard_valid_row_count AS guard_valid_row_count
        FROM denominator
        CROSS JOIN status_summary
        LEFT JOIN topic_totals ON TRUE
        WHERE denominator.brand_total_rows > 0
        ORDER BY topic_totals.share_pct DESC, topic_totals.topic_id
    """
    return db.fetch_all(
        sql,
        (*params, topic_set_version, scope_id, scope_id, topic_set_version),
    )


def _append_in_filter(filters: list[str], params: list[object], column: str, values: Sequence[str]) -> None:
    """Append a parameterized IN predicate for one active keyword dimension."""
    if not values:
        return
    placeholders = ", ".join(["%s"] * len(values))
    filters.append(f"{column} IN ({placeholders})")
    params.extend(values)

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
    if applied:
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


def _has_market_scope(filter_payload: dict[str, JsonValue]) -> bool:
    return isinstance(filter_payload.get("market_scope"), dict)


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
