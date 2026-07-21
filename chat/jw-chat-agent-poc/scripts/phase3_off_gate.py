#!/usr/bin/env python3
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any, Final


SET_B_SUITES: Final[frozenset[str]] = frozenset({"PORTAL_47", "NATURAL_9"})
SEMANTIC_FIELDS: Final[tuple[str, ...]] = (
    "scope",
    "route",
    "tools_called",
    "quality_taxonomy",
    "answer_contract_status",
    "tool_names",
    "tool_statuses",
)
STABLE_TOOL_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "status",
    "row_count",
    "data_as_of",
    "endpoint",
    "source_epoch",
    "built_at",
)
APPROVED_PRESENTATION_EXCEPTIONS: Final[frozenset[str]] = frozenset(
    {"C_03", "owner_brand_share"}
)
EXPECTED_CANDIDATE_ROUTES: Final[dict[str, dict[str, str | None]]] = {
    "B-03": {"mode": "guard", "deterministic_execution": None},
    "B-05": {"mode": "guard", "deterministic_execution": None},
}


@dataclass(frozen=True, slots=True)
class Inputs:
    baseline_path: Path
    candidate_path: Path
    expected_commit: str
    expected_digest: str
    output_path: Path


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def _rows_by_id(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = snapshot.get("rows")
    if not isinstance(rows, list):
        return {}
    return {
        str(row["case_id"]): row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("case_id"), str)
    }


def _tool_contracts(result: dict[str, Any]) -> list[dict[str, Any]]:
    qa_trace = result.get("qa_trace")
    tools = qa_trace.get("tools") if isinstance(qa_trace, dict) else None
    if not isinstance(tools, list):
        return []
    return [
        {field: tool.get(field) for field in STABLE_TOOL_FIELDS}
        for tool in tools
        if isinstance(tool, dict)
    ]


def _routing_v4_absent(result: dict[str, Any]) -> bool:
    qa_trace = result.get("qa_trace")
    return not isinstance(qa_trace, dict) or "routing_v4" not in qa_trace


def _numeric_tokens_comparison(
    case_id: str,
    baseline_value: Any,
    candidate_value: Any,
) -> dict[str, Any]:
    if case_id in APPROVED_PRESENTATION_EXCEPTIONS:
        baseline_tokens = [str(value) for value in baseline_value] if isinstance(baseline_value, list) else []
        candidate_tokens = [str(value) for value in candidate_value] if isinstance(candidate_value, list) else []
        passed = Counter(baseline_tokens) == Counter(candidate_tokens)
        mode = "approved_presentation_numeric_multiset"
    else:
        passed = baseline_value == candidate_value
        mode = "exact"
    return {
        "passed": passed,
        "mode": mode,
        "baseline": baseline_value,
        "candidate": candidate_value,
    }


def _result(row: dict[str, Any]) -> dict[str, Any] | None:
    value = row.get("result")
    return value if isinstance(value, dict) else None


def _compare_case(
    case_id: str,
    baseline_row: dict[str, Any],
    candidate_row: dict[str, Any],
) -> dict[str, Any]:
    baseline_result = _result(baseline_row)
    candidate_result = _result(candidate_row)
    if baseline_result is None or candidate_result is None:
        return {"case_id": case_id, "passed": False, "reason": "missing_result"}

    field_checks = {
        field: baseline_result.get(field) == candidate_result.get(field)
        for field in SEMANTIC_FIELDS
    }
    expected_route = EXPECTED_CANDIDATE_ROUTES.get(case_id)
    if expected_route is not None:
        field_checks["route"] = candidate_result.get("route") == expected_route
    field_checks["tool_contracts"] = _tool_contracts(baseline_result) == _tool_contracts(
        candidate_result
    )
    numeric_comparison = _numeric_tokens_comparison(
        case_id,
        baseline_result.get("numeric_tokens"),
        candidate_result.get("numeric_tokens"),
    )
    field_checks["numeric_tokens"] = numeric_comparison["passed"]
    baseline_chars = baseline_result.get("answer_chars")
    candidate_chars = candidate_result.get("answer_chars")
    length_ok = (
        isinstance(baseline_chars, int)
        and isinstance(candidate_chars, int)
        and candidate_chars > 0
        and candidate_chars <= max(baseline_chars * 2, baseline_chars + 200)
    )
    checks = {
        "identity": candidate_result.get("identity_ok") is True,
        "body_nonempty": bool(str(candidate_result.get("answer") or "").strip()),
        "routing_v4_absent_in_off": _routing_v4_absent(candidate_result),
        "legacy_prs_absent": candidate_result.get("legacy_prs_absent") is True,
        "legacy_ccs_absent": candidate_result.get("legacy_ccs_absent") is True,
        "length_not_over_2x": length_ok,
        **field_checks,
    }
    return {
        "case_id": case_id,
        "passed": all(checks.values()),
        "checks": checks,
        "answer_byte_equal": baseline_result.get("answer_sha256")
        == candidate_result.get("answer_sha256"),
        "baseline_answer_sha256": baseline_result.get("answer_sha256"),
        "candidate_answer_sha256": candidate_result.get("answer_sha256"),
        "baseline_answer_chars": baseline_chars,
        "candidate_answer_chars": candidate_chars,
        "numeric_tokens_comparison": numeric_comparison,
        "route_comparison": {
            "mode": "expected_deterministic_contract" if expected_route else "baseline_exact",
            "expected": expected_route,
        },
    }


def _golden_checks(candidate_rows: dict[str, dict[str, Any]]) -> dict[str, bool]:
    market_hhi = _result(candidate_rows.get("A_03", {})) or {}
    brand_hhi = _result(candidate_rows.get("E1_market_hhi", {})) or {}
    market_answer = str(market_hhi.get("answer") or "")
    brand_answer = str(brand_hhi.get("answer") or "")
    return {
        "market_hhi_253_62": "253.62" in market_answer,
        "market_hhi_period_2026_05": "2026-05" in market_answer,
        "brand_anchor_hhi_262_42_display": "262.42" in brand_answer,
    }


def evaluate(inputs: Inputs) -> dict[str, Any]:
    baseline = _load_object(inputs.baseline_path)
    candidate = _load_object(inputs.candidate_path)
    baseline_rows = _rows_by_id(baseline)
    candidate_rows = _rows_by_id(candidate)
    cases: list[dict[str, Any]] = []
    missing: list[str] = []
    for case_id, baseline_row in baseline_rows.items():
        if baseline_row.get("suite") not in SET_B_SUITES:
            continue
        candidate_row = candidate_rows.get(case_id)
        if candidate_row is None:
            missing.append(case_id)
            continue
        cases.append(_compare_case(case_id, baseline_row, candidate_row))

    failed_cases = [row["case_id"] for row in cases if row.get("passed") is not True]
    presentation_drift = [
        row["case_id"]
        for row in cases
        if row.get("passed") is True and row.get("answer_byte_equal") is False
    ]
    snapshot_checks = {
        "commit": candidate.get("commit") == inputs.expected_commit,
        "digest": candidate.get("digest") == inputs.expected_digest,
        "logical_count_69": candidate.get("logical_case_count") == 69,
        "portal_count_47": candidate.get("portal_47_logical_count") == 47,
        "natural_count_9": candidate.get("natural_9_logical_count") == 9,
        "set_a_count_13": candidate.get("set_a_logical_count") == 13,
        "capture_failures_zero": candidate.get("failures") == [],
        "cleanup_http_failures_zero": (
            isinstance(candidate.get("cleanup"), dict)
            and candidate["cleanup"].get("http_failures") == 0
        ),
        "cleanup_residual_zero": (
            isinstance(candidate.get("cleanup"), dict)
            and candidate["cleanup"].get("residual_count") == 0
        ),
        "set_b_case_count_56": len(cases) == 56,
        "missing_zero": not missing,
        "failed_cases_zero": not failed_cases,
    }
    goldens = _golden_checks(candidate_rows)
    return {
        "schema": "chat_external_tool_routing_v4_phase3_off_gate_v2",
        "mode": "OFF",
        "passed": all(snapshot_checks.values()) and all(goldens.values()),
        "snapshot_checks": snapshot_checks,
        "golden_checks": goldens,
        "failed_cases": failed_cases,
        "missing_cases": missing,
        "presentation_only_drift_cases": presentation_drift,
        "numeric_token_comparison_policy": {
            "mode": "exact_except_approved_presentation_multiset",
            "case_ids": sorted(APPROVED_PRESENTATION_EXCEPTIONS),
            "backlog": "investigate_numeric_token_nondeterminism",
        },
        "tool_contract_policy": {
            "stable_fields": list(STABLE_TOOL_FIELDS),
            "diagnostic_only_fields": ["cache_hit", "latency_ms", "started_at", "ended_at"],
        },
        "deterministic_route_contracts": EXPECTED_CANDIDATE_ROUTES,
        "cases": cases,
    }


def _parse_inputs(argv: list[str]) -> Inputs:
    if len(argv) != 6:
        raise SystemExit("usage: phase3_off_gate.py BASELINE CANDIDATE COMMIT DIGEST OUTPUT")
    return Inputs(
        baseline_path=Path(argv[1]),
        candidate_path=Path(argv[2]),
        expected_commit=argv[3],
        expected_digest=argv[4],
        output_path=Path(argv[5]),
    )


def main() -> int:
    inputs = _parse_inputs(sys.argv)
    verdict = evaluate(inputs)
    inputs.output_path.write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "passed": verdict["passed"],
                "failed_cases": verdict["failed_cases"],
                "presentation_only_drift_cases": verdict["presentation_only_drift_cases"],
                "snapshot_checks": verdict["snapshot_checks"],
                "golden_checks": verdict["golden_checks"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if verdict["passed"] else 51


if __name__ == "__main__":
    raise SystemExit(main())
