from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Literal

from .config import WORKFLOW_ID, resolve_workflow_rev
from .db import DbConfig
from .market_loader import (
    Agent3MarketLoader,
    canonical_market_content_matches,
    compute_market_input_hash,
    make_market_record,
)
from .market_processing import (
    build_native_market_position,
    build_strategic_inputs,
    profile_only_market_summary,
)
from .market_repository import MarketUnit, StrategicMarketRepository, read_market_units
from .run_full import _run_workflow_with_validation
from .run_source import RunMode, _validate_execution_contract
from .workflow_client import Agent3WorkflowClient


MarketRunMode = Literal["dry-run", "full"]


def load_worklist(path: Path) -> list[MarketUnit]:
    return read_market_units(path)


def run_market_source(
    *,
    worklist: Path,
    mode: MarketRunMode,
    output: Path,
    workflow_rev: int,
    expected_workflow_rev: int,
    environment_mode: str | None,
    top_n: int,
) -> dict[str, Any]:
    _validate_execution_contract(
        workflow_rev=workflow_rev,
        expected_workflow_rev=expected_workflow_rev,
        cli_mode=mode,
        environment_mode=environment_mode,
    )
    print(
        "[agent3-market-preflight] "
        f"workflow_rev={workflow_rev} expected_workflow_rev={expected_workflow_rev} "
        f"mode={mode} environment_mode=unset",
        file=sys.stderr,
        flush=True,
    )
    units = load_worklist(worklist)
    repository = StrategicMarketRepository(DbConfig.from_env())
    loader = Agent3MarketLoader(DbConfig.from_env())
    if mode == "full":
        loader.ensure_table()
        existing = loader.load_existing()
    else:
        existing = {}
    client = Agent3WorkflowClient(workflow_id=WORKFLOW_ID)
    scope_cache: dict[tuple[str, str, str], list[Any]] = {}
    pending = []
    records = []
    counts = {
        "source_units": 0,
        "candidate_units": 0,
        "market_position": 0,
        "workflow_calls": 0,
        "workflow_errors": 0,
        "skipped_same_hash": 0,
        "skipped_same_content": 0,
        "canonical_mismatch": 0,
    }
    for index, unit in enumerate(units, start=1):
        counts["source_units"] += 1
        scope_key = (unit.view_kind, unit.market_id, unit.mart_source)
        scope = scope_cache.get(scope_key)
        if scope is None:
            scope = repository.load_native_scope(unit)
            scope_cache[scope_key] = scope
        profile, primary_candidates = build_strategic_inputs(unit, scope, top_n=top_n)
        input_hash = compute_market_input_hash(
            view_kind=unit.view_kind,
            market_id=unit.market_id,
            brand_key=unit.brand_key,
            source=unit.source,
            profile=profile,
            candidates=primary_candidates,
            workflow_rev=workflow_rev,
        )
        old = existing.get((unit.brand_key, unit.source, unit.market_id))
        if old is not None and old.workflow_rev == workflow_rev and old.input_hash == input_hash:
            counts["skipped_same_hash"] += 1
            records.append(_summary(unit, profile, primary_candidates, old.strength_summary_json, "skipped_same_hash", input_hash))
            continue
        counts["candidate_units"] += int(bool(primary_candidates))
        stored_candidates = primary_candidates
        if primary_candidates and mode == "full":
            workflow_result = _run_workflow_with_validation(
                client=client,
                profile=profile,
                candidates=primary_candidates,
                brand=unit.brand_name,
            )
            counts["workflow_calls"] += workflow_result.workflow_calls
            summary = {
                **workflow_result.summary,
                "source": unit.source,
                "view_kind": unit.view_kind,
                "market_id": unit.market_id,
            }
            status = workflow_result.status
        elif not primary_candidates:
            fallback = build_native_market_position(
                unit,
                scope,
                base_summary=profile_only_market_summary(unit, profile, primary_candidates),
            )
            stored_candidates = [fallback.candidate]
            summary = fallback.summary
            status = "market_position"
            counts["market_position"] += 1
        else:
            summary = profile_only_market_summary(unit, profile, primary_candidates)
            status = "candidate_dry_run"
        record = make_market_record(
            brand_key=unit.brand_key,
            source=unit.source,
            market_id=unit.market_id,
            view_kind=unit.view_kind,
            brand_name=unit.brand_name,
            serving_brand_name=unit.brand_name,
            profile=profile,
            candidates=stored_candidates,
            hash_candidates=primary_candidates,
            summary=summary,
            workflow_id=WORKFLOW_ID,
            workflow_rev=workflow_rev,
            generation_status=status,
        )
        if old is not None and canonical_market_content_matches(old, record):
            counts["skipped_same_content"] += 1
            records.append(_summary(unit, profile, stored_candidates, summary, "skipped_same_content", input_hash))
            continue
        if old is not None:
            counts["canonical_mismatch"] += 1
        if mode == "full":
            pending.append(record)
        records.append(_summary(unit, profile, stored_candidates, summary, status, input_hash))
        print(
            f"[agent3-market] {index}/{len(units)} {unit.view_kind} {unit.market_id} "
            f"{unit.brand_key} source={unit.source} status={status}",
            file=sys.stderr,
            flush=True,
        )
    affected = loader.upsert_many(pending) if mode == "full" else 0
    result = {
        "mode": mode,
        "workflow_id": WORKFLOW_ID,
        "workflow_rev": workflow_rev,
        "expected_workflow_rev": expected_workflow_rev,
        "worklist": str(worklist),
        "affected": affected,
        **counts,
        "estimated_cost_krw": counts["workflow_calls"] * 3.39,
        "records": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return result


def _summary(
    unit: MarketUnit,
    profile: dict[str, Any],
    candidates: list[dict[str, Any]],
    summary: dict[str, Any],
    status: str,
    input_hash: str,
) -> dict[str, Any]:
    return {
        "view_kind": unit.view_kind,
        "market_id": unit.market_id,
        "brand_key": unit.brand_key,
        "brand_name": unit.brand_name,
        "source": unit.source,
        "status": status,
        "input_hash": input_hash,
        "profile": profile,
        "candidates": candidates,
        "summary": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worklist", type=Path, required=True)
    parser.add_argument("--mode", choices=("dry-run", "full"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workflow-rev", type=int)
    parser.add_argument("--expected-workflow-rev", type=int, required=True)
    parser.add_argument("--top-n", type=int, default=5)
    args = parser.parse_args()
    result = run_market_source(
        worklist=args.worklist,
        mode=args.mode,
        output=args.output,
        workflow_rev=resolve_workflow_rev(args.workflow_rev),
        expected_workflow_rev=args.expected_workflow_rev,
        environment_mode=os.environ.get("AGENT3_MODE"),
        top_n=args.top_n,
    )
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
