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
from pipeline.scripts.etl import build_cache_deep_analysis_general as general_cache_builder  # noqa: E402
from pipeline.scripts.deploy.mart_direct_import import (  # noqa: E402
    DirectBuildImportConfig,
    DumpImportConfig,
    run_build_dump_import,
    run_dump_import,
)
from pipeline.scripts.deploy.mart_load_verify import (  # noqa: E402
    find_bridge_reference_db,
    verify_loaded_tables,
)
from pipeline.scripts.rollback.recording import (  # noqa: E402
    add_promotion_identity_args,
    identity_from_args,
    record_mysql_component,
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
    parser.add_argument("--manifest-json", type=Path, help="Direct-import manifest path. Written by --direct-import; read with --import-from-dump unless --import-manifest is set.")
    parser.add_argument("--import-manifest", type=Path, help="Manifest JSON to verify an import-only restore.")
    parser.add_argument("--direct-import", action="store_true", help="Build locally, dump verified mart tables, then import the dump directly into --target-db.")
    parser.add_argument("--import-from-dump", type=Path, help="Import a prebuilt direct-import dump into --target-db without rebuilding.")
    parser.add_argument("--create-target-db", action="store_true", help="Create --target-db if missing. Intended for local rehearsal, not production jw_mart.")
    parser.add_argument("--drop-build-after-dump", action="store_true", help="Drop the isolated local build schema after dump creation to reduce disk pressure.")
    parser.add_argument("--drop-target-after-verify", action="store_true", help="Drop the direct-import target schema after successful verification. Refuses protected schemas.")
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
    parser.add_argument(
        "--general-cache-stale-source",
        choices=["all", "ubist", "iqvia", "iqvia_nsa"],
        help="After a successful d2 mart publish/import, mark general deep-analysis cache rows stale for this source.",
    )
    parser.add_argument(
        "--general-cache-stale-reason",
        help="Reason stored in cache stale_reason. Defaults to etl:<source>:<run-id>.",
    )
    parser.add_argument(
        "--general-cache-priority-top-groups",
        type=int,
        help="After stale marking, recompute the top N brand+ATC4 groups with the existing GET_LOCK path.",
    )
    parser.add_argument("--general-cache-workers", type=int, default=4)
    parser.add_argument("--general-cache-group-batch-size", type=int, default=100)
    add_promotion_identity_args(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_id = args.run_id or time.strftime("%Y%m%d_%H%M%S")
    build_db = args.build_db or f"{args.target_db}_build_{run_id}"
    promotion_identity = identity_from_args(
        args,
        promotion_run_id=run_id,
        serving_db=args.target_db,
    )
    include_strategic = not bool(args.skip_strategic_ml_market)
    started = time.perf_counter()
    try:
        load_env_file(args.env_file)
        if args.direct_import:
            if not args.dump_path:
                raise RuntimeError("--direct-import requires --dump-path")
            if not args.manifest_json:
                raise RuntimeError("--direct-import requires --manifest-json")
            summary = run_build_dump_import(
                DirectBuildImportConfig(
                    run_id=run_id,
                    source_db=args.source_db,
                    target_db=args.target_db,
                    build_db=build_db,
                    dump_path=args.dump_path,
                    manifest_json=args.manifest_json,
                    audit_json=args.audit_json,
                    catalog_root=args.catalog_root,
                    input_mode=args.input_mode,
                    include_strategic_ml_market=include_strategic,
                    allow_operating_target=bool(args.allow_operating_target),
                    create_target_db=bool(args.create_target_db),
                    drop_build_after_dump=bool(args.drop_build_after_dump),
                    drop_target_after_verify=bool(args.drop_target_after_verify),
                )
            )
            summary["general_cache_refresh"] = _refresh_general_cache_after_mart_update(args, run_id)
            print(json.dumps({"event": "complete", **summary}, ensure_ascii=False, default=str))
            return 0
        if args.import_from_dump:
            manifest_path = args.import_manifest or args.manifest_json
            if not manifest_path:
                raise RuntimeError("--import-from-dump requires --manifest-json or --import-manifest")
            summary = run_dump_import(
                DumpImportConfig(
                    target_db=args.target_db,
                    dump_path=args.import_from_dump,
                    manifest_json=manifest_path,
                    audit_json=args.audit_json,
                    allow_operating_target=bool(args.allow_operating_target),
                    create_target_db=bool(args.create_target_db),
                    drop_target_after_verify=bool(args.drop_target_after_verify),
                )
            )
            summary["general_cache_refresh"] = _refresh_general_cache_after_mart_update(args, run_id)
            print(json.dumps({"event": "complete", **summary}, ensure_ascii=False, default=str))
            return 0
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
            if promotion_identity is not None:
                record_mysql_component(
                    conn,
                    identity=promotion_identity,
                    component="general",
                    table_pairs=tuple(
                        (action.table, action.backup_table)
                        for action in actions
                        if action.backup_table is not None
                    ),
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
            "general_cache_refresh": _refresh_general_cache_after_mart_update(args, run_id),
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


def _refresh_general_cache_after_mart_update(args: argparse.Namespace, run_id: str) -> dict[str, object] | None:
    should_mark_stale = bool(args.general_cache_stale_source)
    priority_top_groups = args.general_cache_priority_top_groups
    if not should_mark_stale and priority_top_groups is None:
        return None
    if args.target_db != general_cache_builder.TARGET_DATABASE:
        return {
            "skipped": True,
            "reason": "target_db_not_d2",
            "target_db": args.target_db,
        }

    started = time.perf_counter()
    conn = connect_admin()
    try:
        with conn.cursor() as cur:
            cur.execute(f"USE `{args.target_db}`")
        general_cache_builder.assert_d2_database(conn)
        general_cache_builder.ensure_general_cache_table(conn)
        general_cache_builder.ensure_market_forecast_table(conn)
        marked = None
        if should_mark_stale:
            reason = args.general_cache_stale_reason or f"etl:{args.general_cache_stale_source}:{run_id}"
            marked = general_cache_builder.mark_general_forecast_stale(
                conn,
                source=args.general_cache_stale_source,
                reason=reason,
            )

        built_count = 0
        skipped_lock_batches = 0
        if priority_top_groups is not None and priority_top_groups > 0:
            selected = general_cache_builder.select_priority_group_keys(conn, limit_groups=priority_top_groups)
            for group_batch in general_cache_builder.chunked(selected, int(args.general_cache_group_batch_size)):
                locks = general_cache_builder.acquire_group_locks(conn, group_batch)
                if locks is None:
                    skipped_lock_batches += 1
                    continue
                try:
                    built = general_cache_builder.build_batch_rows(
                        conn,
                        group_batch,
                        workers=args.general_cache_workers,
                        verbose=True,
                    )
                    general_cache_builder.write_rows(
                        conn,
                        built,
                        table_name=general_cache_builder.GENERAL_CACHE_TABLE,
                        batch_size=100,
                    )
                    built_count += len(built)
                finally:
                    general_cache_builder.release_group_locks(conn, locks)
        return {
            "skipped": False,
            "marked": marked,
            "priority_top_groups": priority_top_groups,
            "built": built_count,
            "skipped_lock_batches": skipped_lock_batches,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
