#!/usr/bin/env python3
"""Phase ζ bundle builder CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pymysql

from bundle_builder import BundleConfig, build_brand_bundle, render_narrative


def _connect(config: BundleConfig):
    return pymysql.connect(
        host=config.db.host,
        port=config.db.port,
        user=os.environ.get(config.db.user_env, "root"),
        password=os.environ.get(config.db.password_env, ""),
        database=config.db.database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def main():
    parser = argparse.ArgumentParser(description="Phase ζ bundle builder")
    parser.add_argument("--brand", help="Single brand name")
    parser.add_argument("--brands-from", help="File with brand names (one per line)")
    parser.add_argument("--snapshot-at", required=True, help="ISO 8601 datetime with tz")
    parser.add_argument("--config", required=True, help="YAML config path")
    parser.add_argument("--catalog", default="docs/crawl/_catalog.json")
    parser.add_argument("--out", help="Single output file")
    parser.add_argument("--out-dir", help="Output dir for --brands-from")
    parser.add_argument("--render-narrative", action="store_true")
    args = parser.parse_args()

    if not args.brand and not args.brands_from:
        parser.error("either --brand or --brands-from required")
    if args.brand and not args.out:
        parser.error("--out required for --brand")
    if args.brands_from and not args.out_dir:
        parser.error("--out-dir required for --brands-from")

    config = BundleConfig.from_yaml(args.config)
    snapshot_at = datetime.fromisoformat(args.snapshot_at)
    brands = [args.brand] if args.brand else [
        b.strip() for b in Path(args.brands_from).read_text(encoding="utf-8").splitlines() if b.strip()
    ]

    conn = _connect(config)
    results = []
    try:
        for brand in brands:
            try:
                bundle = build_brand_bundle(brand, snapshot_at, config, conn, args.catalog)
                if args.brand:
                    out_path = Path(args.out)
                else:
                    out_path = Path(args.out_dir) / f"{brand}_{snapshot_at.strftime('%Y%m%d')}.json"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

                result = {
                    "brand": brand,
                    "out": str(out_path),
                    "bundle_hash": bundle["bundle_meta"]["bundle_hash"],
                    "stats": bundle["bundle_meta"]["stats"],
                }
                if args.render_narrative:
                    narrative_path = out_path.with_suffix(".narrative.md")
                    narrative_path.write_text(render_narrative(bundle, stage="all"), encoding="utf-8")
                    result["narrative"] = str(narrative_path)
                results.append(result)
                print(
                    f"[OK] {brand}: hash={bundle['bundle_meta']['bundle_hash'][:16]}... "
                    f"events={bundle['bundle_meta']['stats']['event_count_direct']}",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(f"[FAIL] {brand}: {exc}", file=sys.stderr)
                results.append({"brand": brand, "error": str(exc)})
    finally:
        conn.close()

    print(json.dumps({"snapshot_at": args.snapshot_at, "config_version": config.config_version, "results": results}, ensure_ascii=False, indent=2))
    return 1 if any("error" in item for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
