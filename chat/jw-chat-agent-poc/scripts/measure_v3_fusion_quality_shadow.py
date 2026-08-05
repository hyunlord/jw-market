# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Regenerate the 245-answer SHADOW corpus from stored C-3 evidence only."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, datetime
import argparse
import json
from pathlib import Path
import threading
import time

from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    ClinicalTrialFact,
    FileCellFact,
    MarketMetricFact,
    RegulatoryRuleFact,
    ToolDeferredRecord,
    ToolFailureRecord,
    V3EvidenceBundle,
    V3EvidenceFact,
    WebSourceFact,
)
from jw_chat_agent_poc.tool_use.v3_fusion import (
    FusionOutputTruncatedError,
    V3FusionEngine,
    build_fusion_messages,
)
from jw_chat_agent_poc.tool_use.v3_fusion_provider import GenosV3FusionProvider


_WRITE_LOCK = threading.Lock()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-fusion", type=Path, required=True)
    parser.add_argument("--source-web", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    indices = tuple(range(1, 246))
    failures = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_one, index, args): index for index in indices
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                result = future.result()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                failures += 1
                result = {
                    "index": index,
                    "status": "runner_error",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                }
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)

    complete = len(tuple(args.output.glob("*.json")))
    print(json.dumps({"complete": complete, "runner_failures": failures}, sort_keys=True))
    return 0 if complete == len(indices) and failures == 0 else 1


def run_one(index: int, args: argparse.Namespace) -> dict[str, object]:
    source = read_json(args.source_fusion / f"{index:03d}.json")
    web = read_json(args.source_web / f"{index:03d}.json")
    question = str(source["measurement"]["question"])
    bundle = build_bundle(source["execution_before_web"], web)
    messages = build_fusion_messages(question, bundle)
    provider = GenosV3FusionProvider.from_env()
    started = time.monotonic()
    try:
        generation = V3FusionEngine(provider).generate(question, bundle)
        fusion = {
            "status": "generated",
            "provider": provider_record(generation.provider),
            "generated_answer": generation.generated.model_dump(mode="json"),
            "validated_answer": generation.validated.model_dump(mode="json"),
        }
    except FusionOutputTruncatedError as exc:
        fusion = {
            "status": "typed_failure",
            "reason_code": exc.reason_code,
            "limitations": list(exc.limitations),
            "provider": provider_record(exc.provider),
            "partial_recovery_attempted": False,
        }
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        fusion = {
            "status": "error",
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:500],
        }
    row = {
        "index": index,
        "question": question,
        "source_execution_sha256": source.get("execution_before_web", {}).get("sha256"),
        "source_web_fact_count": len(web.get("facts", ())),
        "fusion_request": {
            "messages": messages,
            "message_chars": sum(len(message["content"]) for message in messages),
        },
        "fusion": fusion,
        "wall_ms": round((time.monotonic() - started) * 1000, 3),
        "guards": {
            "tool_execution_count": 0,
            "web_search_count": 0,
            "db_write_count": 0,
            "live_chat_call_count": 0,
            "serving_consumption": False,
        },
        "completed_at_utc": utc_now(),
    }
    write_json(args.output / f"{index:03d}.json", row)
    validated = fusion.get("validated_answer", {})
    answer = validated.get("answer", {}) if isinstance(validated, Mapping) else {}
    claims = answer.get("claims", ()) if isinstance(answer, Mapping) else ()
    return {"index": index, "status": fusion["status"], "accepted": len(claims)}


def build_bundle(execution: object, web: object) -> V3EvidenceBundle:
    if not isinstance(execution, Mapping) or not isinstance(web, Mapping):
        raise TypeError("stored evidence rows must be mappings")
    facts = tuple(fact_from_row(item) for item in mapping_rows(execution.get("facts")))
    web_facts = tuple(web_fact_from_row(item) for item in mapping_rows(web.get("facts")))
    return V3EvidenceBundle(
        status=str(execution["status"]),
        facts=facts + web_facts,
        failures=tuple(
            ToolFailureRecord(**dict(item))
            for item in mapping_rows(execution.get("failures"))
        ),
        deferred=tuple(
            ToolDeferredRecord(**dict(item))
            for item in mapping_rows(execution.get("deferred"))
        ),
        executions=(),
        original_call_count=int(execution["original_call_count"]),
        executed_call_count=int(execution["executed_call_count"]),
        deduplicated_call_count=int(execution["deduplicated_call_count"]),
    )


def fact_from_row(item: Mapping[str, object]) -> V3EvidenceFact:
    values = tuple_fields(item)
    if "entity" in values or "metric" in values:
        return MarketMetricFact(**values)
    if "effective_date" in values or "last_checked" in values:
        return RegulatoryRuleFact(**values)
    if "status" in values or "last_update_posted" in values:
        return ClinicalTrialFact(**values)
    if "file_id" in values or "sheet" in values or "range" in values:
        return FileCellFact(**values)
    raise ValueError(f"cannot infer evidence fact type: {sorted(values)}")


def web_fact_from_row(item: Mapping[str, object]) -> WebSourceFact:
    values = dict(item)
    values["missing_required_fields"] = tuple(values.get("missing_required_fields", ()))
    values["conflicts_with_evidence_ids"] = tuple(values.get("conflicts_with_evidence_ids", ()))
    return WebSourceFact(**values)


def tuple_fields(item: Mapping[str, object]) -> dict[str, object]:
    values = dict(item)
    for key in ("missing_required_fields", "projection_sources", "projection_missing_reasons"):
        values[key] = tuple(tuple(value) if isinstance(value, list) else value for value in values.get(key, ()))
    return values


def mapping_rows(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def provider_record(result: object) -> dict[str, object]:
    return {
        "completed_at_utc": getattr(result, "completed_at_utc"),
        "latency_ms": getattr(result, "latency_ms"),
        "model": getattr(result, "model"),
        "raw_bytes_sha256": getattr(result, "raw_bytes_sha256"),
        "raw_response": getattr(result, "raw_response"),
        "raw_text": getattr(result, "raw_text"),
        "request_body_sha256": getattr(result, "request_body_sha256"),
        "finish_reason": getattr(result, "finish_reason"),
        "usage": getattr(result, "usage"),
    }


def read_json(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise TypeError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, object]) -> None:
    with _WRITE_LOCK:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
