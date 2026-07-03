from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.scripts.analysis.brand_activity.llm_topic.genos_client import GenosServingClient

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
    """Tiny adapter around the existing GenOS serving client."""

    def __init__(self, *, base_url: str, token: str, serving_id: str) -> None:
        from pipeline.scripts.analysis.brand_activity.llm_topic.genos_client import GenosServingClient

        self._client = GenosServingClient(base_url=base_url, token=token, serving_id=serving_id, timeout_s=120.0)

    def classify(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, int], int]:
        """Return raw content, usage, and latency for one batch call."""
        result = self._client.chat(messages)
        if result["status"] != "ok":
            raise AssignmentParseError(f"serving call failed: {result['error_type']} {result['error_message']}")
        usage = {
            "prompt_tokens": int(result.get("usage", {}).get("prompt_tokens", 0)),
            "completion_tokens": int(result.get("usage", {}).get("completion_tokens", 0)),
            "total_tokens": int(result.get("usage", {}).get("total_tokens", 0)),
        }
        return result["content"], usage, int(result["latency_ms"])


def plan_batches(
    rows: list[AssignmentInputRow],
    *,
    batch_size: int,
    prompt_version: str,
    checkpoint_path: Path,
) -> BatchPlan:
    """Build pending batches, excluding completed checkpoint entries."""
    completed = _completed_batch_ids(checkpoint_path)
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
