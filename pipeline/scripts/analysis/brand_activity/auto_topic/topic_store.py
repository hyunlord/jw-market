from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import json
import re
from typing import Final

from .data_source import FALLBACK_ALIAS_PATH, PRIMARY_ALIAS_PATH, SCHEMA
from .models import JsonValue


DEFAULT_CREATED_AT: Final = "1970-01-01 00:00:00"


class TopicStoreError(RuntimeError):
    """Raised when topic result artifacts cannot be stored safely."""


@dataclass(frozen=True, slots=True)
class TopicArtifacts:
    """Measured run artifacts needed to build API-ready topic records."""

    run_summary: dict[str, JsonValue]
    verification: dict[str, JsonValue]
    viz_payload: dict[str, JsonValue]
    axis_results: dict[str, JsonValue]
    call_log: list[JsonValue] = field(default_factory=list)
    alias_payload: dict[str, JsonValue] = field(default_factory=dict)
    db_snapshot: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TopicRecord:
    """One API-ready market payload row."""

    scope_id: str
    display_name: str
    atc4_values: tuple[str, ...]
    quality_grade: str
    source_row_count: int
    payload: dict[str, JsonValue]
    brand_count: int


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One stored topic run metadata row."""

    run_id: str
    created_at: str
    model_id: str
    serving_id: str
    route: str
    total_prompt_tokens: int
    total_completion_tokens: int
    est_cost_usd: float
    market_count: int
    brand_count: int
    axis_compound_count: int
    brand_specific_dup_count: int
    sha256: str
    input_fingerprint: str


def load_artifacts(audit_dir: Path) -> TopicArtifacts:
    """Load the measured latest-run files without raw prompt or source text."""
    return TopicArtifacts(
        run_summary=_read_json_object(audit_dir / "run_summary.json"),
        verification=_read_json_object(audit_dir / "singleconcept_top7_verification.json"),
        viz_payload=_read_json_object(audit_dir / "viz_payload.json"),
        axis_results=_read_json_object(audit_dir / "axis_results_sanitized.json"),
        call_log=_read_json_array(audit_dir / "call_log_sanitized.json"),
        alias_payload=_load_alias_payload(),
        db_snapshot=_read_optional_json_object(audit_dir / "db_snapshot.json"),
    )


def build_topic_records(artifacts: TopicArtifacts) -> list[TopicRecord]:
    """Build one API payload per primary market in `viz_payload`."""
    brands_by_scope = _brands_by_scope(_list(artifacts.viz_payload.get("brand_results")))
    alias_by_brand = _alias_by_brand(artifacts.alias_payload)
    records: list[TopicRecord] = []
    for market_value in _list(artifacts.viz_payload.get("markets")):
        market = _dict(market_value)
        scope_key = _required_text(market, "scope_key")
        scope_id = _required_text(market, "scope_id")
        axis = _dict(artifacts.axis_results.get(scope_key))
        brands = [_brand_payload(_dict(brand), alias_by_brand) for brand in brands_by_scope.get(scope_key, [])]
        payload = {
            "scope": market,
            "axis": {
                "scope_id": axis.get("scope_id") or scope_id,
                "axis_version": axis.get("axis_version"),
                "topics": _list(axis.get("topics")),
                "source_row_count": axis.get("source_row_count") or market.get("axis_row_count"),
                "chunking": axis.get("chunking"),
            },
            "brands": brands,
            "quality": {
                "grade": market.get("quality_grade"),
                "reasons": market.get("reasons") or [],
            },
            "generated_from": artifacts.viz_payload.get("generated_from"),
        }
        records.append(
            TopicRecord(
                scope_id=scope_id,
                display_name=_text(market.get("display_name")),
                atc4_values=_text_tuple(market.get("atc4_values")),
                quality_grade=_text(market.get("quality_grade")),
                source_row_count=_int_value(axis.get("source_row_count") or market.get("axis_row_count")),
                payload=payload,
                brand_count=len(brands),
            )
        )
    return records


def build_run_record(artifacts: TopicArtifacts, *, artifact_sha256: str) -> RunRecord:
    """Build run metadata from measured verification and run summary files."""
    run_id = _required_text(artifacts.run_summary, "tag")
    route = _text(_dict(artifacts.verification.get("serving_route")).get("backend")) or "direct_serving"
    model_id = _text(_dict(artifacts.verification.get("serving_route")).get("model_id")) or _first_call_text(artifacts.call_log, "model_id")
    return RunRecord(
        run_id=run_id,
        created_at=_created_at_from_summary(artifacts.run_summary, run_id),
        model_id=model_id,
        serving_id=_first_call_text(artifacts.call_log, "serving_id") or _serving_id(model_id),
        route=route,
        total_prompt_tokens=_int_value(artifacts.verification.get("prompt_tokens")),
        total_completion_tokens=_int_value(artifacts.verification.get("completion_tokens")),
        est_cost_usd=_float_value(artifacts.verification.get("estimated_usd_vertex_flash_proxy")),
        market_count=len(_list(artifacts.viz_payload.get("markets"))),
        brand_count=len(_list(artifacts.viz_payload.get("brand_results"))),
        axis_compound_count=_int_value(artifacts.verification.get("complex_label_count")),
        brand_specific_dup_count=_int_value(artifacts.verification.get("brand_specific_duplicate_pair_count")),
        sha256=artifact_sha256,
        input_fingerprint=_input_fingerprint(artifacts),
    )


def topic_payload_sample(payload: dict[str, JsonValue], *, brand_limit: int = 2, topic_limit: int = 3) -> dict[str, JsonValue]:
    """Extract non-sensitive topic labels and shares from the stored mart payload shape."""
    axis = _dict(payload.get("axis"))
    brands = _list(payload.get("brands"))[:brand_limit]
    return {
        "axis_topics": [_topic_label_share(_dict(topic)) for topic in _list(axis.get("topics"))[:topic_limit]],
        "brand_topics": [
            {
                "brand": _text(_dict(brand).get("brand")),
                "topic_shares": [
                    _topic_label_share(_dict(topic))
                    for topic in _list(_dict(brand).get("topic_shares"))[:topic_limit]
                ],
                "brand_specific_topics": [
                    _topic_label_share(_dict(topic))
                    for topic in _list(_dict(brand).get("brand_specific_topics"))[:topic_limit]
                ],
            }
            for brand in brands
        ],
    }


def validated_stage_schema(schema: str) -> str:
    """Allow writes only to the brand-activity isolated stage schema."""
    if schema != SCHEMA:
        raise TopicStoreError(f"refusing schema outside {SCHEMA}: {schema}")
    return schema


def _brand_payload(brand: dict[str, JsonValue], alias_by_brand: dict[str, dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """Add stable alias metadata to one measured brand payload when available."""
    product = _text(brand.get("brand"))
    alias = alias_by_brand.get(product, {})
    return {
        **brand,
        "is_jw": alias.get("is_jw") if alias else brand.get("is_jw"),
        "kr_canonical": alias.get("kr_canonical") if alias else brand.get("kr_canonical"),
    }


def _brands_by_scope(values: list[JsonValue]) -> dict[str, list[dict[str, JsonValue]]]:
    """Group measured primary brand payloads by market scope."""
    grouped: dict[str, list[dict[str, JsonValue]]] = {}
    for value in values:
        brand = _dict(value)
        scope_key = _text(brand.get("scope_key"))
        grouped.setdefault(scope_key, []).append(brand)
    return grouped


def _load_alias_payload() -> dict[str, JsonValue]:
    """Load alias metadata for `is_jw` enrichment when the artifact exists."""
    for path in (PRIMARY_ALIAS_PATH, FALLBACK_ALIAS_PATH):
        if path.exists():
            return _read_json_object(path)
    return {}


def _alias_by_brand(alias_payload: dict[str, JsonValue]) -> dict[str, dict[str, JsonValue]]:
    """Index alias records by IQVIA English product name."""
    records = alias_payload.get("records")
    if not isinstance(records, list):
        return {}
    return {
        str(record["iqvia_en"]): record
        for record in records
        if isinstance(record, dict) and isinstance(record.get("iqvia_en"), str)
    }


def _read_json_object(path: Path) -> dict[str, JsonValue]:
    """Read a required JSON object with an explicit missing-file error."""
    if not path.exists():
        raise TopicStoreError(f"required artifact missing: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TopicStoreError(f"required artifact is not an object: {path.name}")
    return value


def _read_json_array(path: Path) -> list[JsonValue]:
    """Read an optional JSON array, returning empty when the file is absent."""
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, list) else []


def _read_optional_json_object(path: Path) -> dict[str, JsonValue]:
    """Read an optional JSON object with empty-object fallback."""
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _created_at_from_summary(run_summary: dict[str, JsonValue], run_id: str) -> str:
    """Prefer measured start time, then parse the timestamp suffix in run tags."""
    for key in ("started_at", "created_at"):
        value = _text(run_summary.get(key))
        if _parse_datetime(value) != DEFAULT_CREATED_AT:
            return _parse_datetime(value)
    return _created_at_from_run_id(run_id)


def _created_at_from_run_id(run_id: str) -> str:
    """Parse the final timestamp token in run tags into a SQL DATETIME string."""
    match = re.search(r"(\d{8}_\d{6})$", run_id)
    suffix = match.group(1) if match else run_id.rsplit("_exec_", 1)[-1]
    return _parse_datetime(suffix)


def _parse_datetime(value: str) -> str:
    """Parse supported audit timestamp strings into SQL DATETIME format."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%d_%H%M%S"):
        candidate = value.rstrip("Z")
        try:
            return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return DEFAULT_CREATED_AT


def _input_fingerprint(artifacts: TopicArtifacts) -> str:
    """Return the stage input fingerprint used by the scheduler no-op guard."""
    from_summary = _text(artifacts.run_summary.get("input_fingerprint"))
    if from_summary:
        return from_summary
    before = _dict(artifacts.db_snapshot.get("before"))
    return _text(before.get("stage_hash_fingerprint"))


def _topic_label_share(topic: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Keep only topic metadata safe for audit samples."""
    sample: dict[str, JsonValue] = {
        "topic_id": topic.get("topic_id"),
        "label": topic.get("label"),
    }
    if "share_pct" in topic:
        sample["share_pct"] = topic.get("share_pct")
    return sample


def _first_call_text(call_log: list[JsonValue], key: str) -> str:
    """Return the first non-empty text field from sanitized call logs."""
    for value in call_log:
        text = _text(_dict(value).get(key))
        if text:
            return text
    return ""


def _serving_id(model_id: str) -> str:
    """Map the retained serving-direct model aliases to their serving ids."""
    match model_id:
        case "genos-pro" | "genos-flash" | "genos-lite":
            return "163"
        case _:
            return ""


def _required_text(values: dict[str, JsonValue], key: str) -> str:
    """Return a required non-empty string field."""
    text = _text(values.get(key))
    if not text:
        raise TopicStoreError(f"required field missing: {key}")
    return text


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    """Return a JSON object or an empty object."""
    return value if isinstance(value, dict) else {}


def _list(value: JsonValue) -> list[JsonValue]:
    """Return a JSON array or an empty array."""
    return value if isinstance(value, list) else []


def _text(value: JsonValue) -> str:
    """Return a string value or an empty string."""
    return value if isinstance(value, str) else ""


def _text_tuple(value: JsonValue) -> tuple[str, ...]:
    """Return a tuple of strings from a JSON array."""
    return tuple(str(item) for item in value if isinstance(item, str)) if isinstance(value, list) else ()


def _int_value(value: JsonValue) -> int:
    """Return an integer from a JSON scalar."""
    return int(value) if isinstance(value, int | float) else 0


def _float_value(value: JsonValue) -> float:
    """Return a float from a JSON scalar."""
    return float(value) if isinstance(value, int | float) else 0.0
