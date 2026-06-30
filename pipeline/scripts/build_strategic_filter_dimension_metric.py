"""CLI for the tracked strategic filter-dimension sidecar build.

This script is the only supported D-2 loader path.  It creates/populates an
isolated target schema from the local strategic mart, records a manifest, and
keeps all metric values sourced from recoded mart_strategic rows.  Ad-hoc SQL
or inline Python loaders would make the build unreproducible, so they are
intentionally not part of the workflow.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.etl.io.mart.general_config import mariadb_connect
from pipeline.etl.io.mart.strategic_filter_dimension_metric import build_strategic_sidecar


SAFE_STAGE_RE = re.compile(r"^jw_mart_d2_strategic_dim_stage_[0-9]{8}_[0-9]{6}$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build strategic filter dimension sidecar into an isolated local schema.")
    parser.add_argument("--source-db", default="jw_mart", help="Read-only strategic mart source schema.")
    parser.add_argument("--target-db", help="Isolated target schema. Defaults to jw_mart_d2_strategic_dim_stage_<ts>.")
    parser.add_argument("--manifest", type=Path, help="Path to write JSON build manifest.")
    parser.add_argument("--replace-table", action="store_true", help="Replace the target sidecar table inside the isolated target schema only.")
    parser.add_argument(
        "--drop-target-after-success",
        action="store_true",
        help="Drop the isolated target schema after a successful build and manifest write.",
    )
    return parser.parse_args(argv)


def drop_stage_schema(conn: object, target_db: str) -> None:
    if not SAFE_STAGE_RE.fullmatch(target_db):
        raise SystemExit(f"Refusing cleanup for non-isolated target schema: {target_db}")
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{target_db}`")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    target_db = args.target_db or f"jw_mart_d2_strategic_dim_stage_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if not SAFE_STAGE_RE.fullmatch(target_db):
        raise SystemExit(f"Refusing non-isolated target schema: {target_db}")
    conn = mariadb_connect()
    try:
        manifest = build_strategic_sidecar(
            source_db=args.source_db,
            target_db=target_db,
            connection=conn,
            replace_table=args.replace_table,
        )
        if args.drop_target_after_success:
            drop_stage_schema(conn, target_db)
            manifest["cleanup"] = {"target_db_dropped": True}
        else:
            manifest["cleanup"] = {"target_db_dropped": False}
    finally:
        conn.close()
    manifest_path = args.manifest or Path("/tmp") / f"{target_db}_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"target_db": target_db, "manifest": str(manifest_path), "rows_inserted": manifest["rows_inserted"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
