from __future__ import annotations

from pathlib import Path

from pipeline.scripts.deploy.brand_activity_307.build_mirror import build_mirror


def test_build_mirror_contains_topic_server_and_requirements(tmp_path: Path) -> None:
    """Given an output directory, When the mirror is built, Then deploy-critical files are present."""
    output = tmp_path / "llmops_307"

    summary = build_mirror(output)

    assert summary["files"] > 0
    assert (output / "requirements.txt").is_file()
    assert (output / "pipeline/scripts/serving/brand_activity/topic_server.py").is_file()
    assert (output / "pipeline/scripts/etl/brand_activity/brand_activity_replay.py").is_file()
    assert (output / "pipeline/scripts/analysis/brand_activity/auto_topic/run_auto_topic.py").is_file()
