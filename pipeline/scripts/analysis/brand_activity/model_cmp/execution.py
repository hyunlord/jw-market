from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .cache import build_cache_key, stable_input_hash
from .data_source import keyword_stats
from .dictionary import seed_for_atc4_values
from .llm import MODEL_SPECS, call_genos_json, call_log_to_json
from .models import CallLog, JsonValue, KeywordRow, ModelSpec, ScopeSpec, TopicDefinition
from .privacy import estimate_tokens
from .prompts import PROMPT_VERSION, brand_share_prompt, market_axis_prompt
from .quality import max_share_delta_pp, share_map, topic_overlap_score
from .response import normalize_share_payload, topics_from_payload
from .sampling import SCOPE_SPECS


REPRESENTATIVE_SAMPLE_KEY = "group:livalo_family:C10C0:LIVALOZET"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Sanitized GenOS result bundle used by reports and audit."""

    axis_results: dict[str, JsonValue]
    brand_results: dict[str, JsonValue]
    repeat_results: dict[str, JsonValue]
    call_logs: list[CallLog]


def build_call_plan(axis_samples: dict[str, list[KeywordRow]], brand_samples: dict[str, list[KeywordRow]]) -> list[dict[str, JsonValue]]:
    """Create a bounded, deterministic call plan before any GenOS request is made."""
    rows: list[dict[str, JsonValue]] = []
    for scope in SCOPE_SPECS:
        for model in MODEL_SPECS:
            sample_rows = axis_samples[scope.scope_id]
            input_hash = stable_input_hash(sample_rows, prompt_version=PROMPT_VERSION, axis_version=scope.scope_id)
            rows.append(_plan_row("market_axis", model.model_key, model.serving_id, scope.scope_id, "*", sample_rows, input_hash))
    for scope in SCOPE_SPECS:
        for atc4, brand in scope.share_brands:
            sample_key = f"{scope.scope_id}:{atc4}:{brand}"
            for model in MODEL_SPECS:
                sample_rows = brand_samples[sample_key]
                input_hash = stable_input_hash(sample_rows, prompt_version=PROMPT_VERSION, axis_version=scope.scope_id)
                rows.append(_plan_row("brand_share", model.model_key, model.serving_id, scope.scope_id, brand, sample_rows, input_hash))
    for model in MODEL_SPECS:
        rows.append(
            {
                **_plan_row(
                    "brand_share_repeat",
                    model.model_key,
                    model.serving_id,
                    "group:livalo_family",
                    "LIVALOZET",
                    brand_samples[REPRESENTATIVE_SAMPLE_KEY],
                    _repeat_hash(brand_samples),
                ),
                "sample_key": REPRESENTATIVE_SAMPLE_KEY,
            }
        )
    return rows


def execute_model_calls(
    *,
    token: str,
    dictionary: dict[str, JsonValue],
    axis_samples: dict[str, list[KeywordRow]],
    brand_samples: dict[str, list[KeywordRow]],
) -> ExecutionResult:
    """Run axis, brand-share, and repeat calls for the bounded 3-model sample."""
    axis_results: dict[str, JsonValue] = {model.model_key: {} for model in MODEL_SPECS}
    brand_results: dict[str, JsonValue] = {model.model_key: {} for model in MODEL_SPECS}
    repeat_results: dict[str, JsonValue] = {}
    axis_topics: dict[str, dict[str, list[TopicDefinition]]] = {model.model_key: {} for model in MODEL_SPECS}
    logs: list[CallLog] = []
    for scope in SCOPE_SPECS:
        seed = seed_for_atc4_values(dictionary, scope.atc4_values)
        for model in MODEL_SPECS:
            rows = axis_samples[scope.scope_id]
            input_hash = stable_input_hash(rows, prompt_version=PROMPT_VERSION, axis_version=scope.scope_id)
            payload, log = call_genos_json(
                token=token,
                spec=model,
                task="market_axis",
                scope_id=scope.scope_id,
                brand="*",
                messages=market_axis_prompt(scope_id=scope.scope_id, scope_label=scope.label, rows=rows, seed_dictionary=seed),
                rows=rows,
                input_hash=input_hash,
            )
            logs.append(log)
            topics = topics_from_payload(payload, scope.label)
            axis_topics[model.model_key][scope.scope_id] = topics
            _dict(axis_results[model.model_key])[scope.scope_id] = _axis_payload(scope, payload, topics)
    for scope in SCOPE_SPECS:
        for atc4, brand in scope.share_brands:
            sample_key = f"{scope.scope_id}:{atc4}:{brand}"
            for model in MODEL_SPECS:
                rows = brand_samples[sample_key]
                payload, log = _call_brand(token, model, scope, brand, rows, axis_topics[model.model_key][scope.scope_id])
                logs.append(log)
                _dict(brand_results[model.model_key])[sample_key] = payload
    for model in MODEL_SPECS:
        scope = SCOPE_SPECS[0]
        rows = brand_samples[REPRESENTATIVE_SAMPLE_KEY]
        payload, log = _call_brand(token, model, scope, "LIVALOZET", rows, axis_topics[model.model_key][scope.scope_id], task="brand_share_repeat")
        logs.append(log)
        repeat_results[model.model_key] = payload
    return ExecutionResult(axis_results=axis_results, brand_results=brand_results, repeat_results=repeat_results, call_logs=logs)


def skipped_execution() -> ExecutionResult:
    """Return an empty execution result for design-only dry runs."""
    return ExecutionResult(axis_results={}, brand_results={}, repeat_results={}, call_logs=[])


def execution_summary(result: ExecutionResult, rows: list[KeywordRow], call_plan: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """Summarize sanitized execution outputs for the three Markdown reports."""
    return {
        "axis_results": result.axis_results,
        "brand_results": result.brand_results,
        "repeat_results": result.repeat_results,
        "call_logs": [call_log_to_json(log) for log in result.call_logs],
        "call_plan": call_plan,
        "keyword_stats": keyword_stats(rows),
        "token_latency_by_model": _token_latency_by_model(result.call_logs),
        "nondeterminism": _nondeterminism(result.brand_results, result.repeat_results),
        "axis_overlap": _axis_overlap(result.axis_results),
    }


def _call_brand(
    token: str,
    model: ModelSpec,
    scope: ScopeSpec,
    brand: str,
    rows: list[KeywordRow],
    topics: list[TopicDefinition],
    *,
    task: str = "brand_share",
) -> tuple[dict[str, JsonValue], CallLog]:
    """Call one model for one brand-share payload and normalize the response."""
    input_hash = stable_input_hash(rows, prompt_version=PROMPT_VERSION, axis_version=scope.scope_id)
    payload, log = call_genos_json(
        token=token,
        spec=model,
        task=task,
        scope_id=scope.scope_id,
        brand=brand,
        messages=brand_share_prompt(scope_id=scope.scope_id, brand=brand, axis_version=scope.scope_id, topics=topics, rows=rows),
        rows=rows,
        input_hash=input_hash,
    )
    return normalize_share_payload(payload, brand=brand, scope_id=scope.scope_id, row_count=len(rows)), log


def _plan_row(task: str, model_key: str, serving_id: str, scope_id: str, brand: str, rows: list[KeywordRow], input_hash: str) -> dict[str, JsonValue]:
    """Build one sanitized planned-call row."""
    return {
        "task": task,
        "model_key": model_key,
        "serving_id": serving_id,
        "scope_id": scope_id,
        "brand": brand,
        "row_count": len(rows),
        "estimated_input_tokens": sum(estimate_tokens(row.keyword_text) for row in rows),
        "input_hash": input_hash,
        "cache_key": build_cache_key(task=task, model_key=model_key, prompt_version=PROMPT_VERSION, input_hash=input_hash),
    }


def _axis_payload(scope: ScopeSpec, payload: dict[str, JsonValue], topics: list[TopicDefinition]) -> dict[str, JsonValue]:
    """Store only topic-axis fields needed for review."""
    return {
        "status": str(payload.get("status") or "ok"),
        "scope_id": scope.scope_id,
        "axis_version": str(payload.get("axis_version") or scope.scope_id),
        "topics": [
            {"topic_id": topic.topic_id, "label": topic.label, "definition": topic.definition, "keywords": list(topic.keywords)}
            for topic in topics
        ],
        "etc": payload.get("etc") if isinstance(payload.get("etc"), dict) else {},
    }


def _token_latency_by_model(logs: list[CallLog]) -> dict[str, JsonValue]:
    """Aggregate usage and latency by model."""
    totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "latency_ms": 0}
    )
    for log in logs:
        item = totals[log.model_key]
        item["calls"] += 1
        item["prompt_tokens"] += log.prompt_tokens
        item["completion_tokens"] += log.completion_tokens
        item["total_tokens"] += log.total_tokens
        item["latency_ms"] += log.latency_ms
    return {
        model: {**item, "avg_latency_ms": round(item["latency_ms"] / item["calls"] if item["calls"] else 0)}
        for model, item in totals.items()
    }


def _nondeterminism(brand_results: dict[str, JsonValue], repeat_results: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Compare first and repeat outputs for the representative brand sample."""
    results: dict[str, JsonValue] = {}
    for model in MODEL_SPECS:
        first = _dict(_dict(brand_results.get(model.model_key)).get(REPRESENTATIVE_SAMPLE_KEY))
        second = _dict(repeat_results.get(model.model_key))
        results[model.model_key] = {
            "sample_key": REPRESENTATIVE_SAMPLE_KEY,
            "max_delta_pp": max_share_delta_pp(share_map(first), share_map(second)) if first and second else None,
            "status": "measured" if first and second else "not_measured",
        }
    return results


def _axis_overlap(axis_results: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Compare topic-axis label overlap between Pro/Flash/Lite by scope."""
    rows: dict[str, JsonValue] = {}
    for scope in SCOPE_SPECS:
        labels_by_model = {model.model_key: _axis_labels(axis_results, model.model_key, scope.scope_id) for model in MODEL_SPECS}
        rows[scope.scope_id] = {
            "pro_flash": topic_overlap_score(labels_by_model["pro"], labels_by_model["flash"]),
            "pro_lite": topic_overlap_score(labels_by_model["pro"], labels_by_model["lite"]),
            "flash_lite": topic_overlap_score(labels_by_model["flash"], labels_by_model["lite"]),
        }
    return rows


def _axis_labels(axis_results: dict[str, JsonValue], model_key: str, scope_id: str) -> list[str]:
    """Read labels from sanitized axis payloads."""
    topics = _dict(_dict(axis_results.get(model_key)).get(scope_id)).get("topics")
    if not isinstance(topics, list):
        return []
    return [str(_dict(topic).get("label") or "") for topic in topics]


def _repeat_hash(brand_samples: dict[str, list[KeywordRow]]) -> str:
    """Return the representative repeat input hash."""
    return stable_input_hash(brand_samples[REPRESENTATIVE_SAMPLE_KEY], prompt_version=PROMPT_VERSION, axis_version="group:livalo_family")


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    """Return a JSON dict or an empty dict."""
    return value if isinstance(value, dict) else {}
