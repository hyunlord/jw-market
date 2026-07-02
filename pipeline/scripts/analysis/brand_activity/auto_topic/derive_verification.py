#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "rich",
#     "typer",
# ]
# ///
# ─── How to run ───
# uv run --script pipeline/scripts/analysis/brand_activity/auto_topic/derive_verification.py --audit-dir /tmp/topic_5b_recovery/audit_dir --post-hoc
"""Derive the legacy verification sidecar from preserved current-run artifacts."""

from __future__ import annotations

from pathlib import Path
import sys

import typer
from rich.console import Console


REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.scripts.analysis.brand_activity.auto_topic.verification import (  # noqa: E402
    VERIFICATION_FILE,
    write_verification_file,
)


CONSOLE = Console()


def main(
    audit_dir: Path = typer.Option(..., "--audit-dir", help="Preserved auto_topic audit directory copy."),
    post_hoc: bool = typer.Option(True, "--post-hoc/--native", help="Mark the sidecar as derived after the original run."),
) -> None:
    """Write `singleconcept_top7_verification.json` from sanitized measured artifacts."""
    payload = write_verification_file(audit_dir, derived_post_hoc=post_hoc)
    output = audit_dir / VERIFICATION_FILE
    CONSOLE.print_json(
        data={
            "verification": str(output),
            "derived_post_hoc": payload.get("derived_post_hoc", False),
            "executed_call_count": payload.get("executed_call_count"),
            "prompt_tokens": payload.get("prompt_tokens"),
            "completion_tokens": payload.get("completion_tokens"),
            "estimated_usd_vertex_flash_proxy": payload.get("estimated_usd_vertex_flash_proxy"),
            "raw_text_leak_count": payload.get("raw_text_leak_count"),
            "quality_grade_distribution": payload.get("quality_grade_distribution"),
        }
    )


if __name__ == "__main__":
    typer.run(main)
