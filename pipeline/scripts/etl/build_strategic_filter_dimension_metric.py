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
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pipeline.etl.io.mart.strategic_filter_dimension_metric import build_strategic_sidecar
from pipeline.etl.io.mart.general_config import PROJECT_ROOT
from pipeline.etl.io.mart.general_config import first_existing
from pipeline.etl.io.mart.general_config import load_env


SAFE_STAGE_RE = re.compile(r"^jw_mart_d2_strategic_dim_stage_[0-9]{8}_[0-9]{6}$")
REHEARSAL_STAGE_RE = re.compile(r"^jw_mart_rehearsal_[A-Za-z0-9_]+$")
LOCAL_SERVING_TARGET = "jw_mart"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build strategic filter dimension sidecar into an isolated local schema.")
    parser.add_argument("--source-db", default="jw_mart", help="Read-only strategic mart source schema.")
    parser.add_argument("--target-db", help="Isolated target schema. Defaults to jw_mart_d2_strategic_dim_stage_<ts>.")
    parser.add_argument("--manifest", type=Path, help="Path to write JSON build manifest.")
    parser.add_argument("--replace-table", action="store_true", help="Replace the target sidecar table inside the isolated target schema only.")
    parser.add_argument(
        "--allow-local-serving-target",
        action="store_true",
        help="Allow replacing the local jw_mart serving sidecar when host is localhost. Operating DB targets remain refused.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    target_db = args.target_db or f"jw_mart_d2_strategic_dim_stage_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if target_db == LOCAL_SERVING_TARGET and args.allow_local_serving_target:
        _guard_local_serving_target(target_db)
    elif not (SAFE_STAGE_RE.fullmatch(target_db) or REHEARSAL_STAGE_RE.fullmatch(target_db)):
        raise SystemExit(f"Refusing non-isolated target schema: {target_db}")
    manifest = build_strategic_sidecar(source_db=args.source_db, target_db=target_db, replace_table=args.replace_table)
    manifest_path = args.manifest or Path("/tmp") / f"{target_db}_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"target_db": target_db, "manifest": str(manifest_path), "rows_inserted": manifest["rows_inserted"]}, ensure_ascii=False))


def _guard_local_serving_target(target_db: str) -> None:
    env_path = first_existing(PROJECT_ROOT / "pipeline" / "docker" / ".env", PROJECT_ROOT / "docker" / ".env")
    env = load_env(env_path)
    host = str(env.get("MARIADB_HOST", "127.0.0.1")).strip().lower()
    port = int(env.get("MARIADB_PORT") or env.get("HOST_PORT") or "3308")
    if target_db != LOCAL_SERVING_TARGET:
        raise SystemExit(f"Refusing serving target other than {LOCAL_SERVING_TARGET}: {target_db}")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit(f"Refusing local serving load on non-local host: {host}")
    if port not in {3306, 3308}:
        raise SystemExit(f"Refusing local serving load on unexpected port: {port}")


if __name__ == "__main__":
    main()
