from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from .audit import write_json
from .models import JsonValue
from .topic_store import TopicStoreError


VERIFICATION_FILE: Final = "singleconcept_top7_verification.json"
INPUT_RATE_USD_PER_MILLION: Final = 0.50
OUTPUT_RATE_USD_PER_MILLION: Final = 3.00


def build_verification(
    *,
    run_summary: dict[str, JsonValue],
    call_log: list[JsonValue],
    quality_summary: dict[str, JsonValue],
    label_quality_summary: dict[str, JsonValue],
    brand_results: dict[str, JsonValue] | None = None,
    derived_post_hoc: bool = False,
    derived_at: str = "",
) -> dict[str, JsonValue]:
    """Build the legacy verification sidecar from measured current-run artifacts."""
    prompt_tokens = sum(_call_token(row, "prompt_tokens") for row in call_log)
    completion_tokens = sum(_call_token(row, "completion_tokens") for row in call_log)
    brand_payloads = _brand_payloads(brand_results or {})
    result: dict[str, JsonValue] = {
        "average_etc_pct": _nullable_number(quality_summary.get("average_etc_pct")),
        "backend_counts": _counts(call_log, "backend"),
        "brand_count_max": _max_brand_count_by_scope(brand_payloads),
        "brand_counts_by_scope": _brand_counts_by_scope(brand_payloads),
        "brand_specific_dedup_count": _sum_int_field(brand_payloads, "brand_specific_dedup_count"),
        "brand_specific_duplicate_pair_count": _int_value(label_quality_summary.get("brand_specific_duplicate_pair_count") or run_summary.get("brand_specific_duplicate_pair_count")),
        "completion_tokens": completion_tokens,
        "complex_label_count": _int_value(label_quality_summary.get("complex_label_count") or run_summary.get("complex_label_count")),
        "cost_note": "proxy estimate: input $0.50/M, output $3.00/M from sanitized usage",
        "estimated_usd_vertex_flash_proxy": _estimated_cost(prompt_tokens, completion_tokens),
        "executed_call_count": _int_value(run_summary.get("executed_call_count")) or len(call_log),
        "model_counts": _counts(call_log, "model_id"),
        "prompt_tokens": prompt_tokens,
        "quality_grade_distribution": _quality_distribution(run_summary, quality_summary),
        "raw_text_leak_count": _int_value(run_summary.get("raw_text_leak_count")),
        "retry_count": _retry_count(call_log),
        "serving_route": _serving_route(call_log),
        "share_total_failures": _int_value(quality_summary.get("share_total_failures")),
        "single_concept_rewrite_count": _sum_nested_int(brand_payloads, "single_concept_rewritten"),
        "status_counts": _counts(call_log, "status"),
        "tag": _text(run_summary.get("tag")),
        "timeout_count": _timeout_count(call_log),
        "topic_id_backfill_count": _sum_int_field(brand_payloads, "topic_id_backfill_count"),
    }
    if derived_post_hoc:
        result.update(
            {
                "derived_post_hoc": True,
                "derived_at": derived_at or datetime.now(UTC).isoformat(timespec="seconds"),
                "derived_from": [
                    "call_log_sanitized.json",
                    "run_summary.json",
                    "quality_summary.json",
                    "label_quality_summary.json",
                    "brand_results_sanitized.json",
                ],
            }
        )
    return result


def build_verification_from_audit_dir(audit_dir: Path, *, derived_post_hoc: bool = False) -> dict[str, JsonValue]:
    """Build verification from files already written in one audit directory."""
    run_summary = _read_json_object(audit_dir / "run_summary.json")
    call_log = _read_json_array(audit_dir / "call_log_sanitized.json")
    quality = _read_json_object(audit_dir / "quality_summary.json")
    label_quality = _read_json_object(audit_dir / "label_quality_summary.json")
    brand_results = _read_json_object(audit_dir / "brand_results_sanitized.json")
    return build_verification(
        run_summary=run_summary,
        call_log=call_log,
        quality_summary=quality,
        label_quality_summary=label_quality,
        brand_results=brand_results,
        derived_post_hoc=derived_post_hoc,
    )


def write_verification_file(audit_dir: Path, *, derived_post_hoc: bool = False) -> dict[str, JsonValue]:
    """Write `singleconcept_top7_verification.json` into an audit directory."""
    payload = build_verification_from_audit_dir(audit_dir, derived_post_hoc=derived_post_hoc)
    write_json(audit_dir / VERIFICATION_FILE, payload)
    return payload


def _read_json_object(path: Path) -> dict[str, JsonValue]:
    if not path.exists():
        raise TopicStoreError(f"required artifact missing for verification: {path.name}")
    import json

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TopicStoreError(f"required verification input is not an object: {path.name}")
    return value


def _read_json_array(path: Path) -> list[JsonValue]:
    if not path.exists():
        return []
    import json

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise TopicStoreError(f"required verification input is not an array: {path.name}")
    return value


def _call_token(value: JsonValue, key: str) -> int:
    call = _dict(value)
    usage = _dict(call.get("usage"))
    return _int_value(usage.get(key) or call.get(key))


def _counts(call_log: list[JsonValue], key: str) -> dict[str, JsonValue]:
    counter: Counter[str] = Counter()
    for value in call_log:
        text = _text(_dict(value).get(key))
        if text:
            counter[text] += 1
    return dict(sorted(counter.items()))


def _serving_route(call_log: list[JsonValue]) -> dict[str, JsonValue]:
    first = next((_dict(value) for value in call_log if _dict(value)), {})
    endpoint = _text(first.get("endpoint"))
    return {
        "backend": _text(first.get("backend")) or "direct_serving",
        "gateway_used": "gateway" in endpoint,
        "manual_hosts_used": "localhost" in endpoint or "127.0.0.1" in endpoint,
        "model_id": _text(first.get("model_id")),
    }


def _quality_distribution(run_summary: dict[str, JsonValue], quality_summary: dict[str, JsonValue]) -> dict[str, JsonValue]:
    value = quality_summary.get("grade_distribution") or run_summary.get("quality_grade_distribution")
    source = _dict(value)
    return {grade: _int_value(source.get(grade)) for grade in ("A", "B", "C", "D")}


def _estimated_cost(prompt_tokens: int, completion_tokens: int) -> float:
    return round(prompt_tokens / 1_000_000 * INPUT_RATE_USD_PER_MILLION + completion_tokens / 1_000_000 * OUTPUT_RATE_USD_PER_MILLION, 4)


def _brand_payloads(brand_results: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    return [_dict(value) for value in brand_results.values()]


def _brand_counts_by_scope(brand_payloads: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    counter: Counter[str] = Counter()
    for brand in brand_payloads:
        scope = _text(brand.get("scope_key") or brand.get("scope_id"))
        if scope:
            counter[scope] += 1
    return dict(sorted(counter.items()))


def _max_brand_count_by_scope(brand_payloads: list[dict[str, JsonValue]]) -> int:
    counts = _brand_counts_by_scope(brand_payloads)
    values = [value for value in counts.values() if isinstance(value, int)]
    return max(values, default=0)


def _sum_int_field(rows: list[dict[str, JsonValue]], key: str) -> int:
    return sum(_int_value(row.get(key)) for row in rows)


def _sum_nested_int(rows: list[dict[str, JsonValue]], key: str) -> int:
    total = 0
    for row in rows:
        for collection_key in ("topic_shares", "brand_specific_topics"):
            for item in _list(row.get(collection_key)):
                if _dict(item).get(key) is True:
                    total += 1
    return total


def _retry_count(call_log: list[JsonValue]) -> int:
    total = 0
    for value in call_log:
        retry = _dict(_dict(value).get("retry"))
        total += _int_value(retry.get("retry_count"))
    return total


def _timeout_count(call_log: list[JsonValue]) -> int:
    total = 0
    for value in call_log:
        call = _dict(value)
        status = _text(call.get("status")).lower()
        phase = _text(call.get("phase")).lower()
        error_type = _text(call.get("error_type")).lower()
        if "timeout" in status or "timeout" in phase or "timeout" in error_type:
            total += 1
    return total


def _nullable_number(value: JsonValue) -> JsonValue:
    return value if isinstance(value, int | float) else None


def _int_value(value: JsonValue) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""
