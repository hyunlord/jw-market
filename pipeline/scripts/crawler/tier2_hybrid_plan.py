"""Plan tier2_llm_v1 hybrid replay work without writing to the database."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.scripts.crawler.crawl_2tier import _article_json_files
from pipeline.scripts.crawler.tier2_match_score import Tier2Brand, candidate_brands_for_item


@dataclass(frozen=True)
class Tier2HybridTask:
    path: str
    title: str
    mode: str
    candidate_count: int
    candidates: list[dict[str, Any]]


def load_brand_plan(path: Path) -> list[Tier2Brand]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("tier2 brand plan must be a JSON list")
    return [
        Tier2Brand(
            brand_name=str(row["brand_name"]),
            brand_key=str(row.get("brand_key") or row["brand_name"]),
            source=str(row.get("source") or "unknown"),
            atc4_code=row.get("atc4_code"),
            reason=row.get("reason"),
        )
        for row in rows
        if isinstance(row, dict)
    ]


def plan_hybrid_tasks(corpus_dir: Path, brands: list[Tier2Brand]) -> list[Tier2HybridTask]:
    tasks: list[Tier2HybridTask] = []
    for path in _article_json_files(corpus_dir):
        item = json.loads(path.read_text(encoding="utf-8"))
        candidates = candidate_brands_for_item(item, brands)
        if not candidates:
            continue
        mode = "tier2_llm_tagging" if len(candidates) >= 2 else "rule_single_wf196"
        tasks.append(
            Tier2HybridTask(
                path=str(path),
                title=str(item.get("title") or ""),
                mode=mode,
                candidate_count=len(candidates),
                candidates=[
                    {
                        "brand_key": brand.brand_key,
                        "brand_name": brand.brand_name,
                        "source": brand.source,
                        "atc4_code": brand.atc4_code,
                    }
                    for brand in candidates
                ],
            )
        )
    return tasks


def summarize_tasks(tasks: list[Tier2HybridTask]) -> dict[str, Any]:
    by_mode: dict[str, int] = {}
    candidate_pairs = 0
    for task in tasks:
        by_mode[task.mode] = by_mode.get(task.mode, 0) + 1
        candidate_pairs += task.candidate_count
    return {
        "article_tasks": len(tasks),
        "candidate_pairs": candidate_pairs,
        "mode_counts": by_mode,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--brand-plan", type=Path, required=True)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    args = parser.parse_args()

    brands = load_brand_plan(args.brand_plan)
    tasks = plan_hybrid_tasks(args.corpus_dir, brands)
    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.output_jsonl.open("w", encoding="utf-8") as f:
        for task in tasks:
            f.write(json.dumps(task.__dict__, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps(summarize_tasks(tasks), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
