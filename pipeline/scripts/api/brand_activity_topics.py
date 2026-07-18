from __future__ import annotations

import json
import re
from typing import Any, Final, TypeAlias

from pipeline.scripts.api import db
from pipeline.scripts.api.config import config


JsonValue: TypeAlias = Any


TOPICS_TABLE: Final = "mart_brand_activity_topics"
IDENTIFIER_PATTERN: Final = re.compile(r"^[A-Za-z0-9_]+$")


class TopicPayloadError(RuntimeError):
    """Raised when stored Brand Activity topic payloads cannot be projected."""


def list_topic_payloads() -> list[dict[str, JsonValue]]:
    """Return all stored Brand Activity topic payloads in the public API contract."""
    return [_project_row(row) for row in _fetch_topic_rows()]


def get_topic_payload(scope_id: str) -> dict[str, JsonValue] | None:
    """Return one Brand Activity topic payload, or None when scope_id is absent."""
    rows = _fetch_topic_rows(scope_id=scope_id)
    if not rows:
        return None
    return _project_row(rows[0])


def _fetch_topic_rows(scope_id: str | None = None) -> list[dict[str, JsonValue]]:
    """Read measured topic payload rows from the isolated Brand Activity schema."""
    table = _qualified_topic_table(config.brand_activity_db_name)
    where_clause = "WHERE scope_id = %s" if scope_id else ""
    params: tuple[str, ...] = (scope_id,) if scope_id else ()
    rows = db.fetch_all(
        f"""
        SELECT scope_id, display_name, atc4_values, quality_grade, source_row_count, run_id, payload
        FROM {table}
        {where_clause}
        ORDER BY scope_id
        """,
        params,
    )
    return [_json_row(row) for row in rows]


def _qualified_topic_table(schema: str) -> str:
    """Return a safely quoted stage-schema topic table name."""
    if not IDENTIFIER_PATTERN.fullmatch(schema):
        raise TopicPayloadError(f"invalid Brand Activity schema: {schema}")
    return f"`{schema}`.`{TOPICS_TABLE}`"


def _project_row(row: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Project one stored DB row into the public Brand Activity topic contract."""
    payload = _json_object(row.get("payload"))
    scope = _json_object(payload.get("scope"))
    axis = _json_object(payload.get("axis"))
    quality = _json_object(payload.get("quality"))
    projected_scope = {
        "scope_id": _text(scope.get("scope_id") or row.get("scope_id")),
        "display_name": _text(scope.get("display_name") or row.get("display_name")),
        "atc4_values": _text_list(scope.get("atc4_values")),
        "scope_type": _text(scope.get("scope_type")),
        "quality_grade": _text(scope.get("quality_grade") or row.get("quality_grade")),
        "avg_etc_pct": _number(scope.get("avg_etc_pct")),
        "source_row_count": _integer(axis.get("source_row_count") or scope.get("source_row_count") or row.get("source_row_count")),
    }
    return {
        "scope": projected_scope,
        "axis": {
            "axis_version": _text(axis.get("axis_version")),
            "source_row_count": projected_scope["source_row_count"],
            "topics": [_public_axis_topic(topic) for topic in sorted(_json_list(axis.get("topics")), key=_topic_sort_key)],
        },
        "brands": [_public_brand(brand) for brand in _json_list(payload.get("brands"))],
        "quality": {
            "grade": _text(quality.get("grade") or projected_scope["quality_grade"]),
            "avg_etc_pct": _number(quality.get("avg_etc_pct") or projected_scope["avg_etc_pct"]),
            "reasons": _text_list(quality.get("reasons")),
        },
    }


def _public_axis_topic(value: JsonValue) -> dict[str, JsonValue]:
    """Project one axis topic into public fields."""
    topic = _json_object(value)
    return {
        "topic_id": _text(topic.get("topic_id")),
        "label": _text(topic.get("label")),
        "definition": _text(topic.get("definition")),
        "keywords": _text_list(topic.get("keywords")),
    }


def _public_brand(value: JsonValue) -> dict[str, JsonValue]:
    """Project one brand payload while dropping diagnostics."""
    brand = _json_object(value)
    shares = [_public_topic_share(share) for share in _json_list(brand.get("topic_shares"))]
    shares.sort(key=lambda share: _number(share.get("share_pct")), reverse=True)
    return {
        "brand": _text(brand.get("brand")),
        "is_jw": _bool(brand.get("is_jw")),
        "etc_pct": _number(brand.get("etc_pct")),
        "topic_shares": shares,
        "topics": shares,
        "brand_specific_topics": [_public_brand_specific(topic) for topic in _json_list(brand.get("brand_specific_topics"))],
    }


def _public_topic_share(value: JsonValue) -> dict[str, JsonValue]:
    """Project one market-axis share into public fields."""
    share = _json_object(value)
    return {
        "topic_id": _text(share.get("topic_id")),
        "label": _text(share.get("label")),
        "share_pct": _number(share.get("share_pct")),
        "row_count": _topic_row_count(share),
    }


def _public_brand_specific(value: JsonValue) -> dict[str, JsonValue]:
    """Project one brand-specific topic into public fields."""
    topic = _json_object(value)
    return {
        "topic_id": _text(topic.get("topic_id")),
        "label": _text(topic.get("label")),
        "definition": _text(topic.get("definition")),
        "share_pct": _number(topic.get("share_pct")),
        "row_count": _topic_row_count(topic),
    }


def _topic_row_count(topic: dict[str, JsonValue]) -> int:
    """Return affected rows for one topic; topic totals are independent, not brand-event totals."""
    return _integer(topic.get("row_count") or topic.get("affected_row_count"))


def _json_row(row: dict[str, object]) -> dict[str, JsonValue]:
    """Convert DB rows into the module JSON value type."""
    return {str(key): _json_value(value) for key, value in row.items()}


def _json_value(value: object) -> JsonValue:
    """Return a JSON-compatible scalar for DB values used by this route."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, bytes | bytearray):
        return value.decode("utf-8")
    return str(value)


def _json_object(value: JsonValue) -> dict[str, JsonValue]:
    """Return a JSON object, parsing strings at the DB boundary."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str | bytes | bytearray):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise TopicPayloadError("invalid JSON payload") from exc
        if isinstance(parsed, dict):
            return parsed
    return {}


def _json_list(value: JsonValue) -> list[JsonValue]:
    """Return a JSON array or an empty array."""
    return value if isinstance(value, list) else []


def _text(value: JsonValue) -> str:
    """Return a text value for API output."""
    return value if isinstance(value, str) else ""


def _text_list(value: JsonValue) -> list[str]:
    """Return only string items from a JSON array."""
    return [item for item in _json_list(value) if isinstance(item, str)]


def _number(value: JsonValue) -> float:
    """Return a numeric JSON scalar as float."""
    return float(value) if isinstance(value, int | float) else 0.0


def _integer(value: JsonValue) -> int:
    """Return a numeric JSON scalar as int."""
    return int(value) if isinstance(value, int | float) else 0


def _bool(value: JsonValue) -> bool:
    """Return null and non-bool values as False."""
    return value if isinstance(value, bool) else False


def _topic_sort_key(value: JsonValue) -> str:
    """Sort axis topics by stable topic id."""
    return _text(_json_object(value).get("topic_id"))
