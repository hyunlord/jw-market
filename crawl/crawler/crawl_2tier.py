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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        brands = brands_for_weekday(brands, args.weekday_slice, modulo=args.slice_mod)
    if args.limit_brands:
        brands = brands[: args.limit_brands]
    return brands


def write_brand_plan(brands: list[Tier2Brand], output: Path, *, slice_mod: int = 7) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            **brand.__dict__,
            "weekday_slice": stable_weekday_slice(brand.brand_key),
            "slice_mod": slice_mod,
            "slice_index": stable_weekday_slice(brand.brand_key, modulo=slice_mod),
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


def effective_tier2_concurrent_sites(args: argparse.Namespace) -> int:
    return max(1, int(args.tier2_concurrent_sites))


def _seed_site_history(parent_history: Path, site_history: Path) -> None:
    site_history.parent.mkdir(parents=True, exist_ok=True)
    if parent_history.exists():
        site_history.write_text(parent_history.read_text(encoding="utf-8"), encoding="utf-8")


def _run_tier2_site(
    args: argparse.Namespace,
    site: str,
    keywords: list[str],
    contexts: dict[str, list[dict]],
    parent_history: Path,
) -> dict[str, Any]:
    crawl_news_v2 = _import_crawler()
    output_dir = Path(args.output_dir)
    site_dir = output_dir / site
    site_dir.mkdir(parents=True, exist_ok=True)
    site_history = site_dir / "scraped_urls.txt"
    _seed_site_history(parent_history, site_history)
    started = time.time()
    saved = int(
        crawl_news_v2.crawl_once(
            months=None,
            days=args.days,
            output_dir=str(site_dir),
            max_pages_per_site=args.max_pages_per_site,
            max_links_per_page=args.max_links_per_page,
            delay_sec=args.delay_sec,
            sites=[site],
            keywords=keywords,
            history_file=str(site_history),
            continue_listing_after_old_page=False,
            skip_similar_merge=args.no_similar_merge,
            unique_json_per_url=args.unique_json_per_url,
            keyword_contexts=contexts,
            max_articles=args.max_articles or None,
        )
    )
    elapsed = time.time() - started
    return {
        "site": site,
        "saved_articles": saved,
        "elapsed_sec": elapsed,
        "exit_code": 0,
    }


def run_tier2_crawl(args: argparse.Namespace, brands: list[Tier2Brand]) -> int:
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
    sites = tier2_sites(args.sites)
    parent_history = output_dir / "scraped_urls.txt"
    concurrent_sites = effective_tier2_concurrent_sites(args)
    if concurrent_sites == 1 or len(sites) <= 1:
        report = [
            _run_tier2_site(args, site, keywords, contexts, parent_history)
            for site in sites
        ]
    else:
        report = []
        with ThreadPoolExecutor(max_workers=concurrent_sites) as executor:
            futures = {
                executor.submit(_run_tier2_site, args, site, keywords, contexts, parent_history): site
                for site in sites
            }
            for future in as_completed(futures):
                site = futures[future]
                try:
                    report.append(future.result())
                except Exception as exc:  # noqa: BLE001 - persisted in report for handoff.
                    report.append({"site": site, "error": str(exc), "saved_articles": 0, "exit_code": -1})
    report = sorted(report, key=lambda item: str(item.get("site", "")))
    (output_dir / "tier2_site_report.json").write_text(
        json.dumps(
            {
                "concurrent_sites": concurrent_sites,
                "sites": sites,
                "results": report,
                "total_news": sum(int(item.get("saved_articles", 0)) for item in report),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    failures = [item for item in report if int(item.get("exit_code", 0)) != 0]
    if failures:
        raise SystemExit("Tier2 site crawl failed: " + json.dumps(failures, ensure_ascii=False))
    return sum(int(item.get("saved_articles", 0)) for item in report)


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


def build_tier1_command(args: argparse.Namespace) -> list[str]:
    sites = args.sites or ",".join(_import_crawler().SITE_CONFIGS.keys())
    command = [
        sys.executable,
        str(CRAWLER_DIR / "crawl_news_full_orchestrator.py"),
        "--crawler",
        str(CRAWLER_DIR / "crawl_news_v2.py"),
        "--drug-profile-dir",
        args.drug_profile_dir,
        "--sites",
        sites,
        "--output-base",
        args.output_dir,
        "--months",
        str(args.months),
        "--delay",
        str(args.delay_sec),
        "--concurrent-sites",
        str(args.concurrent_sites),
        "--max-pages",
        str(args.max_pages_per_site),
    ]
    if args.unique_json_per_url:
        command.append("--batch-by-month")
    return command


def run_tier1_existing_flow(args: argparse.Namespace) -> int:
    command = build_tier1_command(args)
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
    parser.add_argument("--weekday-slice", type=int, default=_today_weekday())
    parser.add_argument("--slice-mod", type=int, default=7)
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
    parser.add_argument("--tier2-concurrent-sites", type=int, default=1)
    parser.add_argument("--no-similar-merge", action="store_true")
    parser.add_argument("--unique-json-per-url", action="store_true")
    parser.add_argument("--drug-profile-dir", default=str(CRAWL_ROOT / "config" / "drug_profiles"))
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("DB_PORT", "3306")))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "jw_mart"))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "root"))
    parser.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.slice_mod <= 0:
        raise SystemExit("--slice-mod must be positive")
    if args.weekday_slice is not None and not 0 <= args.weekday_slice < args.slice_mod:
        raise SystemExit("--weekday-slice must satisfy 0 <= weekday-slice < --slice-mod")
    if args.tier == "1":
        if args.dry_run or not args.run_crawl:
            print(
                json.dumps(
                    {
                        "tier": 1,
                        "mode": "existing_wf196_flow",
                        "planned": True,
                        "orchestrator_command": build_tier1_command(args),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        return run_tier1_existing_flow(args)

    brands = load_tier2_brands(args)
    write_brand_plan(brands, args.brand_plan_output, slice_mod=args.slice_mod)
    summary: dict[str, Any] = {
        "tier": 2,
        "brand_count": len(brands),
        "weekday_slice": args.weekday_slice,
        "slice_mod": args.slice_mod,
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
