from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from pipeline.scripts.api.catalog import DISPLAY_BRANDS
from pipeline.scripts.agent3.config import WORKFLOW_ID, resolve_workflow_rev
from pipeline.scripts.agent3.db import DbConfig
from pipeline.scripts.agent3.loader import Agent3Loader, make_record
from pipeline.scripts.agent3.profile_provider import build_profile
from pipeline.scripts.agent3.repository import Agent3Repository, metric_rows_from_general
from pipeline.scripts.agent3.strength_candidate_extractor import CandidateFloors, extract_strength_candidates
from pipeline.scripts.agent3.summary_postprocess import (
    SummaryValidationError,
    inject_candidate_numbers,
    validate_display_number_narratives,
)
from pipeline.scripts.agent3.workflow_client import Agent3WorkflowClient


def build_agent3_input(profile: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "brand": profile["brand"],
        "profile_summary": profile,
        "strength_candidates": candidates,
    }


def run_pilot(*, apply: bool, output: Path, top_n: int, skip_workflow: bool = False) -> dict[str, Any]:
    repo = Agent3Repository(DbConfig.from_env())
    loader = Agent3Loader(DbConfig.from_env())
    client = Agent3WorkflowClient(workflow_id=WORKFLOW_ID)
    brands = [item.brand_name for item in DISPLAY_BRANDS]
    if len(brands) != 25:
        raise RuntimeError(f"JW25 catalog guard failed: expected 25 brands, got {len(brands)}")
    if apply:
        loader.ensure_table()
    general_by_brand = repo.load_general_rows_for_brands(brands)
    strategic_by_brand = repo.load_strategic_rows_for_brands(brands)
    molecule_by_brand = repo.load_molecule_rows_for_brands(brands)

    records: list[dict[str, Any]] = []
    wf_calls = 0
    workflow_rev = resolve_workflow_rev()
    for index, brand in enumerate(brands, start=1):
        print(f"[agent3] {index:02d}/{len(brands)} {brand}", file=sys.stderr, flush=True)
        general_rows = general_by_brand.get(brand, [])
        strategic_rows = strategic_by_brand.get(brand, [])
        molecule_rows = molecule_by_brand.get(brand, [])
        profile = build_profile(
            brand_name=brand,
            general_rows=general_rows,
            strategic_rows=strategic_rows,
            molecule_rows=molecule_rows,
        )
        candidates = extract_strength_candidates(
            metric_rows_from_general(general_rows),
            floors=CandidateFloors(),
            top_n=top_n,
        )
        if candidates and not skip_workflow:
            summary, meta = client.run(build_agent3_input(profile, candidates))
            summary = inject_candidate_numbers(summary, candidates)
            validation_errors = validate_display_number_narratives(summary, candidates)
            if validation_errors:
                raise SummaryValidationError(brand=brand, errors=validation_errors)
            wf_calls += 1
        else:
            summary = {
                "brand": brand,
                "profile_display": profile,
                "strength_items": [],
                "limitations": [
                    "strength candidate 0건: wf316 호출 없이 profile-only 저장"
                    if not candidates
                    else "skip_workflow=True: wf316 호출 없이 dry-run summary 저장"
                ],
            }
            meta = {"skipped_workflow": True, "skip_workflow": skip_workflow}
        record = make_record(
            brand_name=brand,
            profile=profile,
            candidates=candidates,
            summary=summary,
            workflow_id=WORKFLOW_ID,
            workflow_rev=workflow_rev,
        )
        affected = loader.upsert(record) if apply else 0
        records.append(
            {
                "brand": brand,
                "candidate_count": len(candidates),
                "workflow_called": bool(candidates),
                "affected": affected,
                "input_hash": record.input_hash,
                "candidates": candidates,
                "summary": summary,
                "meta": meta,
            }
        )
    result = {
        "apply": apply,
        "brand_count": len(brands),
        "workflow_calls": wf_calls,
        "workflow_id": WORKFLOW_ID,
        "workflow_rev": workflow_rev,
        "skip_workflow": skip_workflow,
        "records": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Agent3 JW25 pilot into the new agent3 table only.")
    parser.add_argument("--apply", action="store_true", help="Create/upsert the new agent3_brand_strength table.")
    parser.add_argument("--skip-workflow", action="store_true", help="Do not call wf316; useful for DB/profile/candidate dry-runs.")
    parser.add_argument("--output", type=Path, default=Path("/tmp/agent3_jw25_pilot.json"))
    parser.add_argument("--top-n", type=int, default=5)
    args = parser.parse_args()
    result = run_pilot(apply=args.apply, output=args.output, top_n=args.top_n, skip_workflow=args.skip_workflow)
    print(json.dumps({k: v for k, v in result.items() if k != "records"}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
