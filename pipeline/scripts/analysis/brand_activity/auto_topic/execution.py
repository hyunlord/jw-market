from __future__ import annotations

# noqa: SIZE_OK - Bounded GenOS execution coordinator keeps axis/share/quarantine sequencing auditable for this PoC.

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from .cache import build_cache_key, stable_input_hash
from .chunking import chunk_rows_by_token_budget, chunk_summary
from .dictionary import dictionary_baseline
from .llm import MODEL_SPECS, call_genos_json, call_log_to_json
from .label_rules import collapse_brand_specific_topics, single_concept_label
from .market_groups import source_scope_key_from_brand_sample_key
from .market_scope import scope_id
from .models import BrandDescription, CallLog, JsonValue, KeywordRow, TopicDefinition
from .privacy import estimate_tokens
from .prompts import PROMPT_VERSION, brand_share_prompt, brand_specific_axis_prompt, market_axis_merge_prompt, market_axis_prompt, market_seed_dictionary
from .qc_probe import artificial_qc_probe
from .quality import dictionary_cross_check, drift_check, mechanical_guard
from .quarantine import axis_failed, brand_axis_quarantine
from .response import axis_topic_label_map, brand_specific_topics_from_payload, normalize_axis_payload, normalize_share_payload, topics_from_axis
from .stability import axis_similarity, max_share_delta_pp, stabilize_axis


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Sanitized execution outputs used by reports and audit."""

    axis_results: dict[str, JsonValue]
    brand_results: dict[str, JsonValue]
    stability_results: dict[str, JsonValue]
    dictionary_results: dict[str, JsonValue]
    call_logs: list[CallLog]


def build_call_plan(
    *,
    markets: tuple[str, ...],
    axis_samples: dict[str, list[KeywordRow]],
    brand_samples: dict[str, list[KeywordRow]],
    brand_axis_samples: dict[str, list[KeywordRow]] | None = None,
    large_markets: tuple[str, ...],
    scope_metadata: dict[str, JsonValue] | None = None,
    axis_chunk_token_budget: int = 8000,
    brand_batch_token_budget: int = 8000,
) -> list[dict[str, JsonValue]]:
    """Create the bounded real-call plan before any GenOS request is made."""
    rows: list[dict[str, JsonValue]] = []
    metadata = _metadata(scope_metadata)
    for scope_key, sample_rows in axis_samples.items():
        rows.extend(_axis_plan_rows("market_axis", "flash", scope_key, sample_rows, f"{_scope_id(scope_key, metadata)}:new", metadata, token_budget=axis_chunk_token_budget))
    for sample_key, sample_rows in brand_samples.items():
        scope_key, atc4, brand = source_scope_key_from_brand_sample_key(sample_key)
        brand_axis_rows = (brand_axis_samples or {}).get(sample_key, [])
        if brand_axis_rows:
            rows.extend(_share_plan_rows("brand_specific_axis", "flash", scope_key, atc4, brand, brand_axis_rows, f"{_scope_id(scope_key, metadata)}:brand-specific", metadata, token_budget=brand_batch_token_budget))
        rows.extend(_share_plan_rows("brand_share", "flash", scope_key, atc4, brand, sample_rows, f"{_scope_id(scope_key, metadata)}:new", metadata, token_budget=brand_batch_token_budget))
    for scope_key in large_markets:
        sample_rows = axis_samples[scope_key]
        rows.extend(_axis_plan_rows("market_axis_tier_recheck", "lite", scope_key, sample_rows, f"{_scope_id(scope_key, metadata)}:tier", metadata, token_budget=axis_chunk_token_budget))
        first_brand_key = _first_brand_key_for_scope(brand_samples, scope_key)
        if first_brand_key:
            _source_scope, atc4, brand = source_scope_key_from_brand_sample_key(first_brand_key)
            brand_rows = brand_samples[first_brand_key]
            rows.extend(_share_plan_rows("brand_share_tier_recheck", "lite", scope_key, atc4, brand, brand_rows, f"{_scope_id(scope_key, metadata)}:tier", metadata, token_budget=brand_batch_token_budget))
    for scope_key in large_markets[:2]:
        rows.extend(_axis_plan_rows("market_axis_repeat_for_stability", "flash", scope_key, axis_samples[scope_key], f"{_scope_id(scope_key, metadata)}:repeat", metadata, token_budget=axis_chunk_token_budget))
    return rows


def execute_calls(
    *,
    token: str,
    dictionary: dict[str, JsonValue],
    axis_samples: dict[str, list[KeywordRow]],
    brand_samples: dict[str, list[KeywordRow]],
    brand_axis_samples: dict[str, list[KeywordRow]] | None = None,
    descriptions: dict[str, BrandDescription],
    markets: tuple[str, ...],
    large_markets: tuple[str, ...],
    scope_metadata: dict[str, JsonValue] | None = None,
    axis_chunk_token_budget: int = 8000,
    brand_batch_token_budget: int = 8000,
) -> ExecutionResult:
    """Run bounded Flash calls for all markets and Pro/Lite checks for large markets."""
    metadata = _metadata(scope_metadata)
    axis_results: dict[str, JsonValue] = {}
    brand_results: dict[str, JsonValue] = {}
    stability_results: dict[str, JsonValue] = {}
    dictionary_results = _dictionary_results(dictionary, brand_samples)
    call_logs: list[CallLog] = []
    axis_topics: dict[str, list[TopicDefinition]] = {}
    brand_specific_topics: dict[str, list[TopicDefinition]] = {}
    failed_scopes: set[str] = set()
    for scope_key, sample_rows in axis_samples.items():
        payload, logs = _call_axis_map_reduce(token, dictionary, scope_key, metadata, sample_rows, task="market_axis", model_key="flash", token_budget=axis_chunk_token_budget)
        call_logs.extend(logs)
        minimum_topics = _minimum_axis_topics(len(sample_rows))
        normalized = normalize_axis_payload(payload, scope_id=_scope_id(scope_key, metadata), fallback_label=_display_name(scope_key, metadata), minimum_topics=minimum_topics)
        normalized["scope_key"] = scope_key
        normalized["display_name"] = _display_name(scope_key, metadata)
        normalized["atc4_values"] = _scope_atc4_values(scope_key, metadata)
        normalized["source_row_count"] = len(sample_rows)
        normalized["chunking"] = payload.get("chunking") if isinstance(payload.get("chunking"), dict) else {}
        axis_results[scope_key] = normalized
        if axis_failed(normalized):
            failed_scopes.add(scope_key)
            continue
        axis_topics[scope_key] = topics_from_axis(normalized, fallback_label=_display_name(scope_key, metadata))
    for sample_key, sample_rows in brand_samples.items():
        scope_key, atc4, brand = source_scope_key_from_brand_sample_key(sample_key)
        if scope_key in failed_scopes:
            description = descriptions[f"{atc4}:{brand}"]
            brand_results[sample_key] = brand_axis_quarantine(atc4=atc4, brand=brand, scope_id=_scope_id(scope_key, metadata), axis_version=f"{_scope_id(scope_key, metadata)}:{PROMPT_VERSION}", row_count=len(sample_rows), axis_payload=axis_results[scope_key])
            brand_results[sample_key]["scope_key"] = scope_key
            brand_results[sample_key]["display_name"] = _display_name(scope_key, metadata)
            brand_results[sample_key].update(_brand_metadata(description))
            continue
        description = descriptions[f"{atc4}:{brand}"]
        generated_topics, logs = _call_brand_specific_axis_if_needed(token, scope_key, metadata, atc4, brand, (brand_axis_samples or {}).get(sample_key, []), description, axis_topics[scope_key], "flash")
        call_logs.extend(logs)
        brand_specific_topics[sample_key] = generated_topics
        normalized, logs = _call_share_batches(token, scope_key, metadata, atc4, brand, sample_rows, description, axis_topics[scope_key], generated_topics, "flash", task="brand_share", token_budget=brand_batch_token_budget)
        call_logs.extend(logs)
        valid_ids = {topic.topic_id for topic in axis_topics[scope_key]} | {topic.topic_id for topic in generated_topics}
        normalized["scope_key"] = scope_key
        normalized["display_name"] = _display_name(scope_key, metadata)
        normalized["atc4_values"] = _scope_atc4_values(scope_key, metadata)
        normalized.update(_brand_metadata(description))
        normalized["qc"] = {
            "guard": mechanical_guard(normalized, valid_topic_ids=valid_ids, brand_total_rows=int(normalized.get("row_count") or 0)),
            "drift": drift_check(normalized, None),
            "dict_xcheck": dictionary_cross_check(normalized, dictionary_results.get(sample_key, {})),
        }
        brand_results[sample_key] = normalized
    for scope_key in large_markets:
        if scope_key in failed_scopes:
            continue
        axis_results.update(_tier_axis_recheck(token, dictionary, scope_key, metadata, axis_samples[scope_key], call_logs, token_budget=axis_chunk_token_budget))
        first_brand_key = _first_brand_key_for_scope(brand_samples, scope_key)
        if first_brand_key:
            _source_scope, atc4, brand = source_scope_key_from_brand_sample_key(first_brand_key)
            brand_results.update(_tier_share_recheck(token, scope_key, metadata, atc4, brand, first_brand_key, brand_samples[first_brand_key], descriptions[f"{atc4}:{brand}"], axis_topics[scope_key], brand_specific_topics.get(first_brand_key, []), call_logs, token_budget=brand_batch_token_budget))
    for scope_key in large_markets[:2]:
        if scope_key in failed_scopes:
            continue
        repeat_payload, logs = _call_axis_map_reduce(token, dictionary, scope_key, metadata, axis_samples[scope_key], task="market_axis_repeat_for_stability", model_key="flash", token_budget=axis_chunk_token_budget)
        call_logs.extend(logs)
        repeat_axis = normalize_axis_payload(repeat_payload, scope_id=_scope_id(scope_key, metadata), fallback_label=_display_name(scope_key, metadata), minimum_topics=3)
        stability_results[scope_key] = {
            "repeat_similarity": axis_similarity(axis_results[scope_key], repeat_axis) if isinstance(axis_results[scope_key], dict) else None,
            "stabilized": stabilize_axis(axis_results[scope_key] if isinstance(axis_results[scope_key], dict) else None, repeat_axis, threshold=0.8),
        }
    stability_results["quality_gate_artificial_anomalies"] = artificial_qc_probe(axis_topics)
    return ExecutionResult(axis_results=axis_results, brand_results=brand_results, stability_results=stability_results, dictionary_results=dictionary_results, call_logs=call_logs)


def _minimum_axis_topics(source_row_count: int) -> int:
    """Keep a one-row scope usable without inventing a third unsupported axis."""
    if source_row_count == 1:
        return 2
    return 3 if source_row_count < 45 else 5


def skipped_execution(dictionary: dict[str, JsonValue], brand_samples: dict[str, list[KeywordRow]]) -> ExecutionResult:
    """Return a no-call execution result while still producing dictionary baselines."""
    return ExecutionResult(axis_results={}, brand_results={}, stability_results={"status": "not_measured_dry_run"}, dictionary_results=_dictionary_results(dictionary, brand_samples), call_logs=[])


def execution_summary(result: ExecutionResult, call_plan: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """Summarize sanitized execution output for reports."""
    return {
        "axis_results": result.axis_results,
        "brand_results": result.brand_results,
        "stability_results": result.stability_results,
        "dictionary_results": result.dictionary_results,
        "call_logs": [call_log_to_json(log) for log in result.call_logs],
        "call_plan": call_plan,
        "token_latency_by_model": _token_latency_by_model(result.call_logs),
        "tier_axis_similarity": _tier_axis_similarity(result.axis_results),
        "nondeterminism": _brand_share_repeat_summary(result.brand_results),
    }


def _call_axis(token: str, dictionary: dict[str, JsonValue], scope_key: str, scope_metadata: dict[str, dict[str, JsonValue]], rows: list[KeywordRow], *, task: str, model_key: str) -> tuple[dict[str, JsonValue], CallLog]:
    """Call one model for a market-axis payload."""
    spec = MODEL_SPECS[model_key]
    axis_version = f"{_scope_id(scope_key, scope_metadata)}:{PROMPT_VERSION}"
    atc4 = _scope_atc4(scope_key, scope_metadata)
    input_hash = stable_input_hash(rows, prompt_version=PROMPT_VERSION, axis_version=axis_version, extra={"task": task, "model": model_key, "scope": scope_key})
    return call_genos_json(
        token=token,
        spec=spec,
        task=task,
        scope_id=_scope_id(scope_key, scope_metadata),
        atc4=atc4,
        brand="*",
        messages=market_axis_prompt(atc4=atc4, rows=rows, seed_dictionary=_scope_seed_dictionary(scope_key, scope_metadata, dictionary), scope_id=_scope_id(scope_key, scope_metadata), market_name=_display_name(scope_key, scope_metadata), atc4_values=_scope_atc4_values(scope_key, scope_metadata)),
        rows=rows,
        input_hash=input_hash,
    )


def _call_share(
    token: str,
    scope_key: str,
    scope_metadata: dict[str, dict[str, JsonValue]],
    atc4: str,
    brand: str,
    rows: list[KeywordRow],
    description: BrandDescription,
    topics: list[TopicDefinition],
    brand_specific_topics: list[TopicDefinition],
    model_key: str,
    *,
    task: str,
) -> tuple[dict[str, JsonValue], CallLog]:
    """Call one model for a brand-share payload and normalize it."""
    spec = MODEL_SPECS[model_key]
    axis_version = f"{_scope_id(scope_key, scope_metadata)}:{PROMPT_VERSION}"
    all_topics = topics + brand_specific_topics
    input_hash = stable_input_hash(rows, prompt_version=PROMPT_VERSION, axis_version=axis_version, extra={"task": task, "model": model_key, "scope": scope_key, "topics": [topic.topic_id for topic in all_topics]})
    payload, log = call_genos_json(
        token=token,
        spec=spec,
        task=task,
        scope_id=_scope_id(scope_key, scope_metadata),
        atc4=atc4,
        brand=brand,
        messages=brand_share_prompt(atc4=atc4, brand=brand, axis_version=axis_version, topics=topics, brand_specific_topics=brand_specific_topics, description=description, rows=rows, scope_id=_scope_id(scope_key, scope_metadata), market_name=_display_name(scope_key, scope_metadata), atc4_values=_scope_atc4_values(scope_key, scope_metadata)),
        rows=rows,
        input_hash=input_hash,
    )
    return normalize_share_payload(payload, brand=brand, atc4=atc4, scope_id=_scope_id(scope_key, scope_metadata), axis_version=axis_version, row_count=len(rows), axis_topics=all_topics), log


def _call_brand_specific_axis_if_needed(
    token: str,
    scope_key: str,
    scope_metadata: dict[str, dict[str, JsonValue]],
    atc4: str,
    brand: str,
    rows: list[KeywordRow],
    description: BrandDescription,
    market_topics: list[TopicDefinition],
    model_key: str,
) -> tuple[list[TopicDefinition], list[CallLog]]:
    """Generate definition-only brand-specific topics for recent rows."""
    if not rows:
        return [], []
    topics, log = _call_brand_specific_axis(token, scope_key, scope_metadata, atc4, brand, rows, description, market_topics, model_key, task="brand_specific_axis")
    return topics, [log]


def _call_brand_specific_axis(
    token: str,
    scope_key: str,
    scope_metadata: dict[str, dict[str, JsonValue]],
    atc4: str,
    brand: str,
    rows: list[KeywordRow],
    description: BrandDescription,
    market_topics: list[TopicDefinition],
    model_key: str,
    *,
    task: str,
) -> tuple[list[TopicDefinition], CallLog]:
    """Call one model for definition-only brand-specific topics."""
    spec = MODEL_SPECS[model_key]
    axis_version = f"{_scope_id(scope_key, scope_metadata)}:{PROMPT_VERSION}"
    input_hash = stable_input_hash(rows, prompt_version=PROMPT_VERSION, axis_version=axis_version, extra={"task": task, "model": model_key, "scope": scope_key, "brand": brand, "market_topics": [topic.topic_id for topic in market_topics]})
    payload, log = call_genos_json(
        token=token,
        spec=spec,
        task=task,
        scope_id=_scope_id(scope_key, scope_metadata),
        atc4=atc4,
        brand=brand,
        messages=brand_specific_axis_prompt(atc4=atc4, brand=brand, rows=rows, description=description, market_topics=market_topics, scope_id=_scope_id(scope_key, scope_metadata), market_name=_display_name(scope_key, scope_metadata), atc4_values=_scope_atc4_values(scope_key, scope_metadata)),
        rows=rows,
        input_hash=input_hash,
    )
    return brand_specific_topics_from_payload(payload, fallback_label=f"{brand} 특화"), log


def _call_axis_map_reduce(
    token: str,
    dictionary: dict[str, JsonValue],
    scope_key: str,
    scope_metadata: dict[str, dict[str, JsonValue]],
    rows: list[KeywordRow],
    *,
    task: str,
    model_key: str,
    token_budget: int,
) -> tuple[dict[str, JsonValue], list[CallLog]]:
    """Extract chunk candidate axes and merge them into one raw-text-free axis."""
    chunks = chunk_rows_by_token_budget(rows, token_budget=token_budget)
    if len(chunks) <= 1:
        payload, log = _call_axis(token, dictionary, scope_key, scope_metadata, rows, task=task, model_key=model_key)
        payload["chunking"] = {"mode": "single_call", **chunk_summary(chunks or [rows], token_budget=token_budget)}
        return payload, [log]
    logs: list[CallLog] = []
    candidates: list[dict[str, JsonValue]] = []
    for index, chunk in enumerate(chunks, start=1):
        payload, log = _call_axis(token, dictionary, scope_key, scope_metadata, list(chunk), task=f"{task}_chunk", model_key=model_key)
        logs.append(log)
        normalized = normalize_axis_payload(payload, scope_id=_scope_id(scope_key, scope_metadata), fallback_label=_display_name(scope_key, scope_metadata), minimum_topics=3)
        if normalized.get("status") == "ok":
            candidates.append({"chunk_index": index, "row_count": len(chunk), "topics": normalized.get("topics", []), "axis_note": normalized.get("axis_note", "")})
    if not candidates:
        return {"status": "error", "reason": "all_axis_chunks_failed", "chunking": {"mode": "chunk_map_reduce", **chunk_summary(chunks, token_budget=token_budget), "successful_chunks": 0}}, logs
    payload, merge_log = _call_axis_merge(token, scope_key, scope_metadata, candidates, task=f"{task}_merge", model_key=model_key)
    logs.append(merge_log)
    payload["chunking"] = {"mode": "chunk_map_reduce", **chunk_summary(chunks, token_budget=token_budget), "successful_chunks": len(candidates)}
    return payload, logs


def _call_axis_merge(
    token: str,
    scope_key: str,
    scope_metadata: dict[str, dict[str, JsonValue]],
    candidate_axes: list[dict[str, JsonValue]],
    *,
    task: str,
    model_key: str,
) -> tuple[dict[str, JsonValue], CallLog]:
    """Call GenOS for the raw-text-free candidate-axis merge step."""
    spec = MODEL_SPECS[model_key]
    axis_version = f"{_scope_id(scope_key, scope_metadata)}:{PROMPT_VERSION}"
    atc4 = _scope_atc4(scope_key, scope_metadata)
    input_hash = stable_input_hash([], prompt_version=PROMPT_VERSION, axis_version=axis_version, extra={"task": task, "model": model_key, "scope": scope_key, "candidate_axes": candidate_axes})
    return call_genos_json(
        token=token,
        spec=spec,
        task=task,
        scope_id=_scope_id(scope_key, scope_metadata),
        atc4=atc4,
        brand="*",
        messages=market_axis_merge_prompt(atc4=atc4, scope_id=_scope_id(scope_key, scope_metadata), market_name=_display_name(scope_key, scope_metadata), atc4_values=_scope_atc4_values(scope_key, scope_metadata), candidate_axes=candidate_axes),
        rows=[],
        input_hash=input_hash,
    )


def _call_share_batches(
    token: str,
    scope_key: str,
    scope_metadata: dict[str, dict[str, JsonValue]],
    atc4: str,
    brand: str,
    rows: list[KeywordRow],
    description: BrandDescription,
    topics: list[TopicDefinition],
    brand_specific_topics: list[TopicDefinition],
    model_key: str,
    *,
    task: str,
    token_budget: int,
) -> tuple[dict[str, JsonValue], list[CallLog]]:
    """Classify full brand rows in bounded batches and aggregate topic counts."""
    batches = chunk_rows_by_token_budget(rows, token_budget=token_budget)
    logs: list[CallLog] = []
    normalized_batches: list[dict[str, JsonValue]] = []
    for index, batch in enumerate(batches, start=1):
        batch_logs: list[CallLog] = []
        normalized: dict[str, JsonValue] = {}
        for attempt in range(1, 3):
            normalized, log = _call_share(token, scope_key, scope_metadata, atc4, brand, list(batch), description, topics, brand_specific_topics, model_key, task=task if len(batches) == 1 else f"{task}_batch")
            batch_logs.append(log)
            if normalized.get("status") == "ok":
                break
            if attempt == 2:
                break
        logs.extend(batch_logs)
        normalized["batch_index"] = index
        normalized["batch_attempts"] = len(batch_logs)
        normalized_batches.append(normalized)
    if len(normalized_batches) == 1:
        normalized_batches[0]["batching"] = {"mode": "single_call", **chunk_summary(batches, token_budget=token_budget), "batch_attempts": int(normalized_batches[0].get("batch_attempts") or 1)}
        return normalized_batches[0], logs
    aggregated = _aggregate_share_batches(normalized_batches, brand=brand, atc4=atc4, scope_id=_scope_id(scope_key, scope_metadata), axis_version=f"{_scope_id(scope_key, scope_metadata)}:{PROMPT_VERSION}", topics=topics, brand_specific_topics=brand_specific_topics, token_budget=token_budget)
    return aggregated, logs


def _aggregate_share_batches(
    batches: list[dict[str, JsonValue]],
    *,
    brand: str,
    atc4: str,
    scope_id: str,
    axis_version: str,
    topics: list[TopicDefinition],
    brand_specific_topics: list[TopicDefinition] | None = None,
    token_budget: int,
) -> dict[str, JsonValue]:
    """Aggregate batch-level independent topic influence into full-brand percentages."""
    total_rows = sum(int(batch.get("row_count") or 0) for batch in batches)
    successful = [batch for batch in batches if batch.get("status") == "ok" and int(batch.get("row_count") or 0) > 0]
    topic_counts = {topic.topic_id: 0.0 for topic in topics}
    topic_labels = {topic.topic_id: topic.label for topic in topics}
    brand_counts: dict[str, float] = {}
    brand_labels = {topic.topic_id: topic.label for topic in brand_specific_topics or ()}
    empty_successes = 0
    backfill_count = sum(int(batch.get("topic_id_backfill_count") or 0) for batch in successful)
    unmatched_labels: list[str] = []
    for batch in successful:
        batch_topic_counts, batch_brand_counts, batch_backfills, batch_unmatched = _batch_distribution(batch, topics, brand_specific_topics or ())
        backfill_count += batch_backfills
        unmatched_labels.extend(batch_unmatched)
        if sum(batch_topic_counts.values()) + sum(batch_brand_counts.values()) <= 0.0:
            empty_successes += 1
            continue
        for topic_id, count in batch_topic_counts.items():
            topic_counts[topic_id] += count
        for label, count in batch_brand_counts.items():
            brand_counts[label] = brand_counts.get(label, 0.0) + count
            brand_labels.setdefault(label, label)
    failed_count = len(batches) - len(successful) + empty_successes
    if total_rows <= 0:
        return {"status": "quarantined_invalid_schema", "brand": brand, "atc4": atc4, "scope_id": scope_id, "axis_version": axis_version, "row_count": 0, "source_row_count": total_rows, "reason": "empty_successful_brand_batches", "topic_shares": [], "partial_failure": bool(failed_count)}
    shares = [
        {
            "topic_id": topic_id,
            "label": topic_labels[topic_id],
            "affected_row_count": int(min(round(count), total_rows)),
            "share_pct": round(min(round(count), total_rows) * 100.0 / total_rows, 1),
        }
        for topic_id, count in topic_counts.items()
        if count > 0.0
    ]
    brand_specific_candidates = [
        {
            "topic_id": topic_id,
            "label": brand_labels[topic_id],
            "affected_row_count": int(min(round(count), total_rows)),
            "share_pct": round(min(round(count), total_rows) * 100.0 / total_rows, 1),
            "source": "brand_specific",
        }
        for index, (topic_id, count) in enumerate(sorted(brand_counts.items(), key=lambda item: (-item[1], item[0])), start=1)
        if count > 0.0
    ]
    brand_specific, brand_dedup_log = collapse_brand_specific_topics(brand_specific_candidates)
    brand_specific = [_with_influence_pct(topic, total_rows) for topic in brand_specific]
    return {
        "status": "ok",
        "brand": brand,
        "atc4": atc4,
        "scope_id": scope_id,
        "axis_version": axis_version,
        "denominator": "brand_total_row_count",
        "row_count": total_rows,
        "source_row_count": total_rows,
        "topic_shares": shares,
        "brand_specific_topics": brand_specific,
        "brand_specific_dedup_count": len(brand_dedup_log),
        "brand_specific_dedup_log": brand_dedup_log,
        "topic_id_backfill_count": backfill_count,
        "unmatched_missing_topic_labels": sorted(set(unmatched_labels)),
        "cross_insights": _first_cross_insights(successful),
        "evidence_note": "Aggregated from successful bounded full-row batches without persisting raw messages.",
        "partial_failure": bool(failed_count),
        "batching": {
            "mode": "batch_aggregate",
            "batch_count": len(batches),
            "successful_batch_count": len(successful) - empty_successes,
            "failed_batch_count": failed_count,
            "token_budget": token_budget,
            "row_counts": [int(batch.get("row_count") or 0) for batch in batches],
            "batch_statuses": [str(batch.get("status") or "") for batch in batches],
            "batch_attempts": [int(batch.get("batch_attempts") or 1) for batch in batches],
            "topic_id_backfill_count": backfill_count,
            "brand_specific_dedup_count": len(brand_dedup_log),
        },
    }


def _batch_distribution(batch: dict[str, JsonValue], topics: Iterable[TopicDefinition], brand_specific_topics: Iterable[TopicDefinition] = ()) -> tuple[dict[str, float], dict[str, float], int, list[str]]:
    """Return market-topic and brand-topic affected row counts for one batch."""
    topic_list = list(topics)
    topic_counts = {topic.topic_id: 0.0 for topic in topic_list}
    label_map = axis_topic_label_map(topic_list)
    backfill_count = 0
    unmatched_labels: list[str] = []
    for share in _list(batch.get("topic_shares")):
        item = _dict(share)
        topic_id = str(item.get("topic_id") or "")
        if not topic_id:
            label = str(item.get("label") or "")
            topic_id = label_map.get("".join(label.split()).casefold(), "")
            if topic_id:
                backfill_count += 1
            elif label:
                unmatched_labels.append(label)
        if topic_id in topic_counts:
            affected = float(item.get("affected_row_count") or item.get("row_count") or 0.0)
            topic_counts[topic_id] += max(0.0, affected)
    brand_topic_list = list(brand_specific_topics)
    brand_topic_ids = {topic.topic_id for topic in brand_topic_list}
    brand_label_map = axis_topic_label_map(brand_topic_list)
    brand_counts: dict[str, float] = {}
    for share in _list(batch.get("brand_specific_topics")):
        item = _dict(share)
        topic_id = str(item.get("topic_id") or "")
        if brand_topic_ids and topic_id not in brand_topic_ids:
            label, _rewritten = single_concept_label(str(item.get("label") or ""))
            topic_id = brand_label_map.get("".join(label.split()).casefold(), "")
        elif not brand_topic_ids:
            label, _rewritten = single_concept_label(str(item.get("label") or topic_id))
            topic_id = label
        if not topic_id:
            continue
        affected = float(item.get("affected_row_count") or item.get("row_count") or 0.0)
        brand_counts[topic_id] = brand_counts.get(topic_id, 0.0) + max(0.0, affected)
    return topic_counts, brand_counts, backfill_count, unmatched_labels


def _with_influence_pct(topic: dict[str, JsonValue], total_rows: int) -> dict[str, JsonValue]:
    """Recalculate share_pct after label collapse merges affected rows."""
    affected = min(max(0, int(topic.get("affected_row_count") or 0)), total_rows) if total_rows > 0 else 0
    share_pct = round(affected * 100.0 / total_rows, 1) if total_rows > 0 else 0.0
    return {**topic, "affected_row_count": affected, "share_pct": share_pct}


def _first_cross_insights(batches: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """Return the first non-empty cross-insight object from batch results."""
    for batch in batches:
        value = batch.get("cross_insights")
        if isinstance(value, dict) and value:
            return value
    return {}


def _tier_axis_recheck(token: str, dictionary: dict[str, JsonValue], scope_key: str, scope_metadata: dict[str, dict[str, JsonValue]], rows: list[KeywordRow], call_logs: list[CallLog], *, token_budget: int) -> dict[str, JsonValue]:
    """Run the flash-lite axis recheck for one large market."""
    results: dict[str, JsonValue] = {}
    for model_key in ("lite",):
        payload, logs = _call_axis_map_reduce(token, dictionary, scope_key, scope_metadata, rows, task="market_axis_tier_recheck", model_key=model_key, token_budget=token_budget)
        call_logs.extend(logs)
        results[f"{scope_key}:{model_key}"] = normalize_axis_payload(payload, scope_id=_scope_id(scope_key, scope_metadata), fallback_label=_display_name(scope_key, scope_metadata), minimum_topics=3)
    return results


def _brand_metadata(description: BrandDescription) -> dict[str, JsonValue]:
    """Return non-sensitive source brand metadata stored with measured payloads."""
    return {
        "is_jw": description.is_jw,
        "kr_canonical": description.kr_canonical,
        "representing_company": list(description.representing_company),
    }


def _tier_share_recheck(
    token: str,
    scope_key: str,
    scope_metadata: dict[str, dict[str, JsonValue]],
    atc4: str,
    brand: str,
    sample_key: str,
    rows: list[KeywordRow],
    description: BrandDescription,
    topics: list[TopicDefinition],
    brand_specific_topics: list[TopicDefinition],
    call_logs: list[CallLog],
    *,
    token_budget: int,
) -> dict[str, JsonValue]:
    """Run the flash-lite brand-share recheck for the first sampled large-market brand."""
    results: dict[str, JsonValue] = {}
    for model_key in ("lite",):
        payload, logs = _call_share_batches(token, scope_key, scope_metadata, atc4, brand, rows, description, topics, brand_specific_topics, model_key, task="brand_share_tier_recheck", token_budget=token_budget)
        payload.update(_brand_metadata(description))
        call_logs.extend(logs)
        results[f"{sample_key}:{model_key}"] = payload
    return results


def _dictionary_results(dictionary: dict[str, JsonValue], brand_samples: dict[str, list[KeywordRow]]) -> dict[str, JsonValue]:
    """Create dictionary baselines for every sampled brand."""
    return {
        sample_key: dictionary_baseline(rows, market_seed_dictionary(source_scope_key_from_brand_sample_key(sample_key)[1], dictionary))
        for sample_key, rows in brand_samples.items()
    }


def _axis_plan_rows(task: str, model_key: str, scope_key: str, rows: list[KeywordRow], axis_version: str, scope_metadata: dict[str, dict[str, JsonValue]], *, token_budget: int) -> list[dict[str, JsonValue]]:
    """Build planned rows for a possibly chunked market-axis task."""
    chunks = chunk_rows_by_token_budget(rows, token_budget=token_budget)
    if len(chunks) <= 1:
        return [_plan_row(task, model_key, scope_key, _scope_atc4(scope_key, scope_metadata), "", rows, axis_version, scope_metadata, chunk_index=1, chunk_count=1, token_budget=token_budget)]
    planned = [
        _plan_row(f"{task}_chunk", model_key, scope_key, _scope_atc4(scope_key, scope_metadata), "", list(chunk), axis_version, scope_metadata, chunk_index=index, chunk_count=len(chunks), token_budget=token_budget)
        for index, chunk in enumerate(chunks, start=1)
    ]
    planned.append(_plan_row(f"{task}_merge", model_key, scope_key, _scope_atc4(scope_key, scope_metadata), "", [], axis_version, scope_metadata, chunk_index=0, chunk_count=len(chunks), token_budget=token_budget, estimated_input_tokens=max(1, len(chunks) * 320)))
    return planned


def _share_plan_rows(task: str, model_key: str, scope_key: str, atc4: str, brand: str, rows: list[KeywordRow], axis_version: str, scope_metadata: dict[str, dict[str, JsonValue]], *, token_budget: int) -> list[dict[str, JsonValue]]:
    """Build planned rows for a possibly batched brand-share task."""
    batches = chunk_rows_by_token_budget(rows, token_budget=token_budget)
    if len(batches) <= 1:
        return [_plan_row(task, model_key, scope_key, atc4, brand, rows, axis_version, scope_metadata, chunk_index=1, chunk_count=1, token_budget=token_budget)]
    return [
        _plan_row(f"{task}_batch", model_key, scope_key, atc4, brand, list(batch), axis_version, scope_metadata, chunk_index=index, chunk_count=len(batches), token_budget=token_budget)
        for index, batch in enumerate(batches, start=1)
    ]


def _plan_row(
    task: str,
    model_key: str,
    scope_key: str,
    atc4: str,
    brand: str,
    rows: list[KeywordRow],
    axis_version: str,
    scope_metadata: dict[str, dict[str, JsonValue]],
    *,
    chunk_index: int,
    chunk_count: int,
    token_budget: int,
    estimated_input_tokens: int | None = None,
) -> dict[str, JsonValue]:
    """Build one sanitized planned-call row."""
    spec = MODEL_SPECS[model_key]
    input_hash = stable_input_hash(rows, prompt_version=PROMPT_VERSION, axis_version=axis_version, extra={"task": task, "model": model_key, "scope": scope_key})
    return {
        "task": task,
        "model_key": model_key,
        "serving_id": spec.serving_id,
        "atc4": atc4,
        "scope_key": scope_key,
        "scope_id": _scope_id(scope_key, scope_metadata),
        "display_name": _display_name(scope_key, scope_metadata),
        "brand": brand or "*",
        "sample_rows": len(rows),
        "estimated_input_tokens": estimated_input_tokens if estimated_input_tokens is not None else sum(estimate_tokens(row.keyword_text) for row in rows),
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "token_budget": token_budget,
        "input_hash": input_hash,
        "cache_key": build_cache_key(task=task, model_key=model_key, serving_id=spec.serving_id, prompt_version=PROMPT_VERSION, axis_version=axis_version, input_hash=input_hash),
    }


def _metadata(value: dict[str, JsonValue] | None) -> dict[str, dict[str, JsonValue]]:
    """Return scope metadata with object rows only."""
    return {key: row for key, row in (value or {}).items() if isinstance(row, dict)}


def _scope_row(scope_key: str, scope_metadata: dict[str, dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """Return metadata for one scope key or an ATC4 fallback row."""
    return scope_metadata.get(scope_key, {"scope_id": scope_id(scope_key), "display_name": scope_key, "atc4_values": [scope_key]})


def _scope_id(scope_key: str, scope_metadata: dict[str, dict[str, JsonValue]]) -> str:
    """Return the stable LLM/audit scope id."""
    return str(_scope_row(scope_key, scope_metadata).get("scope_id") or scope_id(scope_key))


def _display_name(scope_key: str, scope_metadata: dict[str, dict[str, JsonValue]]) -> str:
    """Return the MI Master display name for one scope."""
    return str(_scope_row(scope_key, scope_metadata).get("display_name") or scope_key)


def _scope_atc4_values(scope_key: str, scope_metadata: dict[str, dict[str, JsonValue]]) -> list[str]:
    """Return source ATC4 values while preserving row-level source markets."""
    values = _scope_row(scope_key, scope_metadata).get("atc4_values")
    return [str(value) for value in values] if isinstance(values, list) else [scope_key]


def _scope_atc4(scope_key: str, scope_metadata: dict[str, dict[str, JsonValue]]) -> str:
    """Return the primary ATC4 marker used for seed fallback and logs."""
    return "+".join(_scope_atc4_values(scope_key, scope_metadata))


def _scope_seed_dictionary(scope_key: str, scope_metadata: dict[str, dict[str, JsonValue]], dictionary: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Merge REDESIGN seed dictionaries for single or grouped ATC4 scopes."""
    seed: dict[str, JsonValue] = {}
    for atc4 in _scope_atc4_values(scope_key, scope_metadata):
        for label, value in market_seed_dictionary(atc4, dictionary).items():
            seed[f"{atc4}:{label}"] = value
    return seed


def _first_brand_key_for_scope(brand_samples: dict[str, list[KeywordRow]], scope_key: str) -> str:
    """Return the first sampled brand key belonging to one final market scope."""
    for sample_key in brand_samples:
        source_scope, _atc4, _brand = source_scope_key_from_brand_sample_key(sample_key)
        if source_scope == scope_key:
            return sample_key
    return ""


def _token_latency_by_model(logs: list[CallLog]) -> dict[str, JsonValue]:
    """Aggregate measured usage and latency by model."""
    totals: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "latency_ms": 0})
    for log in logs:
        item = totals[log.model_key]
        item["calls"] += 1
        item["prompt_tokens"] += log.prompt_tokens
        item["completion_tokens"] += log.completion_tokens
        item["total_tokens"] += log.total_tokens
        item["latency_ms"] += log.latency_ms
    return {model: {**item, "avg_latency_ms": round(item["latency_ms"] / item["calls"] if item["calls"] else 0)} for model, item in totals.items()}


def _tier_axis_similarity(axis_results: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Compare large-market Pro/Lite axes with Flash axes."""
    result: dict[str, JsonValue] = {}
    for key, axis in axis_results.items():
        if key.endswith(":pro") or key.endswith(":lite"):
            base_key, model_key = key.rsplit(":", 1)
            base = axis_results.get(base_key)
            if isinstance(base, dict) and isinstance(axis, dict):
                result[key] = {"vs_flash_similarity": axis_similarity(base, axis), "model_key": model_key}
    return result


def _brand_share_repeat_summary(brand_results: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Summarize tier share differences as a repeatability proxy where measured."""
    summary: dict[str, JsonValue] = {}
    for key, payload in brand_results.items():
        if key.endswith(":pro"):
            base_key = key.removesuffix(":pro")
            lite_key = f"{base_key}:lite"
            if isinstance(payload, dict) and isinstance(brand_results.get(lite_key), dict):
                summary[base_key] = {"pro_lite_max_delta_pp": max_share_delta_pp(payload, brand_results[lite_key])}
    return summary


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    """Return a JSON object or an empty object."""
    return value if isinstance(value, dict) else {}


def _list(value: JsonValue) -> list[JsonValue]:
    """Return a JSON array or an empty array."""
    return value if isinstance(value, list) else []
