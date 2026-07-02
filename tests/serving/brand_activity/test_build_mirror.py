from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.scripts.deploy.brand_activity_307.build_mirror import (
    MirrorPlanError,
    build_mirror,
    verify_mirror_imports,
)


def test_build_mirror_contains_topic_server_and_requirements(tmp_path: Path) -> None:
    """Given an output directory, When the mirror is built, Then deploy-critical files are present."""
    output = tmp_path / "llmops_307"

    summary = build_mirror(output)

    assert summary["files"] > 0
    assert (output / "requirements.txt").is_file()
    assert (output / "pipeline/scripts/serving/brand_activity/topic_server.py").is_file()
    assert (output / "pipeline/scripts/etl/brand_activity/brand_activity_replay.py").is_file()
    assert (output / "pipeline/scripts/analysis/brand_activity/auto_topic/run_auto_topic.py").is_file()
    assert (output / "service.py").is_file()


def test_build_mirror_is_importable_without_source_repo(tmp_path: Path) -> None:
    """Given a fresh mirror, When imports run in isolation, Then transitive local deps are present."""
    output = tmp_path / "llmops_307"

    build_mirror(output)

    assert (output / "pipeline/etl/io/catalog/_lib/common.py").is_file()
    verify_mirror_imports(output)


def test_build_mirror_satisfies_template_service_contract(tmp_path: Path) -> None:
    """Given a fresh mirror, When verified, Then template from-service import succeeds."""
    output = tmp_path / "llmops_307"

    build_mirror(output)

    verify_mirror_imports(output)


def test_build_mirror_rejects_appledouble_python_files(tmp_path: Path) -> None:
    """Given macOS AppleDouble metadata, When verified, Then the mirror fails before deploy."""
    output = tmp_path / "llmops_307"
    build_mirror(output)
    metadata = output / "pipeline/scripts/analysis/brand_activity/auto_topic/._run_auto_topic.py"
    metadata.write_bytes(b"\x00\x05metadata\xa3")

    with pytest.raises(MirrorPlanError, match="appledouble_files"):
        verify_mirror_imports(output)


def test_build_mirror_rejects_non_utf8_python_sources(tmp_path: Path) -> None:
    """Given a non-UTF-8 Python file, When verified, Then the file is reported."""
    output = tmp_path / "llmops_307"
    build_mirror(output)
    bad_source = output / "pipeline/scripts/analysis/brand_activity/auto_topic/non_utf8_probe.py"
    bad_source.write_bytes(b"VALUE = '\xa3'\n")

    with pytest.raises(MirrorPlanError, match="non_utf8_files"):
        verify_mirror_imports(output)
