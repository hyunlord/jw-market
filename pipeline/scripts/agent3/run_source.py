from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Literal

from pipeline.scripts.api.catalog import DISPLAY_BRANDS
from pipeline.scripts.agent3.brand_identity import serving_brand_names_for_identities
from pipeline.scripts.agent3.config import WORKFLOW_ID, resolve_workflow_rev
from pipeline.scripts.agent3.db import DbConfig
from pipeline.scripts.agent3.repository import Agent3Repository
from pipeline.scripts.agent3.run_full import (
    _display_aliases_by_name,
    _run_workflow_with_validation,
)
from pipeline.scripts.agent3.source_loader import Agent3Source, Agent3SourceLoader, compute_source_input_hash, make_source_record
from pipeline.scripts.agent3.source_processing import (
    available_sources_from_general_rows,
    build_source_profile,
    extract_source_candidates,
    profile_only_source_summary,
)
from pipeline.scripts.agent3.workflow_client import Agent3WorkflowClient, WorkflowRetryExhaustedError


BrandSource = Literal["jw25", "strategic_ml", "general_all"]
RunMode = Literal["dry-run", "full"]
SourceSelection = Literal["all", "iqvia", "ubist"]
WORKFLOW_ERROR_CONSECUTIVE_LIMIT = 3


def run_source(
    *,
    brand_source: BrandSource,
    mode: RunMode,
    source_selection: SourceSelection,
    explicit_brands: list[str] | None,
    output: Path,
    top_n: int,
    workflow_rev: int,
) -> dict[str, Any]:
    repo = Agent3Repository(DbConfig.from_env())
    loader = Agent3SourceLoader(DbConfig.from_env())
    brand_refs = explicit_brands or _brand_universe(repo, brand_source)
    identities = repo.resolve_brand_identities(brand_refs, _display_aliases_by_name())
    serving_names = serving_brand_names_for_identities(identities)
    brand_keys = [identity.brand_key for identity in identities]
    brand_names = [identity.brand_name for identity in identities]
    if mode == "full":
        loader.ensure_table()
    existing = loader.load_existing_hashes(brand_keys) if mode == "full" else {}
    general_by_brand = repo.load_general_rows_for_brands(brand_keys)
    strategic_by_brand = repo.load_strategic_rows_for_brands(brand_keys)
    molecule_by_brand = repo.load_molecule_rows_for_brands(brand_names)
    client = Agent3WorkflowClient(workflow_id=WORKFLOW_ID)
    records = []
    pending_records = []
    counts = {"workflow_calls": 0, "profile_only": 0, "skipped_same_hash": 0, "source_units": 0, "workflow_errors": 0}
    consecutive_workflow_errors = 0
    for identity in identities:
        general_rows = general_by_brand.get(identity.brand_key, [])
        available_sources = set(available_sources_from_general_rows(general_rows))
        for source in (item for item in _selected_sources(source_selection) if item in available_sources):
            counts["source_units"] += 1
            print(f"[agent3-source] {identity.brand_key} {identity.brand_name} source={source}", file=sys.stderr, flush=True)
            profile = build_source_profile(
                brand_name=identity.brand_name,
                source=source,
                general_rows=general_rows,
                strategic_rows=strategic_by_brand.get(identity.brand_key, []),
                molecule_rows=molecule_by_brand.get(identity.brand_name, []),
            )
            candidates = extract_source_candidates(
                source=source,
                general_rows=general_rows,
                top_n=top_n,
            )
            input_hash = compute_source_input_hash(profile, candidates, workflow_rev, source)
            old = existing.get((identity.brand_key, source))
            if old is not None and old.input_hash == input_hash and old.workflow_rev == workflow_rev:
                counts["skipped_same_hash"] += 1
                records.append(_record_summary(identity.brand_key, identity.brand_name, source, candidates, input_hash, "skipped_same_hash"))
                continue
            if mode == "full" and candidates:
                try:
                    workflow_result = _run_workflow_with_validation(
                        client=client,
                        profile=profile,
                        candidates=candidates,
                        brand=identity.brand_name,
                    )
                    summary = {**workflow_result.summary, "source": source}
                    status = workflow_result.status
                    counts["workflow_calls"] += workflow_result.workflow_calls
                    consecutive_workflow_errors = 0
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
                        "candidate_count": len(candidates),
                        "unavailable_reason": "workflow_error",
                        "workflow_error": {"attempts": exc.attempts, "last_error": exc.last_error},
                    }
                    status = "workflow_error_isolated"
            else:
                counts["profile_only"] += 1
                summary = profile_only_source_summary(
                    brand=identity.brand_name,
                    profile=profile,
                    candidates=candidates,
                    source=source,
                )
                status = "profile_only"
            record = make_source_record(
                brand_key=identity.brand_key,
                source=source,
                brand_name=identity.brand_name,
                serving_brand_name=serving_names.get(identity.brand_key),
                profile=profile,
                candidates=candidates,
                summary=summary,
                workflow_id=WORKFLOW_ID,
                workflow_rev=workflow_rev,
            )
            if mode == "full":
                pending_records.append(record)
            records.append(_record_summary(identity.brand_key, identity.brand_name, source, candidates, record.input_hash, status))
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
    parser.add_argument("--mode", choices=["dry-run", "full"], required=True)
    parser.add_argument("--source", choices=["all", "iqvia", "ubist"], default="all")
    parser.add_argument("--brands", help="Comma-separated brand keys/names for bounded source-level runs.")
    parser.add_argument("--output", type=Path, default=Path("/tmp/agent3_source.json"))
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--workflow-rev", type=int, help="wf316 revision id to record in source input_hash/idempotency.")
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
    )
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
