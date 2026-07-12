from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any, Literal

from pipeline.scripts.api.catalog import DISPLAY_BRANDS
from pipeline.scripts.agent3.brand_identity import serving_brand_names_for_identities
from pipeline.scripts.agent3.config import WORKFLOW_ID, resolve_workflow_rev
from pipeline.scripts.agent3.db import DbConfig
from pipeline.scripts.agent3.market_position import build_market_position_fallback
from pipeline.scripts.agent3.repository import Agent3Repository
from pipeline.scripts.agent3.run_full import (
    _display_aliases_by_name,
    _run_workflow_with_validation,
)
from pipeline.scripts.agent3.source_loader import (
    Agent3Source,
    Agent3SourceLoader,
    ExistingAgent3SourceState,
    canonical_content_matches,
    compute_source_input_hash,
    make_source_record,
)
from pipeline.scripts.agent3.source_processing import (
    available_sources_from_general_rows,
    build_source_profile,
    extract_source_candidates,
    profile_only_source_summary,
)
from pipeline.scripts.agent3.workflow_client import Agent3WorkflowClient, WorkflowRetryExhaustedError


BrandSource = Literal["jw25", "strategic_ml", "general_all"]
RunMode = Literal["dry-run", "full", "verify-existing"]
SourceSelection = Literal["all", "iqvia", "ubist"]
WORKFLOW_ERROR_CONSECUTIVE_LIMIT = 3
MIN_SOURCE_UNITS = 35_521
MIN_BRANDS = 24_789


class ExecutionContractError(RuntimeError):
    """Raised before I/O when the declared Agent3 execution contract is inconsistent."""


@dataclass(frozen=True, slots=True)
class SourceCoverage:
    source_units: int
    brands: int
    profile_only: int


def validate_source_coverage(coverage: SourceCoverage) -> None:
    if (
        coverage.source_units < MIN_SOURCE_UNITS
        or coverage.brands < MIN_BRANDS
        or coverage.profile_only != 0
    ):
        raise RuntimeError(
            "Agent3 source coverage gate failed: "
            f"source_units={coverage.source_units}, brands={coverage.brands}, "
            f"profile_only={coverage.profile_only}"
        )


def _validate_execution_contract(
    *,
    workflow_rev: int,
    expected_workflow_rev: int,
    cli_mode: RunMode,
    environment_mode: str | None,
) -> None:
    if workflow_rev != expected_workflow_rev:
        raise ExecutionContractError(
            "Agent3 workflow revision mismatch: "
            f"execution={workflow_rev}, expected={expected_workflow_rev}"
        )
    if environment_mode is not None:
        raise ExecutionContractError(
            "AGENT3_MODE must be unset when --mode is supplied; "
            f"cli={cli_mode!r}, environment={environment_mode!r}"
        )


def run_source(
    *,
    brand_source: BrandSource,
    mode: RunMode,
    source_selection: SourceSelection,
    explicit_brands: list[str] | None,
    output: Path,
    top_n: int,
    workflow_rev: int,
    expected_workflow_rev: int,
    environment_mode: str | None,
) -> dict[str, Any]:
    _validate_execution_contract(
        workflow_rev=workflow_rev,
        expected_workflow_rev=expected_workflow_rev,
        cli_mode=mode,
        environment_mode=environment_mode,
    )
    print(
        "[agent3-preflight] "
        f"workflow_rev={workflow_rev} expected_workflow_rev={expected_workflow_rev} "
        f"mode={mode} environment_mode=unset",
        file=sys.stderr,
        flush=True,
    )
    repo = Agent3Repository(DbConfig.from_env())
    loader = Agent3SourceLoader(DbConfig.from_env())
    if mode == "verify-existing":
        return _verify_existing_market_positions(loader, workflow_rev=workflow_rev, output=output)
    brand_refs = explicit_brands or _brand_universe(repo, brand_source)
    identities = repo.resolve_brand_identities(brand_refs, _display_aliases_by_name())
    serving_names = serving_brand_names_for_identities(identities)
    if mode == "full":
        source_units, brands, profile_only = loader.load_coverage()
        validate_source_coverage(
            SourceCoverage(source_units=source_units, brands=brands, profile_only=profile_only)
        )
        loader.ensure_table()
    client = Agent3WorkflowClient(workflow_id=WORKFLOW_ID)
    records = []
    pending_records = []
    counts = {
        "workflow_calls": 0,
        "profile_only": 0,
        "market_position": 0,
        "skipped_same_hash": 0,
        "skipped_same_content": 0,
        "canonical_mismatch": 0,
        "source_units": 0,
        "workflow_errors": 0,
    }
    consecutive_workflow_errors = 0
    for identity, general_rows, strategic_rows, molecule_rows, market_rows, existing_states in _iter_identity_inputs(
        repo,
        loader,
        identities,
        load_existing=mode == "full",
    ):
        available_sources = set(available_sources_from_general_rows(general_rows))
        for source in (item for item in _selected_sources(source_selection) if item in available_sources):
            old = dict(existing_states).get(source)
            counts["source_units"] += 1
            print(f"[agent3-source] {identity.brand_key} {identity.brand_name} source={source}", file=sys.stderr, flush=True)
            profile = build_source_profile(
                brand_name=identity.brand_name,
                source=source,
                general_rows=general_rows,
                strategic_rows=strategic_rows,
                molecule_rows=molecule_rows,
            )
            primary_candidates = extract_source_candidates(
                source=source,
                general_rows=general_rows,
                market_rows=market_rows,
                top_n=top_n,
            )
            input_hash = compute_source_input_hash(profile, primary_candidates, workflow_rev, source)
            if old is not None and old.input_hash == input_hash and old.workflow_rev == workflow_rev:
                counts["skipped_same_hash"] += 1
                records.append(
                    _record_summary(
                        identity.brand_key,
                        identity.brand_name,
                        source,
                        primary_candidates,
                        input_hash,
                        "skipped_same_hash",
                    )
                )
                continue
            stored_candidates = primary_candidates
            if mode == "full" and primary_candidates:
                try:
                    workflow_result = _run_workflow_with_validation(
                        client=client,
                        profile=profile,
                        candidates=primary_candidates,
                        brand=identity.brand_name,
                    )
                    summary = {**workflow_result.summary, "source": source}
                    status = workflow_result.status
                    counts["workflow_calls"] += workflow_result.workflow_calls
                    consecutive_workflow_errors = 0
                    if workflow_result.status == "validation_isolated":
                        fallback = build_market_position_fallback(
                            brand_key=identity.brand_key,
                            brand_name=identity.brand_name,
                            source=source,
                            profile=profile,
                            base_summary=summary,
                            market_rows=market_rows,
                        )
                        stored_candidates = [fallback.candidate]
                        summary = fallback.summary
                        status = "validation_isolated_market_position"
                        counts["market_position"] += 1
                except WorkflowRetryExhaustedError as exc:
                    counts["workflow_errors"] += 1
                    counts["workflow_calls"] += exc.attempts
                    consecutive_workflow_errors += 1
                    if consecutive_workflow_errors >= WORKFLOW_ERROR_CONSECUTIVE_LIMIT:
                        raise RuntimeError(
                            f"wf316 transport failures reached {consecutive_workflow_errors} consecutive source units"
                        ) from exc
                    summary = {
                        "brand": identity.brand_name,
                        "source": source,
                        "profile_display": profile,
                        "strength_items": [],
                        "limitations": ["wf316 workflow transport failed after retries; stored as source profile-only"],
                        "candidate_count": len(primary_candidates),
                        "unavailable_reason": "workflow_error",
                        "workflow_error": {"attempts": exc.attempts, "last_error": exc.last_error},
                    }
                    fallback = build_market_position_fallback(
                        brand_key=identity.brand_key,
                        brand_name=identity.brand_name,
                        source=source,
                        profile=profile,
                        base_summary=summary,
                        market_rows=market_rows,
                    )
                    stored_candidates = [fallback.candidate]
                    summary = fallback.summary
                    status = "workflow_error_market_position"
                    counts["market_position"] += 1
            elif not primary_candidates:
                base_summary = profile_only_source_summary(
                    brand=identity.brand_name,
                    profile=profile,
                    candidates=primary_candidates,
                    source=source,
                )
                fallback = build_market_position_fallback(
                    brand_key=identity.brand_key,
                    brand_name=identity.brand_name,
                    source=source,
                    profile=profile,
                    base_summary=base_summary,
                    market_rows=market_rows,
                )
                stored_candidates = [fallback.candidate]
                summary = fallback.summary
                status = "market_position"
                counts["market_position"] += 1
            else:
                counts["profile_only"] += 1
                summary = profile_only_source_summary(
                    brand=identity.brand_name,
                    profile=profile,
                    candidates=primary_candidates,
                    source=source,
                )
                status = "profile_only"
            record = make_source_record(
                brand_key=identity.brand_key,
                source=source,
                brand_name=identity.brand_name,
                serving_brand_name=serving_names.get(identity.brand_key),
                profile=profile,
                candidates=stored_candidates,
                summary=summary,
                workflow_id=WORKFLOW_ID,
                workflow_rev=workflow_rev,
                hash_candidates=primary_candidates,
            )
            if old is not None and canonical_content_matches(old, record):
                counts["skipped_same_content"] += 1
                records.append(
                    _record_summary(
                        identity.brand_key,
                        identity.brand_name,
                        source,
                        stored_candidates,
                        record.input_hash,
                        "skipped_same_content",
                    )
                )
                continue
            if old is not None:
                counts["canonical_mismatch"] += 1
            if mode == "full":
                pending_records.append(record)
            records.append(
                _record_summary(
                    identity.brand_key,
                    identity.brand_name,
                    source,
                    stored_candidates,
                    record.input_hash,
                    status,
                )
            )
    affected = loader.upsert_many(pending_records, batch_size=200) if mode == "full" else 0
    result = {
        "brand_source": brand_source,
        "mode": mode,
        "source_selection": source_selection,
        "workflow_id": WORKFLOW_ID,
        "workflow_rev": workflow_rev,
        "affected": affected,
        **counts,
        "estimated_cost_krw": counts["workflow_calls"] * 3.39,
        "records": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return result


def _brand_universe(repo: Agent3Repository, source: BrandSource) -> list[str]:
    match source:
        case "jw25":
            return [item.brand_name for item in DISPLAY_BRANDS]
        case "strategic_ml" | "general_all":
            return repo.load_brand_universe(source)


def _verify_existing_market_positions(
    loader: Agent3SourceLoader,
    *,
    workflow_rev: int,
    output: Path,
) -> dict[str, Any]:
    counts = {
        "verified_rows": 0,
        "skipped_same_content": 0,
        "canonical_mismatch": 0,
        "hash_changed": 0,
        "workflow_calls": 0,
        "affected": 0,
    }
    mismatches: list[dict[str, str]] = []
    for state in loader.iter_market_position_states():
        counts["verified_rows"] += 1
        record = make_source_record(
            brand_key=state.brand_key,
            source=state.source,
            brand_name=state.brand_name,
            serving_brand_name=state.serving_brand_name,
            profile=state.profile_json,
            candidates=state.strength_candidates_json,
            summary=state.strength_summary_json,
            workflow_id=state.workflow_id,
            workflow_rev=workflow_rev,
            hash_candidates=[],
        )
        old = ExistingAgent3SourceState(
            input_hash=state.input_hash,
            workflow_rev=state.workflow_rev,
            profile_json=state.profile_json,
            strength_candidates_json=state.strength_candidates_json,
            strength_summary_json=state.strength_summary_json,
        )
        if canonical_content_matches(old, record):
            counts["skipped_same_content"] += 1
        else:
            counts["canonical_mismatch"] += 1
            mismatches.append({"brand_key": state.brand_key, "source": state.source})
        if state.input_hash != record.input_hash:
            counts["hash_changed"] += 1
    result = {
        "mode": "verify-existing",
        "workflow_rev": workflow_rev,
        **counts,
        "mismatches": mismatches,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return result


def _iter_identity_inputs(
    repo: Agent3Repository,
    loader: Agent3SourceLoader,
    identities: list[Any],
    *,
    load_existing: bool,
    batch_size: int = 100,
):
    for offset in range(0, len(identities), batch_size):
        batch = identities[offset : offset + batch_size]
        brand_keys = [identity.brand_key for identity in batch]
        brand_names = [identity.brand_name for identity in batch]
        general_by_brand = repo.load_general_rows_for_brands(brand_keys)
        strategic_by_brand = repo.load_strategic_rows_for_brands(brand_keys)
        molecule_by_brand = repo.load_molecule_rows_for_brands(brand_names)
        market_rows = repo.load_market_metric_rows(
            [row for rows in general_by_brand.values() for row in rows]
        )
        existing = loader.load_existing_hashes(brand_keys) if load_existing else {}
        for identity in batch:
            states = [
                (source, existing.get((identity.brand_key, source)))
                for source in ("iqvia", "ubist")
            ]
            # One identity can have two source rows; callers select the matching state.
            yield (
                identity,
                general_by_brand.get(identity.brand_key, []),
                strategic_by_brand.get(identity.brand_key, []),
                molecule_by_brand.get(identity.brand_name, []),
                market_rows,
                states,
            )


def _selected_sources(source_selection: SourceSelection) -> tuple[Agent3Source, ...]:
    match source_selection:
        case "all":
            return ("iqvia", "ubist")
        case "iqvia":
            return ("iqvia",)
        case "ubist":
            return ("ubist",)


def _record_summary(
    brand_key: str,
    brand_name: str,
    source: Agent3Source,
    candidates: list[dict[str, Any]],
    input_hash: str,
    status: str,
) -> dict[str, Any]:
    return {
        "brand_key": brand_key,
        "brand": brand_name,
        "source": source,
        "candidate_count": len(candidates),
        "slices": [str(item["slice"]) for item in candidates],
        "input_hash": input_hash,
        "status": status,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Agent3 source-level brand strength.")
    parser.add_argument("--brand-source", choices=["jw25", "strategic_ml", "general_all"], required=True)
    parser.add_argument("--mode", choices=["dry-run", "full", "verify-existing"], required=True)
    parser.add_argument("--source", choices=["all", "iqvia", "ubist"], default="all")
    parser.add_argument("--brands", help="Comma-separated brand keys/names for bounded source-level runs.")
    parser.add_argument("--output", type=Path, default=Path("/tmp/agent3_source.json"))
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--workflow-rev", type=int, help="wf316 revision id to record in source input_hash/idempotency.")
    parser.add_argument(
        "--expected-workflow-rev",
        type=int,
        required=True,
        help="Required deployment pin; execution aborts before I/O when it differs from --workflow-rev/AGENT3_WORKFLOW_REV.",
    )
    return parser.parse_args()


def _parse_brands(value: str | None) -> list[str] | None:
    if value is None:
        return None
    brands = [item.strip() for item in value.split(",") if item.strip()]
    if not brands:
        raise SystemExit("--brands must contain at least one non-empty brand")
    return brands


def main() -> int:
    args = _parse_args()
    result = run_source(
        brand_source=args.brand_source,
        mode=args.mode,
        source_selection=args.source,
        explicit_brands=_parse_brands(args.brands),
        output=args.output,
        top_n=args.top_n,
        workflow_rev=resolve_workflow_rev(args.workflow_rev),
        expected_workflow_rev=args.expected_workflow_rev,
        environment_mode=os.environ.get("AGENT3_MODE"),
    )
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
