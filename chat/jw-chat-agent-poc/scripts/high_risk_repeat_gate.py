#!/usr/bin/env python3
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


CONTRACT_FIELDS = ("scope", "route", "tools", "tool_contracts")


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = _load_object(path)
    if manifest.get("schema") != "external_tool_routing_v4_high_risk_repeat_manifest_v1":
        raise ValueError(f"unsupported repeat manifest schema: {manifest.get('schema')!r}")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("repeat manifest must contain cases")
    return manifest


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _tool_names(row: dict[str, Any]) -> list[str]:
    raw = row.get("tools_called")
    if not isinstance(raw, list):
        raw = row.get("tool_names")
    return [str(item) for item in raw] if isinstance(raw, list) else []


def _tool_contracts(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = row.get("tool_contracts")
    if not isinstance(raw, list):
        qa_trace = row.get("qa_trace")
        raw = qa_trace.get("tools") if isinstance(qa_trace, dict) else []
    contracts: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        contracts.append(
            {
                key: item.get(key)
                for key in (
                    "name",
                    "status",
                    "row_count",
                    "data_as_of",
                    "cache_hit",
                    "endpoint",
                    "source_epoch",
                    "built_at",
                )
            }
        )
    return contracts


def _normalized_run(row: dict[str, Any], run_number: int) -> dict[str, Any]:
    tools = _tool_names(row)
    contracts = _tool_contracts(row)
    numeric_tokens = row.get("numeric_tokens")
    if not isinstance(numeric_tokens, list):
        numeric_tokens = []
    normalized = {
        "run": run_number,
        "scope": row.get("scope"),
        "route": row.get("route"),
        "tools": tools,
        "tool_contracts": contracts,
        "answer_chars": row.get("answer_chars"),
        "answer_sha256": row.get("answer_sha256"),
        "numeric_tokens": [str(token) for token in numeric_tokens],
    }
    normalized["contract_fingerprint"] = _stable_hash(
        {field: normalized[field] for field in CONTRACT_FIELDS}
    )
    normalized["presentation_fingerprint"] = _stable_hash(
        {
            "answer_chars": normalized["answer_chars"],
            "answer_sha256": normalized["answer_sha256"],
            "numeric_tokens": normalized["numeric_tokens"],
        }
    )
    return normalized


def _rows(payload: dict[str, Any], case_id: str) -> list[dict[str, Any]]:
    cases = payload.get("cases")
    if not isinstance(cases, dict):
        return []
    value = cases.get(case_id)
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _evaluate_surface(
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
    repeat_count: int,
    *,
    enforce_determinism: bool,
    presentation_exception: bool,
) -> dict[str, Any]:
    runs = [_normalized_run(row, index) for index, row in enumerate(rows, start=1)]
    contract_variants = {run["contract_fingerprint"] for run in runs}
    presentation_variants = {run["presentation_fingerprint"] for run in runs}
    failures: list[str] = []
    if len(runs) != repeat_count:
        failures.append(f"repeat_count:{len(runs)}!={repeat_count}")
    if enforce_determinism and len(contract_variants) != 1:
        failures.append(f"contract_variants:{len(contract_variants)}")
    if enforce_determinism and not presentation_exception and len(presentation_variants) != 1:
        failures.append(f"presentation_variants:{len(presentation_variants)}")

    required_tools = {str(name) for name in spec.get("required_tools", [])}
    forbidden_tools = {str(name) for name in spec.get("forbidden_tools", [])}
    required_tokens = {str(token) for token in spec.get("required_numeric_tokens", [])}
    minimum_answer_chars = spec.get("minimum_answer_chars")
    for run in runs:
        tools = set(run["tools"])
        for name in sorted(required_tools - tools):
            failures.append(f"missing_required_tool:{name}")
        for name in sorted(forbidden_tools & tools):
            failures.append(f"forbidden_tool:{name}")
        numeric_tokens = set(run["numeric_tokens"])
        for token in sorted(required_tokens - numeric_tokens):
            failures.append(f"missing_numeric_token:{token}")
        if isinstance(minimum_answer_chars, int):
            actual = run["answer_chars"]
            if not isinstance(actual, int) or actual < minimum_answer_chars:
                failures.append(f"answer_chars_below_minimum:{actual}")

    return {
        "passed": not failures,
        "failures": sorted(set(failures)),
        "variant_count": max(len(contract_variants), len(presentation_variants)),
        "contract_variant_count": len(contract_variants),
        "presentation_variant_count": len(presentation_variants),
        "runs": runs,
    }


def evaluate_repeats(
    manifest: dict[str, Any], baseline: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    repeat_count = int(manifest["repeat_count"])
    approved = set(manifest.get("approved_presentation_exceptions", []))
    results: dict[str, Any] = {}
    repeat_table: list[dict[str, Any]] = []
    base_capture_failures: list[str] = []
    candidate_failures: list[str] = []

    for spec_value in manifest["cases"]:
        if not isinstance(spec_value, dict):
            continue
        spec = spec_value
        case_id = str(spec["case_id"])
        presentation_exception = case_id in approved
        base_result = _evaluate_surface(
            spec,
            _rows(baseline, case_id),
            repeat_count,
            enforce_determinism=False,
            presentation_exception=presentation_exception,
        )
        candidate_result = _evaluate_surface(
            spec,
            _rows(candidate, case_id),
            repeat_count,
            enforce_determinism=True,
            presentation_exception=presentation_exception,
        )
        if len(base_result["runs"]) != repeat_count:
            base_capture_failures.append(case_id)
        if not candidate_result["passed"]:
            candidate_failures.append(case_id)
        results[case_id] = {
            "question": spec.get("question"),
            "risk_classes": spec.get("risk_classes", []),
            "presentation_exception": presentation_exception,
            "baseline": base_result,
            "candidate": candidate_result,
        }
        for surface, result in (("baseline", base_result), ("candidate", candidate_result)):
            for run in result["runs"]:
                repeat_table.append(
                    {
                        "case_id": case_id,
                        "surface": surface,
                        "run": run["run"],
                        "route": run["route"],
                        "tools": run["tools"],
                        "tool_contract_fingerprint": run["contract_fingerprint"],
                        "answer_chars": run["answer_chars"],
                        "presentation_fingerprint": run["presentation_fingerprint"],
                    }
                )

    return {
        "schema": "external_tool_routing_v4_high_risk_repeat_result_v1",
        "passed": not base_capture_failures and not candidate_failures,
        "repeat_count": repeat_count,
        "approved_presentation_exceptions": sorted(approved),
        "base_capture_failures": sorted(base_capture_failures),
        "failed_candidate_cases": sorted(candidate_failures),
        "cases": results,
        "repeat_table": repeat_table,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate v4 high-risk repeat captures")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = evaluate_repeats(
        load_manifest(args.manifest), _load_object(args.baseline), _load_object(args.candidate)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "base_capture_failures": result["base_capture_failures"],
                "failed_candidate_cases": result["failed_candidate_cases"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["passed"] else 51


if __name__ == "__main__":
    raise SystemExit(main())
