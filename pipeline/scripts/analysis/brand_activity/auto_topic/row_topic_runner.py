from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

from .row_topic_assignment import (
    AssignmentInputRow,
    AssignmentParseError,
    RowTopicAssignment,
    TopicRubric,
    parse_assignment_response,
    row_topic_prompt,
)


EMPIRICAL_USD_PER_CALL = 7.05 / 333.0


@dataclass(frozen=True, slots=True)
class AssignmentBatch:
    """One idempotent row-topic classification batch."""

    batch_id: str
    rows: tuple[AssignmentInputRow, ...]


@dataclass(frozen=True, slots=True)
class BatchPlan:
    """Dry-run estimate and pending work after checkpoint inspection."""

    total_rows: int
    total_scope_brand_pairs: int
    total_batches: int
    pending_batches: tuple[AssignmentBatch, ...]
    estimated_calls: int
    estimated_usd: float


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    """Execution settings for a gated row-topic assignment run."""

    topic_set_version: str
    prompt_version: str = "row_topic_v1"
    batch_size: int = 150
    max_calls: int = 0
    checkpoint_path: Path = Path("row_topic_assignment_checkpoint.jsonl")


class AssignmentChatClient:
    """Small stdlib GenOS serving client for batch assignment calls."""

    def __init__(self, *, base_url: str, token: str, serving_id: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._serving_id = serving_id
        self._path_template = os.environ.get("GENOS_GATEWAY_CHAT_PATH_TEMPLATE", "/api/gateway/rep/serving/{serving_id}/chat/completions")

    def classify(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, int], int]:
        """Return raw content, usage, and latency for one batch call."""
        start = time.perf_counter()
        payload = json.dumps({"messages": messages, "stream": False, "temperature": 0.0}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        path = self._path_template.format(serving_id=self._serving_id)
        endpoint = f"{self._base_url}{path}"
        request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=150) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AssignmentParseError(f"serving call failed: {type(exc).__name__} {str(exc)[:300]}") from exc
        latency_ms = int((time.perf_counter() - start) * 1000)
        usage = _extract_usage(body)
        return _extract_content(body), usage, latency_ms


def plan_batches(
    rows: list[AssignmentInputRow],
    *,
    batch_size: int,
    prompt_version: str,
    checkpoint_path: Path,
    ignore_checkpoint: bool = False,
) -> BatchPlan:
    """Build pending batches, excluding completed checkpoint entries."""
    completed = set() if ignore_checkpoint else _completed_batch_ids(checkpoint_path)
    all_batches = tuple(_build_batches(rows, batch_size, prompt_version))
    pending = tuple(batch for batch in all_batches if batch.batch_id not in completed)
    return BatchPlan(
        total_rows=len(rows),
        total_scope_brand_pairs=len({(row.scope_id, row.brand) for row in rows}),
        total_batches=len(all_batches),
        pending_batches=pending,
        estimated_calls=len(pending),
        estimated_usd=round(len(pending) * EMPIRICAL_USD_PER_CALL, 4),
    )


def run_assignment_batches(
    rows: list[AssignmentInputRow],
    rubric: tuple[TopicRubric, ...],
    client: AssignmentChatClient,
    config: RunnerConfig,
) -> list[RowTopicAssignment]:
    """Classify pending batches with exact-id parsing and checkpoint writes."""
    plan = plan_batches(
        rows,
        batch_size=config.batch_size,
        prompt_version=config.prompt_version,
        checkpoint_path=config.checkpoint_path,
    )
    if config.max_calls and plan.estimated_calls > config.max_calls:
        raise AssignmentParseError(f"pending calls {plan.estimated_calls} exceed cap {config.max_calls}")
    known_topic_ids = {topic.topic_id for topic in rubric}
    assignments: list[RowTopicAssignment] = []
    for batch in plan.pending_batches:
        messages = row_topic_prompt(rubric, batch.rows)
        content, usage, latency_ms = client.classify(messages)
        parsed = parse_assignment_response(content, list(batch.rows), known_topic_ids, config.topic_set_version, batch.batch_id)
        assignments.extend(parsed)
        _append_checkpoint(
            config.checkpoint_path,
            {
                "batch_id": batch.batch_id,
                "status": "ok",
                "row_count": len(batch.rows),
                "assignment_count": len(parsed),
                "latency_ms": latency_ms,
                "usage": usage,
            },
        )
    return assignments


def client_from_env(*, base_url: str, serving_id: str, token_env: str = "GENOS_BEARER_TOKEN") -> AssignmentChatClient:
    """Create a GenOS client without logging secret values."""
    token = os.environ.get(token_env, "")
    if not token:
        raise AssignmentParseError(f"{token_env} is not set")
    return AssignmentChatClient(base_url=base_url, token=token, serving_id=serving_id)


def pending_rows(rows: list[AssignmentInputRow], assigned_row_ids: set[int]) -> list[AssignmentInputRow]:
    """Return rows that still need assignment for the selected topic-set version."""
    return [row for row in rows if row.row_id not in assigned_row_ids]


def _build_batches(rows: list[AssignmentInputRow], batch_size: int, prompt_version: str) -> tuple[AssignmentBatch, ...]:
    if batch_size <= 0:
        raise AssignmentParseError("batch_size must be positive")
    grouped: dict[tuple[str, str], list[AssignmentInputRow]] = {}
    for row in rows:
        grouped.setdefault((row.scope_id, row.brand), []).append(row)
    batches: list[AssignmentBatch] = []
    for scope_id, brand in sorted(grouped):
        group_rows = sorted(grouped[(scope_id, brand)], key=lambda row: row.row_id)
        for index, offset in enumerate(range(0, len(group_rows), batch_size), start=1):
            chunk = tuple(group_rows[offset : offset + batch_size])
            if not chunk:
                continue
            batches.append(
                AssignmentBatch(
                    batch_id=f"{scope_id}:{brand}:{prompt_version}:{index:06d}",
                    rows=chunk,
                )
            )
    return tuple(batches)


def _completed_batch_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    completed: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict) and value.get("status") == "ok" and isinstance(value.get("batch_id"), str):
            completed.add(value["batch_id"])
    return completed


def _append_checkpoint(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _extract_content(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
            text = first.get("text")
            if isinstance(text, str):
                return text
    content = payload.get("content")
    if isinstance(content, str):
        return content
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("content"), str):
        return data["content"]
    return ""


def _extract_usage(payload: object) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    result: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            result[key] = value
    return result
