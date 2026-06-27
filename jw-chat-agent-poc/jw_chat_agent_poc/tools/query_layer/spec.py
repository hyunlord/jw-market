from __future__ import annotations

import ast
import json
from typing import Any, Mapping

from jw_chat_agent_poc.tools.query_layer.catalog import QueryCatalog
from jw_chat_agent_poc.tools.query_layer.store import MartRecord


def parse_spec(raw: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items()}
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = ast.literal_eval(text)
    if not isinstance(parsed, dict):
        raise TypeError("query spec must be an object")
    return {str(key): value for key, value in parsed.items()}


def validate_spec(spec: Mapping[str, Any], catalog: QueryCatalog) -> None:
    fragments = catalog.schema_fragment()
    for key, enum_key in (("dimensions", "dimensions"), ("group_by", "group_by"), ("metrics", "metrics"), ("derive", "derive")):
        unknown = [item for item in as_list(spec.get(key)) if item not in fragments[enum_key]]
        if unknown:
            raise ValueError(f"{key} unknown {unknown}; use catalog enum only")
    filters = spec.get("filters")
    if isinstance(filters, Mapping):
        allowed = set(fragments["dimensions"]) | {"brand", "period", "periods"}
        unknown_filters = [str(key) for key in filters if str(key) not in allowed]
        if unknown_filters:
            raise ValueError(f"filters unknown {unknown_filters}; use catalog enum only")
    sort = spec.get("sort")
    if sort and str(sort) not in fragments["sort"]:
        raise ValueError(f"sort unknown {sort}; use catalog enum only")


def as_list(value: Any) -> list[str]:
    if isinstance(value, list | tuple):
        return [str(item) for item in value]
    if isinstance(value, str) and value:
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def bounded_limit(value: Any, default: int) -> int:
    try:
        return max(1, min(int(str(value)), 50))
    except (TypeError, ValueError):
        return default


def level_name(spec: Mapping[str, Any]) -> str:
    group_by = as_list(spec.get("group_by")) or as_list(spec.get("dimensions"))
    key = group_by[0] if group_by else "product"
    if key == "product":
        return "Brand"
    if key == "molecule":
        return "Molecule"
    return key


def dimension_value(record: MartRecord, key: str) -> str:
    if key == "company":
        return record.company() or "unknown"
    if key == "molecule":
        return record.molecule() or "unknown"
    if key == "dosage_form":
        return record.dosage_form() or record.class_label() or "unknown"
    if key == "nhi_type":
        return record.nhi_type() or "unknown"
    if key == "ox_gx":
        return record.ox_gx() or "unknown"
    return record.brand_name
