#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import argparse
import json
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.scripts.deploy.mart_load_ops import (  # noqa: E402
    MART_TABLES,
    STRATEGIC_MARKET_TABLE,
    connect_admin,
    db_endpoint_summary,
    dump_tables,
    guard_run,
    load_env_file,
    publish_tables,
    run_bridge,
    run_s4_general,
    run_strategic_ml_market_from_source,
)
from pipeline.scripts.deploy.mart_load_verify import (  # noqa: E402
    find_bridge_reference_db,
    verify_loaded_tables,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build general mart and molecule bridge tables in an isolated schema, then safely publish them."
    )
    parser.add_argument("--source-db", default="jw_mart", help="Source DB for raw IQVIA and local mart references.")
    parser.add_argument("--target-db", required=True, help="Target DB to publish into. Use isolated staging for Phase 0.")
    parser.add_argument("--build-db", help="Isolated scratch DB. Defaults to <target-db>_build_<run-id>.")
    parser.add_argument("--run-id", help="Stable run id used for scratch/backup table names.")
    parser.add_argument("--env-file", type=Path, help="Optional env file with MariaDB connection values.")
    parser.add_argument("--catalog-root", type=Path, help="Catalog parquet root. Defaults to output/catalog.")
    parser.add_argument("--input-mode", choices=["raw", "enriched"], default="raw", help="S4 general mart input mode.")
    parser.add_argument("--dump-path", type=Path, help="Optional SQL or .sql.gz dump path for published staging tables.")
    parser.add_argument("--audit-json", type=Path, help="Optional JSON summary path.")
    parser.add_argument("--bridge-reference-db", help="Reference schema for 58,330-row mart_brand_molecule checksum.")
    parser.add_argument(
        "--skip-strategic-ml-market",
        action="store_true",
        help="Do not build/publish mart_strategic_ml_market_metric even though brand activity reads it.",
    )
    parser.add_argument(
        "--allow-operating-target",
        action="store_true",
        help="Required for operations-gated Phase 1 publish into jw_mart. Do not use for Phase 0.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_id = args.run_id or time.strftime("%Y%m%d_%H%M%S")
    build_db = args.build_db or f"{args.target_db}_build_{run_id}"
    include_strategic = not bool(args.skip_strategic_ml_market)
    started = time.perf_counter()
    try:
        load_env_file(args.env_file)
        guard_run(
            source_db=args.source_db,
            target_db=args.target_db,
            build_db=build_db,
            allow_operating_target=bool(args.allow_operating_target),
        )
        print(
            json.dumps(
                {
                    "event": "start",
                    "source_db": args.source_db,
                    "target_db": args.target_db,
                    "build_db": build_db,
                    "endpoint": db_endpoint_summary(),
                    "include_strategic_ml_market": include_strategic,
                },
                ensure_ascii=False,
            )
        )

        build_start = time.perf_counter()
        run_s4_general(
            build_db=build_db,
            source_db=args.source_db,
            catalog_root=args.catalog_root,
            input_mode=args.input_mode,
        )
        if include_strategic:
            run_strategic_ml_market_from_source(build_db=build_db, source_db=args.source_db, catalog_root=args.catalog_root)
        run_bridge(build_db=build_db, source_db=args.source_db, catalog_root=args.catalog_root)
        build_seconds = time.perf_counter() - build_start

        conn = connect_admin()
        try:
            bridge_reference_db = args.bridge_reference_db or find_bridge_reference_db(conn)
            publish_start = time.perf_counter()
            actions = publish_tables(
                conn,
                build_db=build_db,
                target_db=args.target_db,
                run_id=run_id,
                include_strategic_ml_market=include_strategic,
            )
            publish_seconds = time.perf_counter() - publish_start
            verify_results = verify_loaded_tables(
                conn,
                target_db=args.target_db,
                source_db=args.source_db,
                bridge_reference_db=bridge_reference_db,
                include_strategic_ml_market=include_strategic,
            )
        finally:
            conn.close()

        tables = tuple([*MART_TABLES, *([STRATEGIC_MARKET_TABLE] if include_strategic else [])])
        dump_result = dump_tables(target_db=args.target_db, tables=tables, dump_path=args.dump_path) if args.dump_path else None
        summary = {
            "run_id": run_id,
            "source_db": args.source_db,
            "target_db": args.target_db,
            "build_db": build_db,
            "tables": list(tables),
            "include_strategic_ml_market": include_strategic,
            "bridge_reference_db": bridge_reference_db,
            "build_seconds": round(build_seconds, 3),
            "publish_seconds": round(publish_seconds, 3),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "publish_actions": [asdict(action) for action in actions],
            "verification": [_verify_result_to_json(result) for result in verify_results],
            "dump": asdict(dump_result) if dump_result else None,
        }
        if args.audit_json:
            args.audit_json.parent.mkdir(parents=True, exist_ok=True)
            args.audit_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps({"event": "complete", **summary}, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        # CLI boundary: fail closed without printing credentials or continuing a partial publish.
        print(json.dumps({"event": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


def _verify_result_to_json(result: object) -> dict[str, object]:
    data = asdict(result)
    data["groups"] = {"|".join(key): value for key, value in result.groups.items()}
    return data


if __name__ == "__main__":
    raise SystemExit(main())
