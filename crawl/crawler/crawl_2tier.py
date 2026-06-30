"""Two-tier news crawl entrypoint.

Tier1 delegates to the existing JW crawl/classification flow. Tier2 builds a
rolling general-brand universe, crawls exact brand-name keywords, maps articles
by exact match, and scores them with deterministic zero-LLM rules.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from tier2_catalog import (
    brands_for_weekday,
    load_jw_brand_names,
    load_metric_rows_from_db,
    select_tier2_brands,
    stable_weekday_slice,
)
from tier2_match_score import Tier2Brand, build_tier2_matches


CRAWLER_DIR = Path(__file__).resolve().parent
CRAWL_ROOT = CRAWLER_DIR.parent
EXCLUDED_TIER2_SITES = frozenset({"메디칼타임즈"})
DEFAULT_TIER2_DAYS = 7


def _import_crawler() -> Any:
    if str(CRAWLER_DIR) not in sys.path:
        sys.path.insert(0, str(CRAWLER_DIR))
    import crawl_news_v2

    return crawl_news_v2


def _today_weekday() -> int:
    return datetime.now().weekday()


def _brand_from_dict(row: dict[str, Any]) -> Tier2Brand:
    return Tier2Brand(
        brand_name=str(row["brand_name"]),
        brand_key=str(row.get("brand_key") or row["brand_name"]),
        source=str(row.get("source") or "unknown"),
        atc4_code=row.get("atc4_code"),
        reason=row.get("reason"),
    )


def load_tier2_brands(args: argparse.Namespace) -> list[Tier2Brand]:
    if args.brand_file:
        rows = json.loads(Path(args.brand_file).read_text(encoding="utf-8"))
        brands = [_brand_from_dict(row) for row in rows]
    else:
        metric_rows = load_metric_rows_from_db(
            db_host=args.db_host,
            db_port=args.db_port,
            db_user=args.db_user,
            db_password=args.db_password,
            db_name=args.db_name,
        )
        brands = select_tier2_brands(
            metric_rows,
            sales_threshold_krw=args.sales_threshold_krw,
            recent_new_months=args.recent_new_months,
            recent_new_min_sales_krw=args.recent_new_min_sales_krw,
            jw_brand_names=load_jw_brand_names(Path(args.jw_catalog)) if args.jw_catalog else set(),
        )
    if args.weekday_slice is not None:
        brands = brands_for_weekday(brands, args.weekday_slice)
    if args.limit_brands:
        brands = brands[: args.limit_brands]
    return brands


def write_brand_plan(brands: list[Tier2Brand], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            **brand.__dict__,
            "weekday_slice": stable_weekday_slice(brand.brand_key),
        }
        for brand in brands
    ]
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def tier2_sites(selected_sites: str | None) -> list[str]:
    crawl_news_v2 = _import_crawler()
    available = [site for site in crawl_news_v2.SITE_CONFIGS if site not in EXCLUDED_TIER2_SITES]
    if not selected_sites:
        return available
    requested = [site.strip() for site in selected_sites.split(",") if site.strip()]
    return [site for site in requested if site in available]


def run_tier2_crawl(args: argparse.Namespace, brands: list[Tier2Brand]) -> int:
    crawl_news_v2 = _import_crawler()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    keywords = [brand.brand_name for brand in brands]
    contexts = {
        brand.brand_name: [
            {
                "tier": 2,
                "brand_key": brand.brand_key,
                "source": brand.source,
                "matched_keywords": [brand.brand_name],
            }
        ]
        for brand in brands
    }
    return int(
        crawl_news_v2.crawl_once(
            months=None,
            days=args.days,
            output_dir=str(output_dir),
            max_pages_per_site=args.max_pages_per_site,
            max_links_per_page=args.max_links_per_page,
            delay_sec=args.delay_sec,
            sites=tier2_sites(args.sites),
            keywords=keywords,
            history_file=str(output_dir / "scraped_urls.txt"),
            continue_listing_after_old_page=False,
            skip_similar_merge=args.no_similar_merge,
            unique_json_per_url=args.unique_json_per_url,
            keyword_contexts=contexts,
            max_articles=args.max_articles or None,
        )
    )


def _article_json_files(input_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in input_dir.rglob("*.json")
        if path.name not in {"crawl_report.json", "tier2_brand_plan.json"}
        and not path.name.endswith("_report.json")
    )


def score_tier2_corpus(input_dir: Path, output_dir: Path, brands: list[Tier2Brand]) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    by_keyword = {brand.brand_name.casefold(): [brand] for brand in brands}
    total = 0
    matched = 0
    for path in _article_json_files(input_dir):
        item = json.loads(path.read_text(encoding="utf-8"))
        keyword = str(item.get("search_keyword") or "").casefold()
        candidate_brands = by_keyword.get(keyword, brands)
        matches = build_tier2_matches(item, candidate_brands)
        item["matches"] = matches
        item["tier"] = 2
        item["processed_by"] = "tier2_exact_rule_v1"
        item["source_path"] = str(path)
        target = output_dir / f"{path.stem}_scored.json"
        target.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        total += 1
        if matches:
            matched += 1
    return {
        "input_json": total,
        "matched_json": matched,
        "output_dir": str(output_dir),
        "processor": "tier2_exact_rule_v1",
    }


def run_tier1_existing_flow(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(CRAWLER_DIR / "crawl_news_full_orchestrator.py"),
        "--profile-dir",
        args.drug_profile_dir,
        "--output-base-dir",
        args.output_dir,
        "--months",
        str(args.months),
        "--delay-sec",
        str(args.delay_sec),
        "--concurrent-sites",
        str(args.concurrent_sites),
    ]
    if args.sites:
        command.extend(["--sites", args.sites])
    if args.max_articles:
        command.extend(["--max-articles", str(args.max_articles)])
    completed = subprocess.run(command, check=False)
    return int(completed.returncode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=("1", "2"), required=True)
    parser.add_argument("--dry-run", action="store_true", help="Plan only; do not crawl, score, load, or delete.")
    parser.add_argument("--run-crawl", action="store_true", help="Actually perform crawl. Keep off for planning.")
    parser.add_argument("--score", action="store_true", help="Score Tier2 crawled JSON with exact-match rules.")
    parser.add_argument("--output-dir", default="/tmp/jw-news-crawl")
    parser.add_argument("--processed-dir", default="/tmp/jw-news-crawl-processed")
    parser.add_argument("--brand-plan-output", type=Path, default=Path("/tmp/tier2_brand_plan.json"))
    parser.add_argument("--brand-file")
    parser.add_argument("--jw-catalog", default=str(CRAWL_ROOT / "config" / "_catalog.json"))
    parser.add_argument("--weekday-slice", type=int, choices=range(7), default=_today_weekday())
    parser.add_argument("--limit-brands", type=int, default=0)
    parser.add_argument("--sales-threshold-krw", type=int, default=3_000_000_000)
    parser.add_argument("--recent-new-months", type=int, default=6)
    parser.add_argument("--recent-new-min-sales-krw", type=int, default=100_000_000)
    parser.add_argument("--days", type=int, default=DEFAULT_TIER2_DAYS)
    parser.add_argument("--months", type=int, default=60)
    parser.add_argument("--sites")
    parser.add_argument("--max-pages-per-site", type=int, default=3)
    parser.add_argument("--max-links-per-page", type=int, default=80)
    parser.add_argument("--max-articles", type=int, default=0)
    parser.add_argument("--delay-sec", type=float, default=2.0)
    parser.add_argument("--concurrent-sites", type=int, default=4)
    parser.add_argument("--no-similar-merge", action="store_true")
    parser.add_argument("--unique-json-per-url", action="store_true")
    parser.add_argument("--drug-profile-dir", default=str(CRAWL_ROOT / "config" / "drug_profiles"))
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("DB_PORT", "3306")))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "jw_mart_d1_stage_20260625_173115"))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "root"))
    parser.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.tier == "1":
        if args.dry_run or not args.run_crawl:
            print(json.dumps({"tier": 1, "mode": "existing_wf196_flow", "planned": True}, ensure_ascii=False))
            return 0
        return run_tier1_existing_flow(args)

    brands = load_tier2_brands(args)
    write_brand_plan(brands, args.brand_plan_output)
    summary: dict[str, Any] = {
        "tier": 2,
        "brand_count": len(brands),
        "weekday_slice": args.weekday_slice,
        "excluded_sites": sorted(EXCLUDED_TIER2_SITES),
        "site_count": len(tier2_sites(args.sites)),
        "brand_plan_output": str(args.brand_plan_output),
        "llm_calls": 0,
    }
    if args.dry_run or not args.run_crawl:
        summary["planned"] = True
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    summary["saved_articles"] = run_tier2_crawl(args, brands)
    if args.score:
        summary["score_summary"] = score_tier2_corpus(
            Path(args.output_dir),
            Path(args.processed_dir),
            brands,
        )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
