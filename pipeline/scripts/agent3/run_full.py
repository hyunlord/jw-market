from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Literal

from pipeline.scripts.api.catalog import DISPLAY_BRANDS
from pipeline.scripts.agent3.config import WORKFLOW_ID, resolve_workflow_rev
from pipeline.scripts.agent3.db import DbConfig
from pipeline.scripts.agent3.loader import Agent3Loader, compute_input_hash, make_record
from pipeline.scripts.agent3.profile_provider import build_profile
from pipeline.scripts.agent3.brand_identity import BrandIdentity, serving_brand_names_for_identities
from pipeline.scripts.agent3.repository import Agent3Repository, metric_rows_from_general
from pipeline.scripts.agent3.strength_candidate_extractor import CandidateFloors, extract_strength_candidates
from pipeline.scripts.agent3.summary_postprocess import (
    SummaryValidationError,
    inject_candidate_numbers,
    validate_display_number_narratives,
)
from pipeline.scripts.agent3.workflow_client import Agent3WorkflowClient
BrandSource = Literal["jw25", "strategic_ml", "general_all"]
RunMode = Literal["dry-run", "full"]


def build_agent3_input(profile: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "brand": profile["brand"],
        "profile_summary": profile,
        "strength_candidates": candidates,
    }


def run_full(
    *,
    brand_source: BrandSource,
    mode: RunMode,
    chunk_index: int,
    chunk_size: int,
    explicit_brands: list[str] | None,
    output: Path,
    top_n: int,
    workflow_rev: int,
) -> dict[str, Any]:
    repo = Agent3Repository(DbConfig.from_env())
    loader = Agent3Loader(DbConfig.from_env())
    universe = explicit_brands or _brand_universe(repo, brand_source)
    identities = repo.resolve_brand_identities(universe, _display_aliases_by_name())
    serving_names = serving_brand_names_for_identities(identities)
    chunk = identities[chunk_index * chunk_size : (chunk_index + 1) * chunk_size]
    brand_keys = [identity.brand_key for identity in chunk]
    brand_names = [identity.brand_name for identity in chunk]
    if mode == "full":
        loader.ensure_table()
    existing = loader.load_existing_hashes(brand_keys) if mode == "full" else {}
    general_by_brand = repo.load_general_rows_for_brands(brand_keys)
    strategic_by_brand = repo.load_strategic_rows_for_brands(brand_keys)
    molecule_by_brand = repo.load_molecule_rows_for_brands(brand_names)

    client = Agent3WorkflowClient(workflow_id=WORKFLOW_ID)
    records = []
    pending_records = []
    counts = {"workflow_calls": 0, "skipped_same_hash": 0, "profile_only": 0, "candidate_brands": 0}
    for index, identity in enumerate(chunk, start=1):
        brand = identity.brand_name
        print(
            f"[agent3-full] chunk={chunk_index} {index:04d}/{len(chunk)} {identity.brand_key} {brand}",
            file=sys.stderr,
            flush=True,
        )
        profile = build_profile(
            brand_name=brand,
            general_rows=general_by_brand.get(identity.brand_key, []),
            strategic_rows=strategic_by_brand.get(identity.brand_key, []),
            molecule_rows=molecule_by_brand.get(brand, []),
        )
        candidates = extract_strength_candidates(
            metric_rows_from_general(general_by_brand.get(identity.brand_key, [])),
            floors=CandidateFloors(),
            top_n=top_n,
        )
        if candidates:
            counts["candidate_brands"] += 1
        else:
            counts["profile_only"] += 1
        input_hash = compute_input_hash(profile, candidates, workflow_rev)
        old = existing.get(identity.brand_key)
        if old == (input_hash, workflow_rev):
            counts["skipped_same_hash"] += 1
            records.append(_record_summary(identity, candidates, input_hash, "skipped_same_hash", None, 0))
            continue
        if mode == "full" and candidates:
            summary, meta = client.run(build_agent3_input(profile, candidates))
            summary = inject_candidate_numbers(summary, candidates)
            validation_errors = validate_display_number_narratives(summary, candidates)
            if validation_errors:
                raise SummaryValidationError(brand=brand, errors=validation_errors)
            counts["workflow_calls"] += 1
        else:
            summary = _profile_only_summary(brand, profile, candidates, mode)
            meta = {"workflow_skipped": True, "mode": mode}
        record = make_record(
            brand_key=identity.brand_key,
            brand_name=brand,
            serving_brand_name=serving_names.get(identity.brand_key),
            profile=profile,
            candidates=candidates,
            summary=summary,
            workflow_id=WORKFLOW_ID,
            workflow_rev=workflow_rev,
        )
        if mode == "full":
            pending_records.append(record)
        records.append(_record_summary(identity, candidates, record.input_hash, "ready", meta, 0))
    affected = loader.upsert_many(pending_records, batch_size=200) if mode == "full" else 0
    result = {
        "brand_source": brand_source,
        "mode": mode,
        "chunk_index": chunk_index,
        "chunk_size": chunk_size,
        "universe_count": len(universe),
        "chunk_brand_count": len(chunk),
        "workflow_id": WORKFLOW_ID,
        "workflow_rev": workflow_rev,
        "affected": affected,
        **counts,
        "estimated_cost_krw": counts["workflow_calls"] * 3.39 if mode == "full" else counts["candidate_brands"] * 3.39,
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


def _display_aliases_by_name() -> dict[str, tuple[str, ...]]:
    return {item.brand_name: item.layer3_aliases for item in DISPLAY_BRANDS if item.layer3_aliases}


def _profile_only_summary(brand: str, profile: dict[str, Any], candidates: list[dict[str, Any]], mode: RunMode) -> dict[str, Any]:
    reason = "dry-run: wf316 호출 없이 후보 통계만 산출" if mode == "dry-run" else "strength candidate 0건: wf316 호출 없이 profile-only 저장"
    return {"brand": brand, "profile_display": profile, "strength_items": [], "limitations": [reason], "candidate_count": len(candidates)}


def _record_summary(
    identity: BrandIdentity,
    candidates: list[dict[str, Any]],
    input_hash: str,
    status: str,
    meta: dict[str, Any] | None,
    affected: int,
) -> dict[str, Any]:
    return {
        "brand_key": identity.brand_key,
        "brand": identity.brand_name,
        "candidate_count": len(candidates),
        "low_base_candidates": sum(1 for item in candidates if item.get("low_base")),
        "slices": [str(item["slice"]) for item in candidates],
        "input_hash": input_hash,
        "status": status,
        "affected": affected,
        "meta": meta,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Agent3 over a chunked brand universe.")
    parser.add_argument("--brand-source", choices=["jw25", "strategic_ml", "general_all"], required=True)
    parser.add_argument("--mode", choices=["dry-run", "full"], required=True)
    parser.add_argument("--chunk-index", type=int, default=0)
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument("--brands", help="Comma-separated brand keys/names for bounded sample runs.")
    parser.add_argument("--output", type=Path, default=Path("/tmp/agent3_full.json"))
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--workflow-rev", type=int, help="wf316 revision id to record in input_hash/idempotency.")
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
    result = run_full(
        brand_source=args.brand_source,
        mode=args.mode,
        chunk_index=args.chunk_index,
        chunk_size=args.chunk_size,
        explicit_brands=_parse_brands(args.brands),
        output=args.output,
        top_n=args.top_n,
        workflow_rev=resolve_workflow_rev(args.workflow_rev),
    )
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
