#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import csv
import io
import json
from pathlib import Path

from jw_chat_agent_poc.tool_use.routing_v4_capabilities import (
    default_capability_matrix,
)
from jw_chat_agent_poc.tool_use.routing_v4_rules import classify_question


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--material", type=Path, required=True)
    parser.add_argument("--baseline-pairs", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.material.read_text(encoding="utf-8").splitlines()]
    if len(rows) != 245:
        raise ValueError(f"expected 245 material rows, got {len(rows)}")
    baseline = _baseline_by_index(args.baseline_pairs)
    matrix = default_capability_matrix()
    fieldnames = (
        "index",
        "question",
        "legacy_source_domain",
        "legacy_capability",
        "legacy_capability_status",
        "legacy_routing_v4_response_type",
        "stored_live_match_type",
        "stored_live_response_type",
        "v3_response_type",
        "v3_accepted_claim_count",
        "v3_limitation_count",
        "mechanical_response_type_diff",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        for row in rows:
            question = str(row["question"])
            telemetry_sink = io.StringIO()
            with redirect_stdout(telemetry_sink):
                classification = classify_question(question)
            capability = matrix.resolve(
                classification.source_domain,
                classification.requested_capability,
                input_key=classification.input_key,
            )
            legacy_type = _legacy_module_type(capability.status.value)
            stored = baseline.get(int(row["index"]), {})
            stored_answer = str(stored.get("baseline_answer_full") or "")
            v3_type = _v3_type(row)
            writer.writerow(
                {
                    "index": row["index"],
                    "question": question,
                    "legacy_source_domain": classification.source_domain,
                    "legacy_capability": classification.requested_capability,
                    "legacy_capability_status": capability.status.value,
                    "legacy_routing_v4_response_type": legacy_type,
                    "stored_live_match_type": stored.get("match_type", "unmatched"),
                    "stored_live_response_type": _stored_answer_type(stored_answer),
                    "v3_response_type": v3_type,
                    "v3_accepted_claim_count": row.get("accepted_claim_count", 0),
                    "v3_limitation_count": len(row.get("limitations_full") or ()),
                    "mechanical_response_type_diff": legacy_type != v3_type,
                }
            )
    return 0


def _baseline_by_index(path: Path | None) -> dict[int, dict[str, object]]:
    if path is None:
        return {}
    return {
        int(row["index"]): row
        for row in (
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        )
    }


def _legacy_module_type(status: str) -> str:
    if status == "UNRESOLVED":
        return "uncovered"
    if status == "SUPPORTED":
        return "answer_candidate"
    return "typed_failure"


def _v3_type(row: dict[str, object]) -> str:
    if int(row.get("accepted_claim_count") or 0) > 0:
        return "answer_candidate"
    if row.get("limitations_full"):
        return "typed_failure"
    return "empty"


def _stored_answer_type(answer: str) -> str:
    if not answer:
        return "unmatched"
    if answer.startswith("시장, 브랜드, 기간, 지표를 포함해 질문하면"):
        return "generic_help"
    if "확인하지 못" in answer or "조회 오류" in answer or "확인 불가" in answer:
        return "typed_failure"
    return "answer"


if __name__ == "__main__":
    raise SystemExit(main())
