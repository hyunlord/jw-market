from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Literal

from pipeline.scripts.api.catalog import DISPLAY_BRANDS
from pipeline.scripts.agent3.db import DbConfig
from pipeline.scripts.agent3.loader import Agent3Loader, compute_input_hash, make_record
from pipeline.scripts.agent3.profile_provider import build_profile
from pipeline.scripts.agent3.repository import Agent3Repository, metric_rows_from_general
from pipeline.scripts.agent3.strength_candidate_extractor import CandidateFloors, extract_strength_candidates
from pipeline.scripts.agent3.workflow_client import Agent3WorkflowClient


WORKFLOW_ID = 316
WORKFLOW_REV = 5356
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
    output: Path,
    top_n: int,
) -> dict[str, Any]:
    repo = Agent3Repository(DbConfig.from_env())
    loader = Agent3Loader(DbConfig.from_env())
    universe = _brand_universe(repo, brand_source)
    brands = universe[chunk_index * chunk_size : (chunk_index + 1) * chunk_size]
    if mode == "full":
        loader.ensure_table()
    existing = loader.load_existing_hashes(brands) if mode == "full" else {}
    general_by_brand = repo.load_general_rows_for_brands(brands)
    strategic_by_brand = repo.load_strategic_rows_for_brands(brands)
    molecule_by_brand = repo.load_molecule_rows_for_brands(brands)

    client = Agent3WorkflowClient(workflow_id=WORKFLOW_ID)
    records = []
    pending_records = []
    counts = {"workflow_calls": 0, "skipped_same_hash": 0, "profile_only": 0, "candidate_brands": 0}
    for index, brand in enumerate(brands, start=1):
        print(f"[agent3-full] chunk={chunk_index} {index:04d}/{len(brands)} {brand}", file=sys.stderr, flush=True)
        profile = build_profile(
            brand_name=brand,
            general_rows=general_by_brand.get(brand, []),
            strategic_rows=strategic_by_brand.get(brand, []),
            molecule_rows=molecule_by_brand.get(brand, []),
        )
        candidates = extract_strength_candidates(
            metric_rows_from_general(general_by_brand.get(brand, [])),
            floors=CandidateFloors(),
            top_n=top_n,
        )
        if candidates:
            counts["candidate_brands"] += 1
        else:
            counts["profile_only"] += 1
        input_hash = compute_input_hash(profile, candidates, WORKFLOW_REV)
        old = existing.get(brand)
        if old == (input_hash, WORKFLOW_REV):
            counts["skipped_same_hash"] += 1
            records.append(_record_summary(brand, candidates, input_hash, "skipped_same_hash", None, 0))
            continue
        if mode == "full" and candidates:
            summary, meta = client.run(build_agent3_input(profile, candidates))
            counts["workflow_calls"] += 1
        else:
            summary = _profile_only_summary(brand, profile, candidates, mode)
            meta = {"workflow_skipped": True, "mode": mode}
        record = make_record(
            brand_name=brand,
            profile=profile,
            candidates=candidates,
            summary=summary,
            workflow_id=WORKFLOW_ID,
            workflow_rev=WORKFLOW_REV,
        )
        if mode == "full":
            pending_records.append(record)
        records.append(_record_summary(brand, candidates, record.input_hash, "ready", meta, 0))
    affected = loader.upsert_many(pending_records, batch_size=200) if mode == "full" else 0
    result = {
        "brand_source": brand_source,
        "mode": mode,
        "chunk_index": chunk_index,
        "chunk_size": chunk_size,
        "universe_count": len(universe),
        "chunk_brand_count": len(brands),
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


def _profile_only_summary(brand: str, profile: dict[str, Any], candidates: list[dict[str, Any]], mode: RunMode) -> dict[str, Any]:
    reason = "dry-run: wf316 호출 없이 후보 통계만 산출" if mode == "dry-run" else "strength candidate 0건: wf316 호출 없이 profile-only 저장"
    return {"brand": brand, "profile_display": profile, "strength_items": [], "limitations": [reason], "candidate_count": len(candidates)}


def _record_summary(
    brand: str,
    candidates: list[dict[str, Any]],
    input_hash: str,
    status: str,
    meta: dict[str, Any] | None,
    affected: int,
) -> dict[str, Any]:
    return {
        "brand": brand,
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
    parser.add_argument("--output", type=Path, default=Path("/tmp/agent3_full.json"))
    parser.add_argument("--top-n", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = run_full(
        brand_source=args.brand_source,
        mode=args.mode,
        chunk_index=args.chunk_index,
        chunk_size=args.chunk_size,
        output=args.output,
        top_n=args.top_n,
    )
    print(json.dumps({key: value for key, value in result.items() if key != "records"}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
