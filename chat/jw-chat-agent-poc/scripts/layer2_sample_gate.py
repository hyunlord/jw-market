#!/usr/bin/env python3
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any, Iterable


def _case_rows(population: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for raw in population:
        case_id = str(raw.get("case_id") or "").strip()
        category = str(raw.get("category") or "").strip()
        if not case_id or not category:
            raise ValueError("every Layer 2 case requires case_id and category")
        if case_id in indexed:
            raise ValueError(f"duplicate Layer 2 case_id: {case_id}")
        indexed[case_id] = {"case_id": case_id, "category": category}
    if not indexed:
        raise ValueError("Layer 2 population must not be empty")
    return [indexed[case_id] for case_id in sorted(indexed)]


def select_cases(population: Iterable[dict[str, Any]], *, seed: str) -> list[str]:
    rows = _case_rows(population)
    sample_size = min(len(rows), max(3, math.ceil(len(rows) / 10)))
    ranked = sorted(
        rows,
        key=lambda row: (
            sha256(f"{seed}\0{row['case_id']}".encode()).hexdigest(),
            row["case_id"],
        ),
    )
    return sorted(row["case_id"] for row in ranked[:sample_size])


def evaluate_layer2_round(
    *,
    population: Iterable[dict[str, Any]],
    results: dict[str, Any],
    seed: str,
    round_id: str,
    previous_ledger: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = _case_rows(population)
    population_ids = {row["case_id"] for row in rows}
    selected = select_cases(rows, seed=seed)
    missing = sorted(case_id for case_id in selected if case_id not in results)
    failed = sorted(
        case_id
        for case_id in selected
        if case_id in results
        and not (isinstance(results[case_id], dict) and results[case_id].get("passed") is True)
    )

    prior = previous_ledger or {}
    prior_rounds = [row for row in prior.get("rounds", []) if isinstance(row, dict)]
    rounds = [
        *prior_rounds,
        {"round_id": round_id, "seed": seed, "selected_case_ids": selected},
    ]
    recent_rounds = rounds[-10:]
    recently_selected = {
        str(case_id)
        for row in recent_rounds
        for case_id in row.get("selected_case_ids", [])
    }
    permanent_layer1 = sorted(
        {
            *(str(case_id) for case_id in prior.get("permanent_layer1", [])),
            *failed,
            *missing,
        }
    )
    ledger = {
        "schema": "external_tool_layer2_sampling_ledger_v1",
        "rounds": rounds,
        "permanent_layer1": permanent_layer1,
        "never_selected_last_10": sorted(population_ids - recently_selected),
    }
    report = {
        "schema": "external_tool_layer2_sample_result_v1",
        "passed": not failed and not missing,
        "round_id": round_id,
        "seed": seed,
        "population_size": len(rows),
        "sample_size": len(selected),
        "selected_case_ids": selected,
        "failed_case_ids": failed,
        "missing_result_case_ids": missing,
        "promoted_to_layer1": sorted(set(failed) | set(missing)),
    }
    return report, ledger


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate verification-v2 Layer 2 sampling")
    parser.add_argument("population", type=Path)
    parser.add_argument("results", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("ledger_output", type=Path)
    parser.add_argument("--previous-ledger", type=Path)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--round-id", required=True)
    args = parser.parse_args()

    population_payload = _load(args.population)
    population = population_payload.get("cases") if isinstance(population_payload, dict) else None
    if not isinstance(population, list):
        raise ValueError("population JSON must contain a cases list")
    results_payload = _load(args.results)
    results = results_payload.get("cases") if isinstance(results_payload, dict) else None
    if not isinstance(results, dict):
        raise ValueError("results JSON must contain a cases object")
    previous = _load(args.previous_ledger) if args.previous_ledger else None
    report, ledger = evaluate_layer2_round(
        population=population,
        results=results,
        seed=args.seed,
        round_id=args.round_id,
        previous_ledger=previous,
    )
    for path, payload in ((args.output, report), (args.ledger_output, ledger)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "selected": report["selected_case_ids"]}, ensure_ascii=False))
    return 0 if report["passed"] else 52


if __name__ == "__main__":
    raise SystemExit(main())
