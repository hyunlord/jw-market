#!/usr/bin/env python3
"""Run full news crawling as one subprocess per site.

The crawler writes to a site-specific directory, so parallel site workers do not
share mutable files. After a site finishes, this orchestrator copies article
JSON files into `_batches/YYYY-MM/` for quick month-level handoff checks.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


def _article_json_files(news_dir: Path) -> list[Path]:
    if not news_dir.exists():
        return []
    return [
        path
        for path in news_dir.glob("*.json")
        if path.name != "crawl_report.json" and "report" not in path.name.lower()
    ]


def _copy_month_batches(site: str, news_dir: Path, output_base: Path) -> dict[str, int]:
    month_counts: dict[str, int] = {}
    batch_base = output_base / "_batches"
    for path in _article_json_files(news_dir):
        try:
            with path.open(encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        date_raw = str(doc.get("date") or "")
        if len(date_raw) < 7:
            month = "unknown"
        else:
            month = date_raw[:7]
        month_dir = batch_base / month
        month_dir.mkdir(parents=True, exist_ok=True)
        target = month_dir / f"{site}__{path.name}"
        if not target.exists():
            shutil.copy2(path, target)
        month_counts[month] = month_counts.get(month, 0) + 1
    return dict(sorted(month_counts.items()))


def run_one_site(site: str, args: argparse.Namespace) -> dict:
    output_base = Path(args.output_base).resolve()
    site_dir = output_base / site
    news_dir = site_dir / f"news_5years_{site}"
    site_dir.mkdir(parents=True, exist_ok=True)
    log_file = site_dir / f"{site}_crawl.log"

    cmd = [
        sys.executable,
        "-u",
        args.crawler,
        "--stage",
        "news",
        "--drug-profile-dir",
        args.drug_profile_dir,
        "--sites",
        site,
        "--months",
        str(args.months),
        "--max-pages-per-site",
        str(args.max_pages),
        "--delay-sec",
        str(args.delay),
        "--news-dir-name",
        str(news_dir),
        "--reverse-time-order",
    ]
    if args.max_articles:
        cmd.extend(["--max-articles", str(args.max_articles)])
    if args.batch_by_month:
        cmd.append("--batch-by-month")

    started = time.time()
    print(f"[{datetime.now().isoformat(timespec='seconds')}] START {site}", flush=True)
    with log_file.open("w", encoding="utf-8") as logf:
        result = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT, check=False)

    elapsed = time.time() - started
    article_files = _article_json_files(news_dir)
    month_counts = _copy_month_batches(site, news_dir, output_base) if args.batch_by_month else {}
    site_report = {
        "site": site,
        "started_at": datetime.fromtimestamp(started).isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_sec": elapsed,
        "elapsed_hms": _format_elapsed(elapsed),
        "n_files": len(article_files),
        "exit_code": result.returncode,
        "log_file": str(log_file),
        "news_dir": str(news_dir),
        "month_counts": month_counts,
    }
    with (site_dir / "site_orchestrator_report.json").open("w", encoding="utf-8") as f:
        json.dump(site_report, f, ensure_ascii=False, indent=2)
    print(
        f"[{datetime.now().isoformat(timespec='seconds')}] END {site}: "
        f"{len(article_files)} news, {site_report['elapsed_hms']}, code={result.returncode}",
        flush=True,
    )
    return site_report


def _format_elapsed(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}시간 {m}분 {s}초"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--crawler", default="crawl_news_v2.py")
    ap.add_argument("--drug-profile-dir", default="drug_profiles")
    ap.add_argument("--sites", required=True, help="comma-separated")
    ap.add_argument("--output-base", required=True)
    ap.add_argument("--concurrent-sites", type=int, default=4)
    ap.add_argument("--months", type=int, default=60)
    ap.add_argument("--max-pages", type=int, default=10)
    ap.add_argument("--max-articles", type=int, default=0)
    ap.add_argument("--delay", type=float, default=5.0)
    ap.add_argument("--batch-by-month", action="store_true")
    args = ap.parse_args()

    sites = [site.strip() for site in args.sites.split(",") if site.strip()]
    output_base = Path(args.output_base).resolve()
    output_base.mkdir(parents=True, exist_ok=True)

    print(f"[Orchestrator] sites={len(sites)} concurrent={args.concurrent_sites}", flush=True)
    print(f"[Orchestrator] output_base={output_base}", flush=True)
    started = time.time()
    results: list[dict] = []

    with ProcessPoolExecutor(max_workers=args.concurrent_sites) as executor:
        futures = {executor.submit(run_one_site, site, args): site for site in sites}
        for future in as_completed(futures):
            site = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - persisted in report for handoff.
                print(f"[ERROR] {site}: {exc}", flush=True)
                results.append({"site": site, "error": str(exc), "exit_code": -1})

    elapsed = time.time() - started
    report = {
        "started_at": datetime.fromtimestamp(started).isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_sec": elapsed,
        "elapsed_hms": _format_elapsed(elapsed),
        "concurrent_sites": args.concurrent_sites,
        "sites": sites,
        "results": sorted(results, key=lambda item: item.get("site", "")),
        "total_news": sum(int(item.get("n_files", 0)) for item in results),
    }
    report_path = output_base / "orchestrator_report.json"
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[Orchestrator] Done: {report['elapsed_hms']} total_news={report['total_news']}", flush=True)
    print(f"[Orchestrator] Report: {report_path}", flush=True)


if __name__ == "__main__":
    main()
