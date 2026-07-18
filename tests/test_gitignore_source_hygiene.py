from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


SOURCE_SUFFIXES = {".json", ".py", ".sql", ".yaml", ".yml"}
GENERATED_PARTS = {".pytest_cache", "__pycache__"}


def test_gitignore_does_not_hide_source_files() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("git metadata is unavailable in this test environment")

    hidden_sources = []
    for raw_path in result.stdout.splitlines():
        path = Path(raw_path)
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        if GENERATED_PARTS.intersection(path.parts):
            continue
        hidden_sources.append(raw_path)

    assert hidden_sources == [], (
        "gitignore hides source-like files; track them or narrow the matching rule:\n"
        + "\n".join(hidden_sources)
    )
