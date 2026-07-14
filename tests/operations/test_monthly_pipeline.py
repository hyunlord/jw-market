from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from pipeline.scripts.operations.monthly_pipeline import (
    EXPECTED_STAGE_ORDER,
    PipelineError,
    execute_pipeline,
    load_spec,
)


def _command(stage: str, *, fail: bool = False) -> list[str]:
    code = (
        "import json,sys; "
        + ("sys.exit(7)" if fail else f"print(json.dumps({{'stage':'{stage}','status':'complete','requested':1,'generated':1,'validated':1}}))")
    )
    return [sys.executable, "-c", code]


def _write_spec(path: Path, *, fail_stage: str | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "stages": [
                    {
                        "name": stage,
                        "dry_command": _command(stage, fail=stage == fail_stage),
                        "full_command": _command(stage, fail=stage == fail_stage),
                        "incremental_command": _command(stage, fail=stage == fail_stage),
                    }
                    for stage in EXPECTED_STAGE_ORDER
                ]
            }
        ),
        encoding="utf-8",
    )


def test_spec_requires_the_canonical_dependency_order(tmp_path: Path) -> None:
    path = tmp_path / "spec.json"
    _write_spec(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["stages"][0], value["stages"][1] = value["stages"][1], value["stages"][0]
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(PipelineError, match="stage order"):
        load_spec(path)


def test_dry_run_completes_without_writing_checkpoint_or_epoch_state(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.json"
    _write_spec(spec_path)
    state_path = tmp_path / "state.json"
    checkpoint_path = tmp_path / "checkpoint.json"

    result = execute_pipeline(
        load_spec(spec_path),
        mode="full",
        source_epoch="epoch-1",
        dry_run=True,
        state_path=state_path,
        checkpoint_path=checkpoint_path,
        changed_brands_path=None,
    )

    assert result["status"] == "complete"
    assert [row["stage"] for row in result["stages"]] == list(EXPECTED_STAGE_ORDER)
    assert not state_path.exists()
    assert not checkpoint_path.exists()


def test_failure_stops_before_downstream_and_keeps_successful_resume_point(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.json"
    _write_spec(spec_path, fail_stage="forecast")
    checkpoint_path = tmp_path / "checkpoint.json"

    with pytest.raises(PipelineError, match="forecast failed"):
        execute_pipeline(
            load_spec(spec_path),
            mode="full",
            source_epoch="epoch-1",
            dry_run=False,
            state_path=tmp_path / "state.json",
            checkpoint_path=checkpoint_path,
            changed_brands_path=None,
        )

    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["completed_stages"] == ["mart", "cache"]


def test_same_completed_epoch_is_a_noop(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.json"
    _write_spec(spec_path)
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({"source_epoch": "epoch-1", "mode": "full", "status": "complete"}),
        encoding="utf-8",
    )

    result = execute_pipeline(
        load_spec(spec_path),
        mode="full",
        source_epoch="epoch-1",
        dry_run=False,
        state_path=state_path,
        checkpoint_path=tmp_path / "checkpoint.json",
        changed_brands_path=None,
    )

    assert result == {"status": "noop", "source_epoch": "epoch-1", "mode": "full", "stages": []}


def test_incremental_requires_a_nonempty_changed_brand_worklist(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.json"
    _write_spec(spec_path)

    with pytest.raises(PipelineError, match="changed-brands"):
        execute_pipeline(
            load_spec(spec_path),
            mode="incremental",
            source_epoch="epoch-2",
            dry_run=True,
            state_path=tmp_path / "state.json",
            checkpoint_path=tmp_path / "checkpoint.json",
            changed_brands_path=tmp_path / "missing.tsv",
        )


def test_completion_gate_rejects_partial_child_result(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.json"
    _write_spec(spec_path)
    value = json.loads(spec_path.read_text(encoding="utf-8"))
    value["stages"][0]["dry_command"] = [
        sys.executable,
        "-c",
        "import json; print(json.dumps({'stage':'mart','status':'complete','requested':2,'generated':1,'validated':1}))",
    ]
    spec_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(PipelineError, match="completion gate"):
        execute_pipeline(
            load_spec(spec_path),
            mode="full",
            source_epoch="epoch-3",
            dry_run=True,
            state_path=tmp_path / "state.json",
            checkpoint_path=tmp_path / "checkpoint.json",
            changed_brands_path=None,
        )


def test_resume_runs_only_the_remaining_suffix(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.json"
    _write_spec(spec_path)
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(
        json.dumps(
            {
                "source_epoch": "epoch-4",
                "mode": "full",
                "completed_stages": ["mart", "cache", "forecast"],
            }
        ),
        encoding="utf-8",
    )

    result = execute_pipeline(
        load_spec(spec_path),
        mode="full",
        source_epoch="epoch-4",
        dry_run=False,
        state_path=tmp_path / "state.json",
        checkpoint_path=checkpoint_path,
        changed_brands_path=None,
    )

    assert [row["stage"] for row in result["stages"]] == [
        "strength",
        "short_long",
        "events",
        "elements",
    ]
    assert not checkpoint_path.exists()


def test_child_receives_epoch_mode_and_changed_brand_contract(tmp_path: Path) -> None:
    changed = tmp_path / "changed.tsv"
    changed.write_text("brand_key\nbrand-1\n", encoding="utf-8")
    spec_path = tmp_path / "spec.json"
    _write_spec(spec_path)
    value = json.loads(spec_path.read_text(encoding="utf-8"))
    value["stages"][0]["dry_command"] = [
        sys.executable,
        "-c",
        (
            "import json,os; "
            "ok=(os.environ['PIPELINE_SOURCE_EPOCH']=='epoch-5' and "
            "os.environ['PIPELINE_MODE']=='incremental' and "
            "os.environ['PIPELINE_DRY_RUN']=='1' and "
            "os.environ['PIPELINE_CHANGED_BRANDS_FILE'].endswith('changed.tsv')); "
            "print(json.dumps({'stage':'mart','status':'complete' if ok else 'failed',"
            "'requested':1,'generated':1,'validated':1}))"
        ),
    ]
    spec_path.write_text(json.dumps(value), encoding="utf-8")

    result = execute_pipeline(
        load_spec(spec_path),
        mode="incremental",
        source_epoch="epoch-5",
        dry_run=True,
        state_path=tmp_path / "state.json",
        checkpoint_path=tmp_path / "checkpoint.json",
        changed_brands_path=changed,
    )

    assert result["status"] == "complete"
