from __future__ import annotations

import hashlib

from pipeline.scripts.analysis.brand_activity.llm_topic.genos_client import GenosServingClient

from .models import CallLog, JsonValue, KeywordRow, ModelSpec
from .privacy import estimate_tokens
from .response import parse_model_json


GENOS_BASE_URL = "https://jwai-dev.jwhealthcare.com"
MODEL_SPECS = (
    ModelSpec("pro", "145", "GenOS Pro / serving 145"),
    ModelSpec("flash", "76", "GenOS Flash / serving 76"),
    ModelSpec("lite", "163", "GenOS Lite / serving 163"),
)


def call_genos_json(
    *,
    token: str,
    spec: ModelSpec,
    task: str,
    scope_id: str,
    brand: str,
    messages: list[dict[str, str]],
    rows: list[KeywordRow],
    input_hash: str,
) -> tuple[dict[str, JsonValue], CallLog]:
    """Call one GenOS model and return parsed payload plus sanitized call log."""
    client = GenosServingClient(GENOS_BASE_URL, token, spec.serving_id, timeout_s=120.0)
    call = client.chat(messages)
    payload = parse_model_json(call["content"])
    status = "ok" if call["status"] == "ok" and "_invalid" not in payload else "quarantined_invalid_json" if call["status"] == "ok" else "error"
    payload["status"] = status
    usage = call["usage"]
    log = CallLog(
        task=task,
        model_key=spec.model_key,
        serving_id=spec.serving_id,
        scope_id=scope_id,
        brand=brand,
        status=status,
        latency_ms=call["latency_ms"],
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        estimated_input_tokens=sum(estimate_tokens(row.keyword_text) for row in rows),
        input_hash=input_hash,
        output_sha256=hashlib.sha256(call["content"].encode("utf-8")).hexdigest() if call["content"] else "",
        output_length=len(call["content"]),
    )
    return payload, log


def call_log_to_json(log: CallLog) -> dict[str, JsonValue]:
    """Serialize a call log without raw request or response text."""
    return {
        "task": log.task,
        "model_key": log.model_key,
        "serving_id": log.serving_id,
        "scope_id": log.scope_id,
        "brand": log.brand,
        "status": log.status,
        "latency_ms": log.latency_ms,
        "usage": {
            "prompt_tokens": log.prompt_tokens,
            "completion_tokens": log.completion_tokens,
            "total_tokens": log.total_tokens,
        },
        "estimated_input_tokens": log.estimated_input_tokens,
        "input_hash": log.input_hash,
        "raw_output_sha256": log.output_sha256,
        "raw_output_length": log.output_length,
    }
