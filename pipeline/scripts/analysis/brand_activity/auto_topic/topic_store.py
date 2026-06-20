from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import json
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


def load_artifacts(audit_dir: Path) -> TopicArtifacts:
    """Load the measured latest-run files without raw prompt or source text."""
    return TopicArtifacts(
        run_summary=_read_json_object(audit_dir / "run_summary.json"),
        verification=_read_json_object(audit_dir / "singleconcept_top7_verification.json"),
        viz_payload=_read_json_object(audit_dir / "viz_payload.json"),
        axis_results=_read_json_object(audit_dir / "axis_results_sanitized.json"),
        call_log=_read_json_array(audit_dir / "call_log_sanitized.json"),
        alias_payload=_load_alias_payload(),
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
                "avg_etc_pct": market.get("avg_etc_pct"),
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
        created_at=_created_at_from_run_id(run_id),
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
    )


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


def _created_at_from_run_id(run_id: str) -> str:
    """Parse the timestamp suffix in run tags into a SQL DATETIME string."""
    suffix = run_id.rsplit("_exec_", 1)[-1]
    try:
        return datetime.strptime(suffix, "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return DEFAULT_CREATED_AT


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
        case "genos-pro":
            return "145"
        case "genos-flash":
            return "76"
        case "genos-lite":
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
