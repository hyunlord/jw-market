from __future__ import annotations

import ast
from pathlib import Path

from pipeline.scripts.ingest_hook.complete_reingest_runner import STAGE_SEQUENCES
from pipeline.scripts.ingest_hook.job_runner import _SOURCE_STAGE_CONTRACTS, _StageTracker


EXPECTED_STAGE_COUNTS = {
    "ubist": 9,
    "iqvia_nsa": 9,
    "iqvia_csd_channel": 6,
    "iqvia_csd_keyword": 7,
}

RUNTIME_RUNNERS = (
    "job_runner.py",
    "publish_runner.py",
    "csd_channel_publish_runner.py",
    "csd_keyword_publish_runner.py",
    "iqvia_nsa_refresh_recovery_runner.py",
)


def _called_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        match node.func:
            case ast.Name(id=name):
                names.add(name)
            case ast.Attribute(attr=name):
                names.add(name)
    return names


def test_signal_is_absent_from_all_required_stage_contracts() -> None:
    assert "signal" not in _StageTracker.STAGES
    assert set(_SOURCE_STAGE_CONTRACTS) == set(EXPECTED_STAGE_COUNTS)
    assert set(STAGE_SEQUENCES) == set(EXPECTED_STAGE_COUNTS)

    for source, expected_count in EXPECTED_STAGE_COUNTS.items():
        required = tuple(stage for stage in _SOURCE_STAGE_CONTRACTS[source] if stage != "job_submit")
        assert len(required) == expected_count
        assert required[-1] == "dashboard"
        assert STAGE_SEQUENCES[source] == required


def test_signal_delivery_is_not_called_by_ingest_runners() -> None:
    runner_root = Path("pipeline/scripts/ingest_hook")
    forbidden = {"_emit_completion_signal", "_drain_completion_queue", "record_signal"}

    for filename in RUNTIME_RUNNERS:
        assert _called_names(runner_root / filename).isdisjoint(forbidden), filename
