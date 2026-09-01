from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final


ANSWER_STAGE_TRACE_ENV: Final = "JW_CHAT_ANSWER_STAGE_TRACE_ENABLED"


@dataclass(slots=True)
class AnswerAssemblyTrace:
    """Accumulate request-local IDs without retaining answer content in the payload."""

    result: dict[str, Any]
    fact_ids: tuple[str, ...]
    claim_ids_by_text: dict[str, str] = field(default_factory=dict)
    stages: list[dict[str, Any]] = field(default_factory=list)

    def claim_ids(self, answer: str) -> tuple[str, ...]:
        ids: list[str] = []
        for line in answer.splitlines():
            claim = line.strip()
            if not claim:
                continue
            claim_id = self.claim_ids_by_text.get(claim)
            if claim_id is None:
                claim_id = f"c{len(self.claim_ids_by_text) + 1:04d}"
                self.claim_ids_by_text[claim] = claim_id
            if claim_id not in ids:
                ids.append(claim_id)
        return tuple(ids)

    def record(self, name: str, before: str, after: str) -> None:
        before_claim_ids = self.claim_ids(before)
        after_claim_ids = self.claim_ids(after)
        self.stages.append(
            {
                "seq": len(self.stages) + 1,
                "name": name,
                "before": self._snapshot(before_claim_ids),
                "after": self._snapshot(after_claim_ids),
                "diff": {
                    "fact_ids": {"added": [], "removed": []},
                    "claim_ids": {
                        "added": self._difference(after_claim_ids, before_claim_ids),
                        "removed": self._difference(before_claim_ids, after_claim_ids),
                    },
                },
            }
        )
        self._publish()

    def _snapshot(self, claim_ids: tuple[str, ...]) -> dict[str, list[str]]:
        return {"fact_ids": list(self.fact_ids), "claim_ids": list(claim_ids)}

    @staticmethod
    def _difference(values: tuple[str, ...], baseline: tuple[str, ...]) -> list[str]:
        baseline_set = set(baseline)
        return [value for value in values if value not in baseline_set]

    def _publish(self) -> None:
        gate = self.result.setdefault("_qa_claim_gate", {})
        observability = gate.setdefault("pipeline_observability", {})
        observability["answer_assembly_v1"] = {
            "schema_version": 1,
            "enabled": True,
            "redaction": "ids_only_no_user_text",
            "stages": list(self.stages),
        }


def answer_stage_trace_enabled() -> bool:
    return os.getenv(ANSWER_STAGE_TRACE_ENV, "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def fact_ids_from_markdown_response(markdown_response: Any) -> tuple[str, ...]:
    if not isinstance(markdown_response, Mapping):
        return ()
    evidence = markdown_response.get("evidence")
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
        return ()
    ids: list[str] = []
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        fact_id = item.get("fact_id") or item.get("evidence_id")
        if isinstance(fact_id, str) and fact_id and fact_id not in ids:
            ids.append(fact_id)
    return tuple(ids)


def traced_transform(
    trace: AnswerAssemblyTrace,
    name: str,
    transform: Callable[[str], str],
) -> Callable[[str], str]:
    def apply(answer: str) -> str:
        updated = transform(answer)
        trace.record(name, answer, updated)
        return updated

    return apply
