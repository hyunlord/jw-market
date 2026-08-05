# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any

from jw_chat_agent_poc.agent_loop.factory import build_agent_loop_dependencies
from jw_chat_agent_poc.tools.external.client import ExternalApiClient
from jw_chat_agent_poc.tool_use.v3_execution_factory import build_default_shadow_executor
from jw_chat_agent_poc.tool_use.v3_fusion import (
    FusionOutputTruncatedError,
    V3FusionEngine,
    build_fusion_messages,
)
from jw_chat_agent_poc.tool_use.v3_fusion_provider import (
    DEFAULT_FUSION_MAX_TOKENS,
    GenosV3FusionProvider,
)
from jw_chat_agent_poc.tool_use.v3_selection import V3ToolSelector
from jw_chat_agent_poc.tool_use.v3_selection_provider import GenosV3ToolChoiceProvider
from jw_chat_agent_poc.tool_use.v3_web_augmentation import (
    V3WebAugmenter,
    WebSearchResult,
)
import jw_chat_agent_poc.tool_use.v3_selection_provider as selection_provider_module


NONDETERMINISM_QUESTIONS = {
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
TARGETED_QUESTIONS = {
    103: "엔커버 시장에 하모닐란도 포함되는 이유가 뭐야?",
}
RECOVERY_FLOOR = datetime.fromisoformat("2026-08-05T10:56:03+00:00")
CORPUS_ROOT = Path(
    os.environ.get(
        "REMEASURE_CORPUS_ROOT",
        "/tmp/v3_remeasure_corpus",
    )
)
CANDIDATE_PATTERN = re.compile(
    r"(?:HIRA|심평원|심사평가원|급여|보험인정|상병|질병코드|환자수|"
    r"효능효과|식약처|허가정보|의약품|NEDRUG)",
    re.IGNORECASE,
)
FORMULAIC_LIMITATIONS = frozenset(
    {
        "요청한 조회 중 일부를 확인하지 못했습니다.",
        "근거와 결속되지 않은 일부 표현은 답변에서 제외했습니다.",
    }
)
OUTPUT_ROOT = Path(os.environ.get("REMEASURE_OUTPUT", "/tmp/v3_post_mcp_remeasure"))
INTERVAL_S = float(os.environ.get("REMEASURE_INTERVAL_S", "2.0"))
MAX_ATTEMPTS = int(os.environ.get("REMEASURE_MAX_ATTEMPTS", "2"))
_WRITE_LOCK = threading.Lock()
_READ_SQL = frozenset({"SELECT", "WITH", "SHOW", "DESCRIBE", "EXPLAIN"})
_COMMENT_PREFIX = re.compile(r"^(?:\s|/\*.*?\*/|--[^\n]*\n|#[^\n]*\n)+", re.DOTALL)


class DbWriteGuard:
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
                and not guard._safe_session_set(query)
            ):
                with guard._lock:
                    guard.blocked_statements.append(verb or "UNKNOWN")
                raise RuntimeError(f"DB_WRITE_BLOCKED:{verb or 'UNKNOWN'}")
            with guard._lock:
                guard.read_statements += 1
            return original_execute(cursor, query, args)

        def guarded_executemany(
            cursor: object,
            query: object,
            args: object,
        ) -> object:
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
    def _safe_session_set(query: object) -> bool:
        text = " ".join(str(query).upper().split())
        return text in {
            "SET TRANSACTION READ ONLY",
            "SET SESSION TRANSACTION READ ONLY",
        }


class SelectionCapture:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate
        self.request: dict[str, object] | None = None
        self.response_text = ""
        self.response_json: object = None
        self.status_code: int | None = None
        self.latency_ms = 0.0

    def __call__(self, url: str, **kwargs: object) -> object:
        payload = kwargs.get("json")
        self.request = dict(payload) if isinstance(payload, Mapping) else None
        started = time.monotonic()
        response = self.delegate(url, **kwargs)
        self.latency_ms = (time.monotonic() - started) * 1000
        self.status_code = int(getattr(response, "status_code", 0))
        content = bytes(getattr(response, "content", b""))
        self.response_text = content.decode("utf-8", errors="replace")
        try:
            self.response_json = response.json()
        except ValueError:
            self.response_json = None
        return response


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("targeted", "nondeterminism"))
    args = parser.parse_args()
    assert_after_recovery(utc_now())
    raw_dir = OUTPUT_ROOT / "remeasure"
    raw_dir.mkdir(parents=True, exist_ok=True)
    log_path = OUTPUT_ROOT / "run_progress.jsonl"
    db_guard = DbWriteGuard()
    db_guard.install()

    # Fail early if the runtime cannot construct its read-only dependencies.
    dependencies = build_agent_loop_dependencies(external_mode="live")
    if dependencies.query_layer is None:
        raise RuntimeError("read-only query layer unavailable")

    selection_provider = GenosV3ToolChoiceProvider.from_env()
    fusion_provider = GenosV3FusionProvider.from_env()
    selector = V3ToolSelector(provider=selection_provider, max_calls=8)
    web_client = ExternalApiClient(mode="live", timeout_s=12)
    questions, rounds = measurement_plan(args.mode)
    conditions = {
        "started_at_utc": utc_now(),
        "mode": args.mode,
        "question_count": len(questions),
        "rounds": rounds,
        "planned_runs": sum(rounds.values()),
        "interval_s": INTERVAL_S,
        "interleaved_rounds": True,
        "selection_provider": {
            "base_url": selection_provider.base_url,
            "model_request": selection_provider.model,
            "timeout_s": selection_provider.timeout_s,
            "max_tokens": selection_provider.max_tokens,
            "temperature": 0,
            "top_p": None,
            "top_k": None,
            "seed": None,
            "n": 1,
        },
        "fusion_provider": {
            "base_url": fusion_provider.base_url,
            "model_request": fusion_provider.model,
            "timeout_s": fusion_provider.timeout_s,
            "max_tokens": fusion_provider.max_tokens,
            "temperature": fusion_provider.temperature,
            "top_p": None,
            "top_k": None,
            "seed": None,
            "n": 1,
            "default_max_tokens": DEFAULT_FUSION_MAX_TOKENS,
        },
        "cache_bypass_requested": False,
        "serving_consumption": False,
        "live_chat_call_count": 0,
        "general_composite_execution_allowed": False,
        "recovery_floor_utc": RECOVERY_FLOOR.isoformat().replace("+00:00", "Z"),
    }
    write_json(OUTPUT_ROOT / f"conditions_{args.mode}.json", conditions)

    completed = 0
    for run_number in range(1, max(rounds.values()) + 1):
        for index, question in questions.items():
            if run_number > rounds[index]:
                continue
            destination = raw_dir / f"{index:03d}_run{run_number}.json"
            if destination.exists():
                validate_existing_record(
                    destination,
                    expected_mode=args.mode,
                    expected_index=index,
                    expected_run=run_number,
                )
                continue
            record = run_one(
                index=index,
                run_number=run_number,
                question=question,
                mode=args.mode,
                selector=selector,
                fusion_provider=fusion_provider,
                web_client=web_client,
                db_guard=db_guard,
            )
            write_json(destination, record)
            append_jsonl(log_path, run_summary(record))
            completed += 1
            print(json.dumps(run_summary(record), ensure_ascii=False, sort_keys=True), flush=True)

            accepted = accepted_claims(record)
            if index == 95 and accepted:
                write_json(
                    OUTPUT_ROOT / "hard_stop.json",
                    {
                        "reason": "fake_brand_answered",
                        "index": index,
                        "run": run_number,
                        "accepted_claims": accepted,
                        "at_utc": utc_now(),
                    },
                )
                return 95
            if index == 86 and accepted:
                write_json(
                    OUTPUT_ROOT / "hard_stop.json",
                    {
                        "reason": "typed_empty_case_086_answered",
                        "index": index,
                        "run": run_number,
                        "accepted_claims": accepted,
                        "at_utc": utc_now(),
                    },
                )
                return 86
            if db_guard.blocked_statements:
                write_json(
                    OUTPUT_ROOT / "hard_stop.json",
                    {
                        "reason": "db_write_attempt",
                        "blocked_statement_verbs": db_guard.blocked_statements,
                        "at_utc": utc_now(),
                    },
                )
                return 96
            time.sleep(INTERVAL_S)

    write_json(
        OUTPUT_ROOT / f"runtime_guard_{args.mode}.json",
        {
            "mode": args.mode,
            "completed_at_utc": utc_now(),
            "completed_new_runs": completed,
            "total_raw_files": len(tuple(raw_dir.glob("*_run*.json"))),
            "db_read_statements": db_guard.read_statements,
            "db_write_attempts": len(db_guard.blocked_statements),
            "blocked_statement_verbs": db_guard.blocked_statements,
            "live_chat_call_count": 0,
            "serving_consumption": False,
            "cache_bypass_requested": False,
        },
    )
    return 0


def measurement_plan(mode: str) -> tuple[dict[int, str], dict[int, int]]:
    if mode == "nondeterminism":
        return dict(NONDETERMINISM_QUESTIONS), {
            index: 5 for index in NONDETERMINISM_QUESTIONS
        }

    candidates = candidate_questions()
    # Index 77 is measured five times by the exact nondeterminism procedure;
    # its first three runs are also the required targeted comparison.
    candidates.pop(77, None)
    questions = dict(candidates)
    questions.update(TARGETED_QUESTIONS)
    rounds = {index: 1 for index in candidates}
    rounds.update({index: 3 for index in TARGETED_QUESTIONS})
    write_json(
        OUTPUT_ROOT / "candidate_selection.json",
        {
            "criteria": CANDIDATE_PATTERN.pattern,
            "corpus_size": len(tuple(CORPUS_ROOT.glob("*.json"))),
            "candidate_count_including_077": len(candidates) + 1,
            "candidates_executed_here": sorted(candidates),
            "index_077_measured_in_nondeterminism_mode": True,
        },
    )
    return questions, rounds


def candidate_questions() -> dict[int, str]:
    rows: dict[int, str] = {}
    files = sorted(CORPUS_ROOT.glob("*.json"))
    if len(files) != 245:
        raise RuntimeError(f"expected 245 corpus records, found {len(files)}")
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        measurement = payload.get("measurement")
        if not isinstance(measurement, Mapping):
            raise RuntimeError(f"missing measurement in {path}")
        index = int(measurement["index"])
        question = str(measurement["question"])
        if CANDIDATE_PATTERN.search(question):
            rows[index] = question
    return rows


def validate_existing_record(
    path: Path,
    *,
    expected_mode: str,
    expected_index: int,
    expected_run: int,
) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    measurement = payload.get("measurement")
    if not isinstance(measurement, Mapping):
        raise RuntimeError(f"missing measurement in existing raw record: {path}")
    identity = (
        str(measurement.get("mode")),
        int(measurement.get("index", -1)),
        int(measurement.get("run", -1)),
    )
    expected = (expected_mode, expected_index, expected_run)
    if identity != expected:
        raise RuntimeError(
            f"existing raw record identity mismatch: {path}: {identity!r} != {expected!r}"
        )
    assert_after_recovery(str(measurement.get("started_at_utc") or ""))
    assert_after_recovery(str(measurement.get("completed_at_utc") or ""))


def run_one(
    *,
    index: int,
    run_number: int,
    question: str,
    mode: str,
    selector: V3ToolSelector,
    fusion_provider: GenosV3FusionProvider,
    web_client: ExternalApiClient,
    db_guard: DbWriteGuard,
) -> dict[str, object]:
    started_at = utc_now()
    assert_after_recovery(started_at)
    started = time.monotonic()
    selection_started = time.monotonic()
    selection_capture = SelectionCapture(selection_provider_module.requests.post)
    selection_provider_module.requests.post = selection_capture
    selection_error: dict[str, str] | None = None
    selection = None
    try:
        selection = selector.select(question)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        selection_error = {"type": type(exc).__name__, "message": str(exc)[:500]}
    finally:
        selection_provider_module.requests.post = selection_capture.delegate
    selection_wall_ms = (time.monotonic() - selection_started) * 1000
    choices = tuple(selection.choices) if selection is not None else ()

    execution_started = time.monotonic()
    executor = build_default_shadow_executor(question)
    bundle = executor.execute(choices)
    execution_wall_ms = (time.monotonic() - execution_started) * 1000

    web_calls: list[dict[str, object]] = []
    augmenter = V3WebAugmenter(
        search=lambda query, **kwargs: web_search(
            web_client,
            web_calls,
            query,
            **kwargs,
        )
    )
    web_started = time.monotonic()
    augmented = augmenter.augment(question, bundle)
    web_wall_ms = (time.monotonic() - web_started) * 1000

    messages = build_fusion_messages(question, augmented.bundle)
    attempts: list[dict[str, object]] = []
    fusion_started = time.monotonic()
    generated = None
    typed_failure: dict[str, object] | None = None
    fusion_error: dict[str, object] | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        call_started = utc_now()
        try:
            generated = V3FusionEngine(fusion_provider).generate(question, augmented.bundle)
            attempts.append(
                fusion_attempt_record(
                    attempt,
                    call_started,
                    provider=generated.provider,
                )
            )
            break
        except FusionOutputTruncatedError as exc:
            typed_failure = {
                "reason_code": exc.reason_code,
                "limitations": list(exc.limitations),
                "provider": provider_record(exc.provider),
            }
            attempts.append(
                fusion_attempt_record(
                    attempt,
                    call_started,
                    provider=exc.provider,
                )
            )
            break
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            fusion_error = {"type": type(exc).__name__, "message": str(exc)[:500]}
            attempts.append(
                {
                    "attempt": attempt,
                    "started_at_utc": call_started,
                    "completed_at_utc": utc_now(),
                    "error": fusion_error,
                }
            )
    fusion_wall_ms = (time.monotonic() - fusion_started) * 1000

    if generated is not None:
        fusion = {
            "status": "generated",
            "provider": provider_record(generated.provider),
            "generated_answer": generated.generated.model_dump(mode="json"),
            "validated_answer": generated.validated.model_dump(mode="json"),
            "attempts": attempts,
        }
    elif typed_failure is not None:
        fusion = {"status": "typed_failure", **typed_failure, "attempts": attempts}
    else:
        fusion = {"status": "error", "error": fusion_error, "attempts": attempts}

    selection_record = {
        "status": "selected" if selection is not None else "error",
        "error": selection_error,
        "intent": selection.intent.model_dump(mode="json") if selection is not None else None,
        "candidate_names": list(selection.candidate_names) if selection is not None else [],
        "provider_choice_count": selection.provider_choice_count if selection is not None else None,
        "unknown_tool_names": list(selection.unknown_tool_names) if selection is not None else [],
        "choices": [
            {
                "name": choice.name,
                "arguments": normalize(choice.arguments),
                "call_id": choice.call_id,
            }
            for choice in choices
        ],
        "request": selection_capture.request,
        "raw_response_text": selection_capture.response_text,
        "raw_response_json": selection_capture.response_json,
        "http_status": selection_capture.status_code,
        "provider_latency_ms": round(selection_capture.latency_ms, 3),
        "wall_ms": round(selection_wall_ms, 3),
    }
    execution_record = bundle_record(bundle)
    web_record = {
        "eligibility": asdict(augmented.eligibility),
        "expanded_to_general": augmented.expanded_to_general,
        "search_log": [asdict(entry) for entry in augmented.search_log],
        "calls": web_calls,
        "web_facts": [
            normalize(asdict(fact))
            for fact in augmented.bundle.facts
            if getattr(fact, "fact_type", "") == "web_source"
        ],
        "wall_ms": round(web_wall_ms, 3),
    }
    hashes = {
        "selected_tools_hash": stable_hash(
            sorted(
                (
                    {"name": choice.name, "arguments": normalize(choice.arguments)}
                    for choice in choices
                ),
                key=lambda item: (str(item["name"]), canonical_json(item["arguments"])),
            )
        ),
        "evidence_bundle_hash": stable_hash(
            sorted(
                (
                    {
                        "evidence_id": getattr(fact, "evidence_id", ""),
                        "value": normalize(asdict(fact) if is_dataclass(fact) else fact),
                    }
                    for fact in augmented.bundle.facts
                ),
                key=lambda item: str(item["evidence_id"]),
            )
        ),
        "internal_evidence_bundle_hash": stable_hash(
            sorted(
                (
                    {
                        "evidence_id": getattr(fact, "evidence_id", ""),
                        "value": normalize(asdict(fact) if is_dataclass(fact) else fact),
                    }
                    for fact in bundle.facts
                ),
                key=lambda item: str(item["evidence_id"]),
            )
        ),
        "web_result_hash": stable_hash(
            sorted(
                (
                    {
                        "url": str(fact.get("url") or ""),
                        "excerpt": str(fact.get("excerpt") or ""),
                    }
                    for fact in web_record["web_facts"]
                    if isinstance(fact, Mapping)
                ),
                key=lambda item: (item["url"], item["excerpt"]),
            )
        ),
    }
    completed_at = utc_now()
    assert_after_recovery(completed_at)
    return {
        "measurement": {
            "mode": mode,
            "index": index,
            "run": run_number,
            "question": question,
            "started_at_utc": started_at,
            "completed_at_utc": completed_at,
            "end_to_end_latency_ms": round((time.monotonic() - started) * 1000, 3),
        },
        "selection": selection_record,
        "execution": execution_record,
        "web": web_record,
        "fusion_request": {"messages": messages},
        "fusion": fusion,
        "hashes": hashes,
        "latency": {
            "selection_ms": round(selection_wall_ms, 3),
            "execution_ms": round(execution_wall_ms, 3),
            "web_ms": round(web_wall_ms, 3),
            "fusion_ms": round(fusion_wall_ms, 3),
        },
        "guards": {
            "db_read_statements_cumulative": db_guard.read_statements,
            "db_write_attempts_cumulative": len(db_guard.blocked_statements),
            "live_chat_call_count": 0,
            "serving_consumption": False,
            "general_composite_execution_allowed": False,
            "cache_bypass_requested": False,
        },
    }


def web_search(
    client: ExternalApiClient,
    calls: list[dict[str, object]],
    query: str,
    *,
    topic: str,
) -> WebSearchResult:
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


def fusion_attempt_record(
    attempt: int,
    started_at: str,
    *,
    provider: object,
) -> dict[str, object]:
    return {
        "attempt": attempt,
        "started_at_utc": started_at,
        "completed_at_utc": utc_now(),
        "provider": provider_record(provider),
    }


def accepted_claims(record: Mapping[str, object]) -> list[dict[str, object]]:
    fusion = record.get("fusion")
    if not isinstance(fusion, Mapping):
        return []
    validated = fusion.get("validated_answer")
    if not isinstance(validated, Mapping):
        return []
    answer = validated.get("answer")
    if not isinstance(answer, Mapping):
        return []
    claims = answer.get("claims")
    return [dict(item) for item in claims if isinstance(item, Mapping)] if isinstance(claims, Sequence) else []


def run_summary(record: Mapping[str, object]) -> dict[str, object]:
    measurement = record["measurement"]
    fusion = record["fusion"]
    return {
        "at_utc": measurement["completed_at_utc"],
        "index": measurement["index"],
        "run": measurement["run"],
        "selection_status": record["selection"]["status"],
        "selected_count": len(record["selection"]["choices"]),
        "execution_status": record["execution"]["status"],
        "web_call_count": len(record["web"]["calls"]),
        "fusion_status": fusion["status"],
        "accepted_claim_count": len(accepted_claims(record)),
        "hashes": record["hashes"],
        "end_to_end_latency_ms": measurement["end_to_end_latency_ms"],
    }


def normalize(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return normalize(asdict(value))
    if hasattr(value, "model_dump"):
        return normalize(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(key): normalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [normalize(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def canonical_json(value: object) -> str:
    return json.dumps(normalize(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def stable_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def append_jsonl(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK, path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def assert_after_recovery(value: str) -> None:
    measured_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if measured_at <= RECOVERY_FLOOR:
        raise RuntimeError(
            f"measurement timestamp {value} is not after recovery floor "
            f"{RECOVERY_FLOOR.isoformat()}"
        )


if __name__ == "__main__":
    sys.exit(main())
