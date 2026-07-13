from __future__ import annotations

"""Build dynamic filter dimension sidecars into an isolated schema.

This tracked CLI is the only approved path for dynamic filter-dimension sidecar
loads. It writes a new ``jw_mart_dim_stage_*`` schema by default. D-1 also uses
the same audited path to install the verified sidecar into the local developer
``jw_mart`` serving schema, but only behind ``--allow-local-serving-target`` and
only when the DB host is localhost.
The sidecar uses product-level rows because source-specific dimensions such as
UBIST 제형/투여경로 or IQVIA STRENGTH/MOLECULE TYPE belong to products.
Filtering a whole brand row by one product's dimension would overstate market
size. IQVIA MOLECULE DESC is exposed as the molecule_desc dimension from the raw
source value; PACK DESC remains outside the dynamic API dimension contract.
"""

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import pymysql

from pipeline.etl.io.mart.filter_dimension_load import create_filter_dimension_table
from pipeline.etl.io.mart.filter_dimension_load import copy_filter_dimension_source_rows
from pipeline.etl.io.mart.filter_dimension_load import insert_filter_dimension_rows
from pipeline.etl.io.mart.filter_dimension_load import quote_id
from pipeline.etl.io.mart.filter_dimension_metric import FILTER_DIMENSION_TABLE
from pipeline.etl.io.mart.filter_dimension_metric import build_filter_dimension_rows
from pipeline.etl.io.mart.filter_dimension_metric import guard_dimension_stage_target
from pipeline.etl.io.mart.filter_dimension_metric import summarize_dimension_rows
from pipeline.etl.io.mart.filter_dimension_promote import promote_filter_dimension_slice
from pipeline.etl.io.mart.filter_dimension_promote import promote_filter_dimension_rows
from pipeline.etl.io.mart.general_config import PROJECT_ROOT
from pipeline.etl.io.mart.general_config import first_existing
from pipeline.etl.io.mart.general_config import load_env
from pipeline.etl.io.mart.general_config import MEASURES_BY_SOURCE
from pipeline.etl.io.mart.general_iqvia import iqvia_measure_frame
from pipeline.etl.io.mart.general_iqvia import load_iqvia_base_frame
from pipeline.etl.io.mart.general_ubist import load_ubist_base_frame
from pipeline.etl.io.mart.general_ubist import ubist_measure_frame


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-db", required=True, help="New isolated schema, must start with jw_mart_dim_stage_")
    parser.add_argument("--manifest-path", type=Path, required=True)
    parser.add_argument(
        "--source",
        choices=("ubist", "iqvia_nsa", "all"),
        default="all",
        help="Source dimension rows to emit. Use all for the complete sidecar evidence build.",
    )
    parser.add_argument(
        "--copy-ubist-from",
        help=(
            "Optional verified jw_mart_dim_stage_* schema to copy UBIST rows from. "
            "This keeps STAGE B from re-running the expensive UBIST raw aggregation "
            "while still using a tracked, batch-limited loader path."
        ),
    )
    parser.add_argument(
        "--copy-all-from",
        help=(
            "Optional verified jw_mart_dim_stage_* schema to copy all source rows from. "
            "D-1 uses this to install the already-verified sidecar into local jw_mart "
            "through the tracked Galera-safe batch path instead of an ad hoc loader."
        ),
    )
    parser.add_argument(
        "--allow-local-serving-target",
        action="store_true",
        help="Permit target-db=jw_mart only when the connection host is localhost.",
    )
    parser.add_argument("--ubist-dir", type=Path, help="Raw UBIST parquet root. Defaults to S4_UBIST_DIR/output/ubist")
    parser.add_argument("--max-rows", type=int, default=None, help="Fast validation only; do not use for production evidence")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument(
        "--dimension-type",
        help="Build one enabled dimension only. F-046 uses molecule for the bounded UBIST rebuild.",
    )
    parser.add_argument("--promote-to", help="Approved shared serving schema for the bounded ubist/molecule promotion.")
    parser.add_argument(
        "--allow-shared-serving-target",
        action="store_true",
        help="Explicit PL gate for promoting only the ubist/molecule slice into --promote-to.",
    )
    parser.add_argument(
        "--direct-shared-promotion",
        action="store_true",
        help="Compute the approved ubist/molecule slice fully before bounded direct promotion; creates no staging schema.",
    )
    parser.add_argument("--build-sha", default=os.environ.get("BUILD_GIT_SHA"), help="Code SHA recorded in provenance.")
    return parser.parse_args()


def run(args: argparse.Namespace) -> dict[str, Any]:
    guard_dimension_stage_target(args.target_db, allow_local_serving_target=args.allow_local_serving_target)
    if args.batch_size > 200:
        raise ValueError("--batch-size must be <= 200 for Galera writeset safety")
    if args.ubist_dir:
        os.environ["S4_UBIST_DIR"] = str(args.ubist_dir)
    if args.dimension_type and args.source != "ubist":
        raise ValueError("--dimension-type is currently restricted to --source ubist")
    if args.promote_to and not (args.allow_shared_serving_target and args.dimension_type == "molecule"):
        raise ValueError("shared promotion requires --dimension-type molecule and explicit approval")
    if args.promote_to and not args.build_sha:
        raise ValueError("--build-sha is required for shared promotion provenance")
    if args.direct_shared_promotion and not args.promote_to:
        raise ValueError("--direct-shared-promotion requires --promote-to")

    started = time.perf_counter()
    conn = _connect_admin()
    try:
        if args.allow_local_serving_target:
            _guard_local_serving_target(conn, args.target_db)
        if (
            not args.direct_shared_promotion
            and _schema_exists(conn, args.target_db)
            and not args.allow_local_serving_target
        ):
            raise RuntimeError(f"target schema already exists: {args.target_db}")
        serving_guard_schema = _serving_guard_schema(args)
        before_live = _general_table_counts(conn, serving_guard_schema)
        if not args.direct_shared_promotion:
            create_filter_dimension_table(
                conn,
                args.target_db,
                allow_local_serving_target=args.allow_local_serving_target,
            )

        manifest: dict[str, Any] = {
            "target_db": args.target_db,
            "table": FILTER_DIMENSION_TABLE,
            "source": args.source,
            "policy": {
                "isolated_prefix": "jw_mart_dim_stage_",
                "live_schema_blocked": "jw_mart",
                "local_serving_target_allowed": bool(args.allow_local_serving_target),
                "batch_size": args.batch_size,
                "iqvia_molecule_desc_dimension": "enabled",
                "ubist_molecule_dimension": "enabled_raw_unsplit",
                "pack_desc": "disabled",
                "grain": "product_level",
                "direct_shared_promotion": bool(args.direct_shared_promotion),
            },
            "provenance": {
                "code_sha": args.build_sha,
                "source_epoch": _source_epoch(),
                "ubist_dir": str(args.ubist_dir or os.environ.get("S4_UBIST_DIR") or ""),
            },
            "sources": {},
            "live_before": before_live,
        }
        computed_rows: list[dict[str, Any]] = []
        for source in _selected_sources(args.source):
            if args.direct_shared_promotion:
                source_manifest, rows = _build_direct_source_rows(
                    source,
                    max_rows=args.max_rows,
                    dimension_types={args.dimension_type} if args.dimension_type else None,
                )
                manifest["sources"][source] = source_manifest
                computed_rows.extend(rows)
                continue
            if args.copy_all_from:
                manifest["sources"][source] = _copy_source_rows(
                    conn,
                    args.copy_all_from,
                    args.target_db,
                    source,
                    batch_size=args.batch_size,
                    allow_local_serving_target=args.allow_local_serving_target,
                )
            elif source == "ubist" and args.copy_ubist_from:
                manifest["sources"][source] = _copy_source_rows(
                    conn, args.copy_ubist_from, args.target_db, source,
                    batch_size=args.batch_size,
                )
            else:
                manifest["sources"][source] = _load_source_rows(
                    conn,
                    args.target_db,
                    source,
                    max_rows=args.max_rows,
                    batch_size=args.batch_size,
                    dimension_types={args.dimension_type} if args.dimension_type else None,
                )

        if args.direct_shared_promotion:
            manifest["target"] = {
                "row_count": len(computed_rows),
                "storage": "computed_before_direct_promotion",
            }
        else:
            manifest["target"] = _target_summary(conn, args.target_db)
        if args.promote_to:
            build_marker = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            if args.direct_shared_promotion:
                manifest["promotion"] = promote_filter_dimension_rows(
                    conn,
                    computed_rows,
                    target_db=args.promote_to,
                    source="ubist",
                    dimension_type="molecule",
                    build_marker=build_marker,
                    batch_size=args.batch_size,
                    allow_shared_serving_target=args.allow_shared_serving_target,
                )
            else:
                manifest["promotion"] = promote_filter_dimension_slice(
                    conn,
                    source_db=args.target_db,
                    target_db=args.promote_to,
                    source="ubist",
                    dimension_type="molecule",
                    build_marker=build_marker,
                    batch_size=args.batch_size,
                    allow_shared_serving_target=args.allow_shared_serving_target,
                )
        manifest["live_after"] = _general_table_counts(conn, serving_guard_schema)
        manifest["live_unchanged"] = manifest["live_before"] == manifest["live_after"]
        manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        _write_json(args.manifest_path, manifest)
        return manifest
    finally:
        conn.close()


def _selected_sources(source: str) -> tuple[str, ...]:
    if source == "all":
        return ("ubist", "iqvia_nsa")
    return (source,)


def _serving_guard_schema(args: argparse.Namespace) -> str:
    """Check the schema that this run is allowed to affect."""

    return args.promote_to or "jw_mart"


def _load_source_rows(
    conn: pymysql.connections.Connection,
    target_db: str,
    source: str,
    *,
    max_rows: int | None,
    batch_size: int,
    dimension_types: set[str] | None = None,
) -> dict[str, Any]:
    if source == "ubist":
        base = load_ubist_base_frame(max_rows=max_rows)
        measure_frame = ubist_measure_frame
    elif source == "iqvia_nsa":
        base = load_iqvia_base_frame(max_rows=max_rows)
        measure_frame = iqvia_measure_frame
    else:
        raise ValueError(f"unsupported dimension source: {source}")

    source_manifest: dict[str, Any] = {
        "source_rows": int(len(base)),
        "measures": {},
    }
    for measure in MEASURES_BY_SOURCE[source]:
        frame = measure_frame(base, measure)
        rows = build_filter_dimension_rows(source, measure, frame, dimension_types=dimension_types)
        insert_filter_dimension_rows(conn, target_db, rows, batch_size=batch_size)
        source_manifest["measures"][measure] = {
            "input_rows": int(len(frame)),
            "sidecar": summarize_dimension_rows(rows),
        }
    return source_manifest


def _build_direct_source_rows(
    source: str,
    *,
    max_rows: int | None,
    dimension_types: set[str] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if source != "ubist":
        raise ValueError("direct shared promotion is restricted to UBIST")
    base = load_ubist_base_frame(max_rows=max_rows)
    source_manifest: dict[str, Any] = {
        "source_rows": int(len(base)),
        "measures": {},
    }
    computed_rows: list[dict[str, Any]] = []
    for measure in MEASURES_BY_SOURCE[source]:
        frame = ubist_measure_frame(base, measure)
        rows = build_filter_dimension_rows(source, measure, frame, dimension_types=dimension_types)
        computed_rows.extend(rows)
        source_manifest["measures"][measure] = {
            "input_rows": int(len(frame)),
            "sidecar": summarize_dimension_rows(rows),
        }
    return source_manifest, computed_rows


def _source_epoch() -> str | None:
    try:
        from pipeline.scripts.api.dynamic_market import response_cache

        return response_cache._store.source_epoch()
    except Exception as exc:
        if os.environ.get("REQUIRE_SOURCE_EPOCH") == "1":
            raise RuntimeError("source epoch is required for this build") from exc
        return None


def _copy_source_rows(
    conn: pymysql.connections.Connection,
    source_db: str,
    target_db: str,
    source: str,
    *,
    batch_size: int,
    allow_local_serving_target: bool = False,
) -> dict[str, Any]:
    return copy_filter_dimension_source_rows(
        conn,
        source_db,
        target_db,
        source,
        batch_size=batch_size,
        allow_local_serving_target=allow_local_serving_target,
    )


def _connect_admin() -> pymysql.connections.Connection:
    env_path = first_existing(PROJECT_ROOT / "pipeline" / "docker" / ".env", PROJECT_ROOT / "docker" / ".env")
    env = load_env(env_path)
    password = env.get("MARIADB_ROOT_PASSWORD") or env.get("MARIADB_PASSWORD")
    if not password:
        raise RuntimeError("MARIADB_ROOT_PASSWORD/MARIADB_PASSWORD is missing")
    return pymysql.connect(
        host=env.get("MARIADB_HOST", "127.0.0.1"),
        port=int(env.get("MARIADB_PORT") or env.get("HOST_PORT", "3308")),
        user="root" if env.get("MARIADB_ROOT_PASSWORD") else env.get("MARIADB_USER", "jwapp"),
        password=password,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _guard_local_serving_target(conn: pymysql.connections.Connection, target_db: str) -> None:
    """Allow serving installs only for the local developer MariaDB instance."""

    if target_db != "jw_mart":
        raise RuntimeError("--allow-local-serving-target only permits target-db=jw_mart")
    with conn.cursor() as cur:
        cur.execute("SELECT @@hostname AS hostname, @@port AS port")
        row = cur.fetchone()
    host = str(conn.host).lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"refusing non-local serving sidecar target host={conn.host!r}")
    if int(row["port"]) != 3306 and int(row["port"]) != 3308:
        raise RuntimeError(f"unexpected local MariaDB port: {row['port']}")


def _schema_exists(conn: pymysql.connections.Connection, db_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.schemata WHERE schema_name=%s", (db_name,))
        return int(cur.fetchone()["n"]) > 0


def _general_table_counts(conn: pymysql.connections.Connection, db_name: str) -> dict[str, int | None]:
    counts: dict[str, int | None] = {}
    for table in ("mart_general_brand_metric", "mart_general_market_metric"):
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS n FROM {quote_id(db_name)}.{quote_id(table)}")
                counts[table] = int(cur.fetchone()["n"])
        except pymysql.err.ProgrammingError:
            counts[table] = None
    return counts


def _target_summary(conn: pymysql.connections.Connection, db_name: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM {quote_id(db_name)}.{quote_id(FILTER_DIMENSION_TABLE)}")
        row_count = int(cur.fetchone()["n"])
        cur.execute(
            f"""
            SELECT source, measure, dimension_type,
                   COUNT(*) AS row_count,
                   COUNT(DISTINCT dimension_value_norm) AS option_count
            FROM {quote_id(db_name)}.{quote_id(FILTER_DIMENSION_TABLE)}
            GROUP BY source, measure, dimension_type
            ORDER BY source, measure, dimension_type
            """
        )
        coverage = cur.fetchall()
    return {"row_count": row_count, "coverage": coverage}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    manifest = run(parse_args())
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    if not manifest["live_unchanged"]:
        raise SystemExit("live jw_mart general table counts changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
