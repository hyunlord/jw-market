#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "pymysql",
#     "rich",
#     "typer",
# ]
# ///
# ─── How to run ───
# uv run --script pipeline/scripts/analysis/brand_activity/auto_topic/save_topic_results.py --audit-dir docs/research/brand_activity/auto_topic/audit/<run_tag> --artifact-sha256 <sha256>
"""Store measured Brand Activity topic results in isolated API tables."""

from __future__ import annotations

from pathlib import Path
import sys

import typer
from rich.console import Console


REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.scripts.analysis.brand_activity.auto_topic.data_source import (  # noqa: E402
    SCHEMA,
    connect_mariadb,
    read_env_file,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.topic_store import (  # noqa: E402
    load_artifacts,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.topic_store_db import (  # noqa: E402
    ensure_store_summary_nonzero,
    save_artifacts,
    store_summary_json,
    topic_table_ddl,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.audit import write_json  # noqa: E402
from pipeline.scripts.analysis.brand_activity.auto_topic.models import JsonValue  # noqa: E402


DEFAULT_AUDIT_DIR = REPO_ROOT / "docs/research/brand_activity/auto_topic/audit/serving_direct_singleconcept_top7_exec_20260620_143124"
CONSOLE = Console()


def main(
    audit_dir: Path = typer.Option(DEFAULT_AUDIT_DIR, "--audit-dir", help="Measured auto_topic audit directory."),
    artifact_sha256: str = typer.Option("", "--artifact-sha256", help="SHA256 of the reviewed latest run artifact zip."),
    stage_schema: str = typer.Option(SCHEMA, "--stage-schema", help="Allowed isolated API schema."),
    output: Path | None = typer.Option(None, "--output", help="Optional JSON summary path."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Build records and DDL without writing DB."),
) -> None:
    """Load measured artifacts and optionally upsert them into MariaDB."""
    artifacts = load_artifacts(audit_dir)
    sha256 = artifact_sha256 or str(artifacts.run_summary.get("zip_sha256") or "")
    if not sha256:
        raise typer.BadParameter("--artifact-sha256 is required when run_summary has no zip_sha256")
    if dry_run:
        result: dict[str, JsonValue] = {
            "dry_run": True,
            "ddl": list(topic_table_ddl(stage_schema)),
            "run_id": artifacts.run_summary.get("tag"),
            "market_count": len(_list(artifacts.viz_payload.get("markets"))),
            "brand_count": len(_list(artifacts.viz_payload.get("brand_results"))),
        }
    else:
        connection = connect_mariadb(read_env_file())
        try:
            summary = save_artifacts(
                connection,
                schema=stage_schema,
                artifacts=artifacts,
                artifact_sha256=sha256,
            )
            ensure_store_summary_nonzero(summary)
        finally:
            connection.close()
        result = {"dry_run": False, **store_summary_json(summary)}
    if output is not None:
        write_json(output, result)
    CONSOLE.print_json(data=result)


def _list(value: JsonValue) -> list[JsonValue]:
    """Return a JSON array or an empty array."""
    return value if isinstance(value, list) else []


if __name__ == "__main__":
    typer.run(main)
