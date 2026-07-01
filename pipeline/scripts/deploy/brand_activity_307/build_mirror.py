#!/usr/bin/env python3
# /// script
# dependencies = [
#   "typer>=0.12.0",
# ]
# ///
# ─── How to run ───
# uv run --script pipeline/scripts/deploy/brand_activity_307/build_mirror.py --output /tmp/llmops_307_mirror
"""Build the deployable gitea llmops/307 mirror from jw-market sources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Final

import typer

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
@dataclass(frozen=True, slots=True)
class MirrorPlan:
    """Static mirror inputs copied into the llmops/307 deployment repo."""

    source_dirs: tuple[Path, ...]
    source_files: tuple[Path, ...]


class MirrorPlanError(RuntimeError):
    """Raised when the mirror manifest contains an unsupported support file."""


PLAN: Final = MirrorPlan(
    source_dirs=(
        Path("pipeline/scripts/serving/brand_activity"),
        Path("pipeline/scripts/etl/brand_activity"),
        Path("pipeline/scripts/analysis/brand_activity/auto_topic"),
        Path("pipeline/etl/io/catalog/master"),
    ),
    source_files=(
        Path("pipeline/scripts/deploy/brand_activity_307/requirements.txt"),
        Path("pipeline/scripts/deploy/brand_activity_307/DEPLOY_NOTES.md"),
    ),
)


def build_mirror(output: Path) -> dict[str, int]:
    """Copy the code-serving 307 source subset into an output directory."""
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    copied = 0
    for directory in PLAN.source_dirs:
        copied += _copy_python_tree(REPO_ROOT / directory, output / directory)
    for file_path in PLAN.source_files:
        _copy_file(REPO_ROOT / file_path, output / _mirror_file_name(file_path))
        copied += 1
    return {"files": copied}


def _copy_python_tree(source: Path, destination: Path) -> int:
    count = 0
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(source)
        _copy_file(path, destination / relative)
        count += 1
    return count


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _mirror_file_name(path: Path) -> Path:
    if path.name == "requirements.txt":
        return Path("requirements.txt")
    if path.name == "DEPLOY_NOTES.md":
        return Path("DEPLOY_NOTES.md")
    raise MirrorPlanError(f"unexpected mirror support file: {path.name}")


def main(output: Path = typer.Option(..., "--output", help="Mirror output directory.")) -> None:
    """Create a fresh local mirror directory."""
    summary = build_mirror(output)
    typer.echo(f"mirror={output}")
    typer.echo(f"files={summary['files']}")


if __name__ == "__main__":
    typer.run(main)
