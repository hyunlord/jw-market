# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Measure the V3 SHADOW pipeline without serving or write-side effects.

Run inside an environment that already has the chat runtime configuration:

    PYTHONPATH=. python3 scripts/measure_v3_latency_shadow.py \
      --output /tmp/v3-latency --step evidence-projection
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
import argparse
import hashlib
import json
import re
from pathlib import Path
import threading
import time
from typing import Any

from jw_chat_agent_poc.agent_loop.factory import build_agent_loop_dependencies
from jw_chat_agent_poc.tool_use.v3_execution_factory import (
    build_default_shadow_executor,
)
from jw_chat_agent_poc.tool_use.v3_fusion import (
    FusionOutputTruncatedError,
    V3FusionEngine,
    build_fusion_messages,
)
from jw_chat_agent_poc.tool_use.v3_fusion_provider import GenosV3FusionProvider
from jw_chat_agent_poc.tool_use.v3_selection import V3ToolSelector
from jw_chat_agent_poc.tool_use.v3_selection_provider import GenosV3ToolChoiceProvider
from jw_chat_agent_poc.tool_use.v3_web_augmentation import V3WebAugmenter, WebSearchResult
import jw_chat_agent_poc.tool_use.v3_selection_provider as selection_provider_module


QUESTIONS = {
    78: "아일리아 매출 알려줘",
    223: "아일리아 시장 HHI",
    170: "리바로 시장 경쟁 구도가 최근 어떻게 변하고 있어?",
    64: "리바로, 리바로젯, 로수젯, 리피토 네 브랜드 순위를 비교해줘",
    40: "리바로 시장 경쟁사 영업활동 변화 있어?",
    114: "리바로 원인분석 좀 뽑아줘",
    77: "아일리아 급여기준 알려줘",
    51: "리바로 왜 이렇게 됐어?",
    95: "존재하지않는브랜드XYZ987654 매출 알려줘",
    86: "아일리아 최근 4개 분기 매출 알려줘",
}
_READ_SQL = frozenset({"SELECT", "WITH", "SHOW", "DESCRIBE", "EXPLAIN"})
_COMMENT_PREFIX = re.compile(r"^(?:\s|/\*.*?\*/|--[^\n]*\n|#[^\n]*\n)+", re.DOTALL)


class DbWriteGuard:
    """Fail closed if any pymysql caller attempts a write."""

    def __init__(self) -> None:
        self.read_statements = 0
        self.blocked_statements: list[str] = []
        self._lock = threading.Lock()

    def install(self) -> None:
        import pymysql.cursors

        original_execute = pymysql.cursors.Cursor.execute
        original_executemany = pymysql.cursors.Cursor.executemany
        guard = self

        def guarded_execute(cursor: object, query: object, args: object = None) -> object:
            verb = guard._verb(query)
            if (
                verb not in _READ_SQL
                and not guard._read_only_transaction(query)
                and not guard._read_only_statement(query)
            ):
                with guard._lock:
                    guard.blocked_statements.append(verb or "UNKNOWN")
                raise RuntimeError(f"DB_WRITE_BLOCKED:{verb or 'UNKNOWN'}")
            with guard._lock:
                guard.read_statements += 1
            return original_execute(cursor, query, args)

        def guarded_executemany(cursor: object, query: object, args: object) -> object:
            del cursor, args
            verb = guard._verb(query)
            with guard._lock:
                guard.blocked_statements.append(f"EXECUTEMANY:{verb or 'UNKNOWN'}")
            raise RuntimeError(f"DB_WRITE_BLOCKED:EXECUTEMANY:{verb or 'UNKNOWN'}")

        pymysql.cursors.Cursor.execute = guarded_execute
        pymysql.cursors.Cursor.executemany = guarded_executemany

    @staticmethod
    def _verb(query: object) -> str:
        text = query.decode("utf-8", errors="replace") if isinstance(query, bytes) else str(query)
        stripped = _COMMENT_PREFIX.sub("", text).lstrip()
        return stripped.split(None, 1)[0].upper() if stripped else ""

    @staticmethod
    def _read_only_transaction(query: object) -> bool:
        text = str(query).upper()
        return text.lstrip().startswith("START TRANSACTION") and "READ ONLY" in text

    @staticmethod
    def _read_only_statement(query: object) -> bool:
        text = " ".join(str(query).upper().split())
        return (
            text.startswith("SET NAMES ")
            or text.startswith("SET CHARACTER SET ")
            or text == "SET TRANSACTION READ ONLY"
            or text == "SET SESSION TRANSACTION READ ONLY"
        )


class SelectionCapture:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate
        self.request: object = None
        self.response_text = ""
        self.response_json: object = None
        self.http_status: int | None = None
        self.latency_ms = 0.0

    def __call__(self, url: str, **kwargs: object) -> object:
        self.request = normalize(kwargs.get("json"))
        started = time.monotonic()
        response = self.delegate(url, **kwargs)
        self.latency_ms = (time.monotonic() - started) * 1000
        self.http_status = int(getattr(response, "status_code", 0))
        content = bytes(getattr(response, "content", b""))
        self.response_text = content.decode("utf-8", errors="replace")
        try:
            self.response_json = response.json()
        except ValueError:
            self.response_json = None
        return response


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--step", required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--indices", default="")
    parser.add_argument("--prewarm", action="store_true")
    args = parser.parse_args()

    selected_indices = {
        int(value) for value in args.indices.split(",") if value.strip()
    }
    questions = {
        index: question
        for index, question in QUESTIONS.items()
        if not selected_indices or index in selected_indices
    }
    output = args.output / "latency" / args.step
    output.mkdir(parents=True, exist_ok=True)

    guard = DbWriteGuard()
    guard.install()
    dependencies = build_agent_loop_dependencies(external_mode="live")
    if dependencies.query_layer is None:
        raise RuntimeError("read-only query layer unavailable")
    selection_provider = GenosV3ToolChoiceProvider.from_env()
    selector = V3ToolSelector(provider=selection_provider, max_calls=8)
    fusion_provider = GenosV3FusionProvider.from_env()
    strategic_membership_count = None
    strategic_warmup_ms = 0.0
    membership_cache = None
    warmup_ms = 0.0
    if args.prewarm:
        from jw_chat_agent_poc.tool_use.v3_execution_factory import (
            prewarm_default_shadow_dependencies,
        )

        strategic_warmup_started = time.monotonic()
        strategic_membership_count = len(dependencies.query_layer.brand_memberships())
        strategic_warmup_ms = elapsed_ms(strategic_warmup_started)
        warmup_started = time.monotonic()
        membership_cache = prewarm_default_shadow_dependencies()
        warmup_ms = elapsed_ms(warmup_started)
    write_json(
        args.output / f"conditions_{args.step}.json",
        {
            "started_at_utc": utc_now(),
            "step": args.step,
            "rounds": args.rounds,
            "interval_seconds": args.interval,
            "question_indices": list(questions),
            "interleaved_rounds": True,
            "cache_bypass_requested": False,
            "prewarm_enabled": args.prewarm,
            "membership_warmup": {
                "wall_ms": round(warmup_ms, 3),
                "observability": membership_cache,
            },
            "strategic_mart_warmup": {
                "wall_ms": round(strategic_warmup_ms, 3),
                "membership_count": strategic_membership_count,
            },
            "selection": {
                "temperature": 0,
                "max_tokens": selection_provider.max_tokens,
                "candidate_count": 33,
            },
            "fusion": {
                "temperature": fusion_provider.temperature,
                "max_tokens": fusion_provider.max_tokens,
                "stream": False,
            },
            "live_chat_call_count": 0,
            "serving_consumption": False,
            "general_composite_execution_allowed": False,
        },
    )

    completed = 0
    for run_number in range(1, args.rounds + 1):
        for index, question in questions.items():
            path = output / f"{index:03d}_run{run_number}.json"
            if path.exists():
                continue
            record = run_one(
                index=index,
                run_number=run_number,
                question=question,
                selector=selector,
                fusion_provider=fusion_provider,
                web_client=dependencies.external,
                guard=guard,
            )
            write_json(path, record)
            completed += 1
            print(json.dumps(summary(record), ensure_ascii=False, sort_keys=True), flush=True)
            if index == 95 and accepted_claims(record):
                write_json(args.output / "hard_stop.json", {"reason": "fake_brand_answered", "record": summary(record)})
                return 95
            if guard.blocked_statements:
                write_json(args.output / "hard_stop.json", {"reason": "db_write_attempt", "verbs": guard.blocked_statements})
                return 96
            time.sleep(args.interval)

    write_json(
        args.output / f"runtime_guard_{args.step}.json",
        {
            "completed_at_utc": utc_now(),
            "new_runs": completed,
            "raw_file_count": len(tuple(output.glob("*_run*.json"))),
            "db_read_statements": guard.read_statements,
            "db_write_attempts": len(guard.blocked_statements),
            "cache_bypass_requested": False,
            "live_chat_call_count": 0,
            "serving_consumption": False,
        },
    )
    return 0


def run_one(
    *,
    index: int,
    run_number: int,
    question: str,
    selector: V3ToolSelector,
    fusion_provider: GenosV3FusionProvider,
    web_client: object,
    guard: DbWriteGuard,
) -> dict[str, object]:
    started_at = utc_now()
    started = time.monotonic()

    selection_started = time.monotonic()
    capture = SelectionCapture(selection_provider_module.requests.post)
    selection_provider_module.requests.post = capture
    selection = None
    selection_error = None
    try:
        selection = selector.select(question)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        selection_error = error_record(exc)
    finally:
        selection_provider_module.requests.post = capture.delegate
    selection_ms = elapsed_ms(selection_started)
    choices = tuple(selection.choices) if selection is not None else ()

    execution_started = time.monotonic()
    bundle = build_default_shadow_executor(question).execute(choices)
    execution_ms = elapsed_ms(execution_started)

    web_calls: list[dict[str, object]] = []
    augmenter = V3WebAugmenter(
        search=lambda query, **kwargs: web_search(web_client, web_calls, query, **kwargs)
    )
    web_started = time.monotonic()
    augmented = augmenter.augment(question, bundle)
    web_ms = elapsed_ms(web_started)

    messages = build_fusion_messages(question, augmented.bundle)
    fusion_started = time.monotonic()
    try:
        generated = V3FusionEngine(fusion_provider).generate(question, augmented.bundle)
        fusion = {
            "status": "generated",
            "provider": provider_record(generated.provider),
            "generated_answer": generated.generated.model_dump(mode="json"),
            "validated_answer": generated.validated.model_dump(mode="json"),
        }
    except FusionOutputTruncatedError as exc:
        fusion = {
            "status": "typed_failure",
            "reason_code": exc.reason_code,
            "limitations": list(exc.limitations),
            "provider": provider_record(exc.provider),
            "partial_recovery_attempted": False,
        }
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        fusion = {"status": "error", "error": error_record(exc)}
    fusion_ms = elapsed_ms(fusion_started)

    choice_rows = [
        {"name": choice.name, "arguments": normalize(choice.arguments), "call_id": choice.call_id}
        for choice in choices
    ]
    fact_rows = [normalize(asdict(fact)) for fact in augmented.bundle.facts]
    return {
        "measurement": {
            "index": index,
            "run": run_number,
            "question": question,
            "started_at_utc": started_at,
            "completed_at_utc": utc_now(),
            "end_to_end_latency_ms": round((time.monotonic() - started) * 1000, 3),
        },
        "selection": {
            "status": "selected" if selection is not None else "error",
            "error": selection_error,
            "choices": choice_rows,
            "candidate_names": list(selection.candidate_names) if selection is not None else [],
            "provider_choice_count": selection.provider_choice_count if selection is not None else None,
            "unknown_tool_names": list(selection.unknown_tool_names) if selection is not None else [],
            "request": capture.request,
            "raw_response_text": capture.response_text,
            "raw_response_json": capture.response_json,
            "http_status": capture.http_status,
            "provider_latency_ms": round(capture.latency_ms, 3),
        },
        "execution": bundle_record(bundle),
        "web": {
            "eligibility": asdict(augmented.eligibility),
            "expanded_to_general": augmented.expanded_to_general,
            "search_log": [asdict(item) for item in augmented.search_log],
            "calls": web_calls,
            "facts": [row for row in fact_rows if row.get("fact_type") == "web_source"],
        },
        "fusion_request": {
            "messages": messages,
            "message_chars": sum(len(str(message.get("content") or "")) for message in messages),
        },
        "fusion": fusion,
        "hashes": {
            "selected_tools_hash": stable_hash(choice_rows),
            "evidence_bundle_hash": stable_hash(fact_rows),
            "answer_claims_hash": stable_hash(accepted_claims_from_fusion(fusion)),
        },
        "latency": {
            "selection_ms": round(selection_ms, 3),
            "execution_ms": round(execution_ms, 3),
            "web_ms": round(web_ms, 3),
            "fusion_ms": round(fusion_ms, 3),
        },
        "guards": {
            "db_read_statements_cumulative": guard.read_statements,
            "db_write_attempts_cumulative": len(guard.blocked_statements),
            "cache_bypass_requested": False,
            "live_chat_call_count": 0,
            "serving_consumption": False,
            "general_composite_execution_allowed": False,
        },
    }


def web_search(client: object, calls: list[dict[str, object]], query: str, *, topic: str) -> WebSearchResult:
    started_at = utc_now()
    call = client.web_search(query, max_results=5, topic=topic)
    items = call.render_data.get("items")
    safe_items = (
        tuple(item for item in items if isinstance(item, Mapping))
        if isinstance(items, Sequence) and not isinstance(items, (str, bytes, bytearray))
        else ()
    )
    calls.append(
        {
            "started_at_utc": started_at,
            "completed_at_utc": utc_now(),
            "provider": call.source,
            "query": query,
            "topic": topic,
            "status": call.status,
            "latency_ms": call.elapsed_ms,
            "items": normalize(safe_items),
        }
    )
    return WebSearchResult(
        provider=call.source,
        query=query,
        items=safe_items,
        latency_ms=float(call.elapsed_ms or 0.0),
        status=call.status,
        error=None if call.status in {"ok", "fixture"} else call.summary_text,
    )


def bundle_record(bundle: object) -> dict[str, object]:
    return {
        "status": getattr(bundle, "status"),
        "facts": [normalize(asdict(fact)) for fact in getattr(bundle, "facts")],
        "failures": [normalize(asdict(item)) for item in getattr(bundle, "failures")],
        "deferred": [normalize(asdict(item)) for item in getattr(bundle, "deferred")],
        "executions": [normalize(asdict(item)) for item in getattr(bundle, "executions")],
        "original_call_count": getattr(bundle, "original_call_count"),
        "executed_call_count": getattr(bundle, "executed_call_count"),
        "deduplicated_call_count": getattr(bundle, "deduplicated_call_count"),
    }


def provider_record(result: object) -> dict[str, object]:
    return {
        "completed_at_utc": getattr(result, "completed_at_utc"),
        "latency_ms": getattr(result, "latency_ms"),
        "model": getattr(result, "model"),
        "raw_bytes_sha256": getattr(result, "raw_bytes_sha256"),
        "raw_response": normalize(getattr(result, "raw_response")),
        "raw_text": getattr(result, "raw_text"),
        "request_body_sha256": getattr(result, "request_body_sha256"),
        "finish_reason": getattr(result, "finish_reason"),
        "usage": normalize(getattr(result, "usage")),
    }


def accepted_claims_from_fusion(fusion: Mapping[str, object]) -> list[object]:
    validated = fusion.get("validated_answer")
    if not isinstance(validated, Mapping):
        return []
    answer = validated.get("answer")
    if not isinstance(answer, Mapping):
        return []
    claims = answer.get("claims")
    return list(claims) if isinstance(claims, Sequence) else []


def accepted_claims(record: Mapping[str, object]) -> list[object]:
    fusion = record.get("fusion")
    return accepted_claims_from_fusion(fusion) if isinstance(fusion, Mapping) else []


def summary(record: Mapping[str, object]) -> dict[str, object]:
    measurement = record["measurement"]
    fusion = record["fusion"]
    return {
        "at_utc": measurement["completed_at_utc"],
        "index": measurement["index"],
        "run": measurement["run"],
        "selected_count": len(record["selection"]["choices"]),
        "execution_status": record["execution"]["status"],
        "web_call_count": len(record["web"]["calls"]),
        "fusion_status": fusion["status"],
        "accepted_claim_count": len(accepted_claims(record)),
        "end_to_end_latency_ms": measurement["end_to_end_latency_ms"],
    }


def normalize(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return normalize(asdict(value))
    if hasattr(value, "model_dump"):
        return normalize(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): normalize(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [normalize(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def stable_hash(value: object) -> str:
    encoded = json.dumps(normalize(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def error_record(exc: BaseException) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc)[:500]}


def elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1000


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
