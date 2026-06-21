#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "httpx2[http2,brotli,zstd]",
#     "pymysql",
#     "rich",
#     "typer",
# ]
# ///

# ─── How to run ───
# 1. Install uv (if not installed):
#      curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. Dry-run, no GenOS call:
#      uv run --script pipeline/scripts/analysis/brand_activity/llm_topic/run_llm_topic_poc.py
# 3. Real bounded GenOS calls:
#      GENOS_BEARER_TOKEN=... uv run --script pipeline/scripts/analysis/brand_activity/llm_topic/run_llm_topic_poc.py --execute
# ──────────────────
"""Run the analysis-only GenOS LLM topic PoC with redacted audit artifacts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Final

import typer
from rich import print as rprint

REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.scripts.analysis.brand_activity.llm_topic.audit import (  # noqa: E402
    AUDIT_ROOT,
    DOCS_DIR,
    create_zip_package,
    generated_files,
    raw_text_scan,
    write_git_status,
    write_json,
    write_manifest,
)
from pipeline.scripts.analysis.brand_activity.llm_topic.cache import build_cache_key, stable_input_hash  # noqa: E402
from pipeline.scripts.analysis.brand_activity.llm_topic.data_source import (  # noqa: E402
    ALIAS_PATH,
    DICTIONARY_PATH,
    BrandDescription,
    brand_stats,
    connect_mariadb,
    deterministic_sample,
    fetch_keyword_rows,
    fetch_snapshot,
    load_alias_descriptions,
    market_stats,
    read_env_file,
    rows_for_brand,
    rows_for_market,
    stratified_market_sample,
)
from pipeline.scripts.analysis.brand_activity.llm_topic.genos_client import GenosServingClient, parse_json_object  # noqa: E402
from pipeline.scripts.analysis.brand_activity.llm_topic.models import JsonValue, KeywordRow, TopicDefinition, TopicShare  # noqa: E402
from pipeline.scripts.analysis.brand_activity.llm_topic.privacy import estimate_tokens, redacted_rows_for_audit  # noqa: E402
from pipeline.scripts.analysis.brand_activity.llm_topic.prompts import (  # noqa: E402
    PROMPT_VERSION,
    brand_share_prompt,
    market_axis_prompt,
    prompt_template_manifest,
)
from pipeline.scripts.analysis.brand_activity.llm_topic.reports import render_comparison, render_design, render_poc_result  # noqa: E402
from pipeline.scripts.analysis.brand_activity.llm_topic.response import normalized_share_payload  # noqa: E402


SAMPLE_BRANDS: Final[dict[str, tuple[str, ...]]] = {
    "C10C0": ("LIVALOZET", "ATOZET", "ROSUVAMIBE", "ROSUZET"),
    "G04C2": ("THRUPAS", "HANMITAMS", "FLIVAS", "HARNAL-D"),
}
FLASH_SERVING_ID: Final = "76"
LITE_SERVING_ID: Final = "163"
GENOS_BASE_URL: Final = "https://jwai-dev.jwhealthcare.com"


def main(
    execute: bool = typer.Option(False, "--execute", help="Run bounded GenOS calls when a bearer token is available."),
    token_env: str = typer.Option("GENOS_BEARER_TOKEN", "--token-env", help="Environment variable containing the GenOS bearer token."),
    market_axis_per_brand: int = typer.Option(30, "--axis-per-brand", min=5, max=60),
    brand_rows_limit: int = typer.Option(35, "--brand-rows", min=5, max=80),
) -> None:
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    tag = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    audit_dir = AUDIT_ROOT / tag
    audit_dir.mkdir(parents=True, exist_ok=True)

    dictionary = _read_json(DICTIONARY_PATH)
    alias_payload = _read_json(ALIAS_PATH)
    env = read_env_file()
    connection = connect_mariadb(env)
    try:
        before_snapshot = fetch_snapshot(connection)
        rows = fetch_keyword_rows(connection, tuple(SAMPLE_BRANDS))
        after_snapshot = fetch_snapshot(connection)
    finally:
        connection.close()

    selected_brand_keys = [(market, brand) for market, brands in SAMPLE_BRANDS.items() for brand in brands]
    descriptions = load_alias_descriptions(alias_payload, selected_brand_keys)
    samples = _build_samples(rows, market_axis_per_brand, brand_rows_limit)
    call_plan = _build_call_plan(samples)
    token = os.environ.get(token_env, "")
    execution_status = "executed" if execute else "dry_run_not_executed"

    axis_results, brand_results, call_logs, nondeterminism = _run_or_plan_calls(
        execute=execute,
        token=token,
        dictionary=dictionary,
        samples=samples,
        descriptions=descriptions,
        call_plan=call_plan,
    )
    dictionary_baseline = _dictionary_baseline(dictionary, samples["brand_rows"])
    monthly_estimate = _monthly_estimate(call_logs, rows)

    write_json(audit_dir / "db_snapshot.json", {"before": before_snapshot, "after": after_snapshot, "read_only_equal": before_snapshot == after_snapshot})
    write_json(audit_dir / "market_stats_sample_scope.json", market_stats(rows))
    write_json(audit_dir / "brand_stats_sample_scope.json", brand_stats(rows))
    write_json(audit_dir / "input_rows_redacted.json", _redacted_samples(samples))
    write_json(audit_dir / "prompt_templates.json", prompt_template_manifest())
    write_json(audit_dir / "credential_check.json", {"token_env": token_env, "token_present": bool(token), "token_value_logged": False, "auth_mode": "bearer" if token else "no_auth_dev_gateway"})
    write_json(audit_dir / "call_plan.json", call_plan)
    write_json(audit_dir / "call_log_sanitized.json", call_logs)
    write_json(audit_dir / "axis_results_sanitized.json", axis_results)
    write_json(audit_dir / "brand_results_sanitized.json", brand_results)
    write_json(audit_dir / "nondeterminism.json", nondeterminism)
    write_json(audit_dir / "dictionary_baseline.json", dictionary_baseline)
    write_git_status(audit_dir / "git_status.txt")

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "LLM_TOPIC_01_DESIGN.md").write_text(render_design(generated_at, market_stats(rows), call_plan), encoding="utf-8")
    (DOCS_DIR / "LLM_TOPIC_02_POC_RESULT.md").write_text(
        render_poc_result(generated_at, execution_status, call_logs, axis_results, brand_results, nondeterminism, monthly_estimate),
        encoding="utf-8",
    )
    (DOCS_DIR / "LLM_TOPIC_03_COMPARISON.md").write_text(
        render_comparison(generated_at, dictionary_baseline, execution_status, brand_results),
        encoding="utf-8",
    )

    scan = raw_text_scan(generated_files(audit_dir), rows)
    write_json(audit_dir / "raw_text_scan.json", scan)
    manifest = write_manifest(audit_dir)
    zip_path, zip_sha, backup_path = create_zip_package(tag, audit_dir)
    summary: dict[str, JsonValue] = {
        "tag": tag,
        "generated_at": generated_at,
        "execution_status": execution_status,
        "sample_markets": list(SAMPLE_BRANDS),
        "sample_brand_count": len(selected_brand_keys),
        "planned_call_count": len(call_plan),
        "executed_call_count": sum(1 for item in call_logs if item.get("status") == "ok"),
        "raw_text_scan": scan["status"],
        "manifest_entries": len(manifest),
        "zip_path": str(zip_path),
        "zip_sha256": zip_sha,
        "permanent_backup_zip": str(backup_path),
        "open_questions": _open_questions(execution_status),
    }
    write_json(audit_dir / "run_summary.json", summary)
    rprint(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


def _build_samples(rows: Sequence[KeywordRow], market_axis_per_brand: int, brand_rows_limit: int) -> dict[str, dict[str, list[KeywordRow]]]:
    market_rows: dict[str, list[KeywordRow]] = {}
    brand_rows: dict[str, list[KeywordRow]] = {}
    for market, brands in SAMPLE_BRANDS.items():
        market_rows[market] = stratified_market_sample(rows_for_market(rows, market), brands=brands, per_brand=market_axis_per_brand, seed="axis")
        for brand in brands:
            key = f"{market}:{brand}"
            brand_rows[key] = deterministic_sample(rows_for_brand(rows, market, brand), limit=brand_rows_limit, seed="brand")
    return {"market_rows": market_rows, "brand_rows": brand_rows}


def _build_call_plan(samples: dict[str, dict[str, list[KeywordRow]]]) -> list[dict[str, JsonValue]]:
    plan: list[dict[str, JsonValue]] = []
    for market, rows in samples["market_rows"].items():
        input_hash = stable_input_hash(rows, prompt_version=PROMPT_VERSION, axis_version=f"{market}_axis_seed")
        plan.append(_plan_item("market_axis", FLASH_SERVING_ID, market, "", rows, input_hash))
    c10_rows = samples["market_rows"]["C10C0"]
    c10_hash = stable_input_hash(c10_rows, prompt_version=PROMPT_VERSION, axis_version="C10C0_axis_seed")
    plan.append(_plan_item("market_axis_lite_compare", LITE_SERVING_ID, "C10C0", "", c10_rows, c10_hash))
    for key, rows in samples["brand_rows"].items():
        market, brand = key.split(":", 1)
        input_hash = stable_input_hash(rows, prompt_version=PROMPT_VERSION, axis_version=f"{market}_axis")
        plan.append(_plan_item("brand_share", FLASH_SERVING_ID, market, brand, rows, input_hash))
    first_brand = samples["brand_rows"]["C10C0:LIVALOZET"]
    repeat_hash = stable_input_hash(first_brand, prompt_version=PROMPT_VERSION, axis_version="C10C0_axis")
    plan.append(_plan_item("brand_share_repeat_for_nondeterminism", FLASH_SERVING_ID, "C10C0", "LIVALOZET", first_brand, repeat_hash))
    return plan


def _plan_item(task: str, serving_id: str, market: str, brand: str, rows: Sequence[KeywordRow], input_hash: str) -> dict[str, JsonValue]:
    return {
        "task": task,
        "serving_id": serving_id,
        "atc4": market,
        "brand": brand,
        "sample_rows": len(rows),
        "estimated_input_tokens": sum(estimate_tokens(row.keyword_text) for row in rows),
        "input_hash": input_hash,
        "cache_key": build_cache_key(task=task, model_serving_id=serving_id, prompt_version=PROMPT_VERSION, input_hash=input_hash),
    }


def _run_or_plan_calls(
    *,
    execute: bool,
    token: str,
    dictionary: dict[str, JsonValue],
    samples: dict[str, dict[str, list[KeywordRow]]],
    descriptions: dict[str, BrandDescription],
    call_plan: list[dict[str, JsonValue]],
) -> tuple[dict[str, JsonValue], dict[str, JsonValue], list[dict[str, JsonValue]], dict[str, JsonValue]]:
    axis_results: dict[str, JsonValue] = {}
    brand_results: dict[str, JsonValue] = {}
    call_logs: list[dict[str, JsonValue]] = []
    fallback_axes = {market: _axis_from_seed(market, dictionary.get(market, {})) for market in SAMPLE_BRANDS}
    if not execute:
        for item in call_plan:
            call_logs.append({**item, "status": "skipped_missing_token_or_execute_false", "latency_ms": 0})
        for market, topics in fallback_axes.items():
            axis_results[f"{market}:seed_reference"] = {"status": "seed_reference_not_llm", "topics": [_topic_json(topic) for topic in topics]}
        return axis_results, brand_results, call_logs, {"status": "not_measured", "max_share_delta_pp": "n/a", "note": "GenOS call not executed."}

    client_flash = GenosServingClient(GENOS_BASE_URL, token, FLASH_SERVING_ID)
    client_lite = GenosServingClient(GENOS_BASE_URL, token, LITE_SERVING_ID)
    accepted_axes: dict[str, list[TopicDefinition]] = {}
    for market, rows in samples["market_rows"].items():
        messages = market_axis_prompt(atc4=market, seed_dictionary=dictionary.get(market, {}), rows=rows)
        result = _call_and_parse(client_flash, "market_axis", market, "", messages, rows)
        call_logs.append(result["log"])
        axis_results[market] = result["payload"]
        accepted_axes[market] = _topics_from_payload(result["payload"], fallback_axes[market])
    lite_messages = market_axis_prompt(atc4="C10C0", seed_dictionary=dictionary.get("C10C0", {}), rows=samples["market_rows"]["C10C0"])
    lite_result = _call_and_parse(client_lite, "market_axis_lite_compare", "C10C0", "", lite_messages, samples["market_rows"]["C10C0"])
    call_logs.append(lite_result["log"])
    axis_results["C10C0:lite_compare"] = lite_result["payload"]

    first_repeat_payload: dict[str, JsonValue] | None = None
    second_repeat_payload: dict[str, JsonValue] | None = None
    for key, rows in samples["brand_rows"].items():
        market, brand = key.split(":", 1)
        description = descriptions[key]
        topics = accepted_axes.get(market, fallback_axes[market])
        messages = brand_share_prompt(atc4=market, brand=brand, axis_version=f"{market}_axis", topics=topics, description=description, rows=rows)
        result = _call_and_parse(client_flash, "brand_share", market, brand, messages, rows)
        call_logs.append(result["log"])
        brand_results[key] = _normalize_brand_payload(result["payload"], brand, market, rows)
        if key == "C10C0:LIVALOZET":
            first_repeat_payload = brand_results[key] if isinstance(brand_results[key], dict) else None
            repeat = _call_and_parse(client_flash, "brand_share_repeat_for_nondeterminism", market, brand, messages, rows)
            call_logs.append(repeat["log"])
            second_repeat_payload = _normalize_brand_payload(repeat["payload"], brand, market, rows)
    return axis_results, brand_results, call_logs, _nondeterminism(first_repeat_payload, second_repeat_payload)


def _call_and_parse(
    client: GenosServingClient,
    task: str,
    market: str,
    brand: str,
    messages: list[dict[str, str]],
    rows: Sequence[KeywordRow],
) -> dict[str, dict[str, JsonValue]]:
    call = client.chat(messages)
    parsed = parse_json_object(call["content"])
    status = "ok" if call["status"] == "ok" and "_invalid" not in parsed else "quarantined_invalid_json" if call["status"] == "ok" else "error"
    raw_sha = hashlib.sha256(call["content"].encode("utf-8")).hexdigest() if call["content"] else ""
    log: dict[str, JsonValue] = {
        "task": task,
        "serving_id": call["serving_id"],
        "atc4": market,
        "brand": brand,
        "status": status,
        "latency_ms": call["latency_ms"],
        "estimated_input_tokens": sum(estimate_tokens(row.keyword_text) for row in rows),
        "usage": call["usage"],
        "raw_output_sha256": raw_sha,
        "raw_output_length": len(call["content"]),
        "error_type": call["error_type"],
        "error_message": call["error_message"],
    }
    payload = {**parsed, "status": status}
    return {"log": log, "payload": payload}


def _axis_from_seed(market: str, seed: JsonValue) -> list[TopicDefinition]:
    if not isinstance(seed, dict):
        return ()
    topics: list[TopicDefinition] = []
    for index, (label, payload) in enumerate(seed.items(), start=1):
        if len(topics) >= 8:
            break
        keywords = payload.get("keywords") if isinstance(payload, dict) else []
        topics.append(
            TopicDefinition(
                topic_id=f"T{index}",
                label=str(label),
                definition=f"{market} seed dictionary label",
                keywords=tuple(str(item) for item in keywords if isinstance(item, str))[:8],
            )
        )
    return topics


def _topics_from_payload(payload: JsonValue, fallback: Sequence[TopicDefinition]) -> list[TopicDefinition]:
    if not isinstance(payload, dict):
        return list(fallback)
    topics = payload.get("topics")
    if not isinstance(topics, list):
        return list(fallback)
    parsed: list[TopicDefinition] = []
    for index, item in enumerate(topics, start=1):
        if not isinstance(item, dict):
            continue
        keywords = item.get("keywords")
        parsed.append(
            TopicDefinition(
                topic_id=str(item.get("topic_id") or f"T{index}"),
                label=str(item.get("label") or item.get("label_name") or f"Topic {index}"),
                definition=str(item.get("definition") or ""),
                keywords=tuple(str(keyword) for keyword in keywords if isinstance(keyword, str)) if isinstance(keywords, list) else (),
            )
        )
    return parsed or list(fallback)


def _normalize_brand_payload(payload: JsonValue, brand: str, market: str, rows: Sequence[KeywordRow]) -> dict[str, JsonValue]:
    if not isinstance(payload, dict) or not isinstance(payload.get("topic_shares"), list):
        return {"status": "quarantined_invalid_schema", "brand": brand, "atc4": market}
    shares: list[TopicShare] = []
    for item in payload["topic_shares"]:
        if not isinstance(item, dict):
            continue
        shares.append(
            TopicShare(
                topic_id=str(item.get("topic_id") or ""),
                label=str(item.get("label") or ""),
                share_pct=float(item.get("share_pct") or 0.0),
                row_count=int(item.get("row_count") or 0),
            )
        )
    normalized = normalized_share_payload(
        brand=brand,
        atc4=market,
        axis_version=str(payload.get("axis_version") or f"{market}_axis"),
        row_count=len(rows),
        shares=shares,
        evidence_note=str(payload.get("evidence_note") or ""),
    )
    return {**normalized, "status": payload.get("status", "ok"), "cross_insights": payload.get("cross_insights", {})}


def _nondeterminism(first: dict[str, JsonValue] | None, second: dict[str, JsonValue] | None) -> dict[str, JsonValue]:
    if first is None or second is None:
        return {"status": "not_measured", "max_share_delta_pp": "n/a", "note": "Repeat call unavailable."}
    first_map = _share_map(first)
    second_map = _share_map(second)
    labels = set(first_map) | set(second_map)
    max_delta = max((abs(first_map.get(label, 0.0) - second_map.get(label, 0.0)) for label in labels), default=0.0)
    return {"status": "measured", "max_share_delta_pp": round(max_delta, 1), "note": "Same prompt/model/input called twice at temperature 0."}


def _share_map(payload: dict[str, JsonValue]) -> dict[str, float]:
    shares = payload.get("topic_shares")
    result: dict[str, float] = {}
    if not isinstance(shares, list):
        return result
    for item in shares:
        if isinstance(item, dict):
            result[str(item.get("topic_id") or item.get("label") or "")] = float(item.get("share_pct") or 0.0)
    return result


def _dictionary_baseline(dictionary: dict[str, JsonValue], brand_rows: dict[str, list[KeywordRow]]) -> list[dict[str, JsonValue]]:
    rows: list[dict[str, JsonValue]] = []
    for key, sample in sorted(brand_rows.items()):
        market, brand = key.split(":", 1)
        labels = dictionary.get(market, {})
        counter: Counter[str] = Counter()
        if isinstance(labels, dict):
            for row in sample:
                text = row.keyword_text.lower()
                for label, payload in labels.items():
                    keywords = payload.get("keywords") if isinstance(payload, dict) else []
                    if any(str(keyword).lower() in text for keyword in keywords if isinstance(keyword, str)):
                        counter[str(label)] += 1
        top = ", ".join(f"{label} {count}" for label, count in counter.most_common(4)) or "no dictionary hits"
        rows.append({"atc4": market, "brand": brand, "sample_rows": len(sample), "top_dictionary_labels": top})
    return rows


def _redacted_samples(samples: dict[str, dict[str, list[KeywordRow]]]) -> dict[str, JsonValue]:
    return {
        "market_rows": {market: redacted_rows_for_audit(rows) for market, rows in samples["market_rows"].items()},
        "brand_rows": {key: redacted_rows_for_audit(rows) for key, rows in samples["brand_rows"].items()},
    }


def _monthly_estimate(call_logs: list[dict[str, JsonValue]], rows: Sequence[KeywordRow]) -> dict[str, JsonValue]:
    sample_estimated = sum(int(item.get("estimated_input_tokens") or 0) for item in call_logs)
    measured_total = 0
    for item in call_logs:
        usage = item.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
            measured_total += int(usage["total_tokens"])
    avg_tokens = sample_estimated / max(1, len(call_logs))
    return {
        "sample_estimated_input_tokens": sample_estimated,
        "sample_measured_total_tokens": measured_total or "not_available",
        "monthly_calls": 119,
        "monthly_input_tokens": round(avg_tokens * 119),
        "cost_note": "Internal GenOS token tariff was not present in repo/env; cost = input/output tokens x approved tariff.",
        "sample_scope_rows": len(rows),
    }


def _topic_json(topic: TopicDefinition) -> dict[str, JsonValue]:
    return {"topic_id": topic.topic_id, "label": topic.label, "definition": topic.definition, "keywords": list(topic.keywords)}


def _read_json(path: Path) -> dict[str, JsonValue]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data
    return {}


def _open_questions(execution_status: str) -> int:
    base_questions = 2
    if execution_status != "executed":
        return base_questions + 1
    return base_questions


if __name__ == "__main__":
    typer.run(main)
