from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.orchestrator import cli
from pipeline.orchestrator.incremental_rehearsal import (
    IncrementalRehearsalConfig,
    build_incremental_refresh_plan,
    execute_incremental_rehearsal,
    install_holdout_inputs,
    prepare_incremental_inputs,
)
from pipeline.orchestrator.full_rehearsal import RehearsalContractError


def _write_config(tmp_path: Path) -> IncrementalRehearsalConfig:
    raw = tmp_path / "raw"
    ubist = raw / "ubist"
    iqvia = raw / "iqvia"
    holdout = ubist / "monthly" / "may.xlsx"
    baseline = ubist / "history" / "history.xlsx"
    holdout.parent.mkdir(parents=True)
    baseline.parent.mkdir(parents=True)
    iqvia.mkdir(parents=True)
    holdout.write_bytes(b"may-workbook")
    baseline.write_bytes(b"history-workbook")
    (iqvia / "nsa.csv").write_text("period,brand,value\n", encoding="utf-8")
    master = raw / "mi-master.xlsx"
    master.write_bytes(b"mi-master")
    full_manifest = raw / "full-inputs.json"
    full_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ubist_source_dir": str(ubist),
                "iqvia_source_dir": str(iqvia),
                "mi_master": str(master),
            }
        ),
        encoding="utf-8",
    )
    submission = raw / "submission.json"
    submission.write_text(
        json.dumps(
            {
                "contract_version": "v2",
                "epoch": "2026-05",
                "category": "ubist",
                "complete": True,
                "submitted_at": "2026-07-18T09:00:00+09:00",
                "files": [
                    {
                        "path": "incoming/may.xlsx",
                        "sha256": hashlib.sha256(holdout.read_bytes()).hexdigest(),
                        "period_start": "2026-05",
                        "period_end": "2026-05",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return IncrementalRehearsalConfig(
        full_input_manifest=full_manifest,
        submission_manifest=submission,
        target_db="jw_mart_rehearsal_r2_20260718",
        cache_db="jw_mart_s6_rehearsal_r2_20260718",
        source_db="jw_mart_d2_stage_20260630_r2",
        reference_db="jw_mart_rehearsal_r1_20260718b",
        reference_cache_db="jw_mart_s6_rehearsal_r1_20260718b",
        work_dir=tmp_path / "work",
        comparison_output=tmp_path / "evidence" / "r2-comparison.json",
    )


def test_prepare_matches_holdout_by_sha_and_builds_full_minus_increment(tmp_path):
    config = _write_config(tmp_path)

    prepared = prepare_incremental_inputs(config)

    baseline_ubist = prepared.baseline_ubist_dir
    assert [path.relative_to(baseline_ubist).as_posix() for path in baseline_ubist.rglob("*.xlsx")] == [
        "history/history.xlsx"
    ]
    assert len(prepared.holdouts) == 1
    assert prepared.holdouts[0].relative_path.as_posix() == "monthly/may.xlsx"
    assert not (baseline_ubist / "monthly" / "may.xlsx").exists()

    adjusted = install_holdout_inputs(prepared)

    assert (baseline_ubist / "monthly" / "may.xlsx").read_bytes() == b"may-workbook"
    assert [entry.path for entry in adjusted.files] == ["monthly/may.xlsx"]


def test_incremental_refresh_reuses_isolated_full_outputs_from_catalog_onward(tmp_path):
    config = _write_config(tmp_path)
    prepared = prepare_incremental_inputs(config)

    plan = build_incremental_refresh_plan(config, prepared.baseline_manifest)

    assert [step.key for step in plan] == [
        "catalog",
        "enrich",
        "general_mart",
        "general_dimension",
        "strategic_mart",
        "strategic_dimension",
        "bridge",
        "prepare_malb",
        "analysis_blocks",
        "cache",
        "general_deep_cache",
        "brand_elements_cache",
    ]
    assert all(not step.writes_operating for step in plan)
    assert all("jw_mart_d2_stage_20260630_r2" not in step.argv for step in plan[:7])


def test_config_rejects_unsafe_reference_schema_before_execution(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    unsafe = IncrementalRehearsalConfig(
        **{
            **config.__dict__,
            "reference_db": "jw_mart_rehearsal_r1;DROP_TABLE",
        }
    )

    with pytest.raises(RehearsalContractError, match="reference_db"):
        unsafe.validate()


def test_execute_runs_full_then_incremental_then_sigma_then_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    events: list[str] = []

    monkeypatch.setattr(
        "pipeline.orchestrator.incremental_rehearsal.execute_full_rehearsal",
        lambda *_args, **_kwargs: events.append("full-minus") or 0,
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.incremental_rehearsal._validate_and_load",
        lambda *_args, **_kwargs: events.append("g3-load")
        or SimpleNamespace(epoch="2026-05", observed_periods={"2026-05"}),
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.incremental_rehearsal._execute_steps",
        lambda _steps: events.append("refresh") or 0,
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.incremental_rehearsal._check_market_sigma",
        lambda *_args: events.append("sigma"),
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.incremental_rehearsal.run_comparison",
        lambda *_args: events.append("compare") or 0,
    )

    rc = execute_incremental_rehearsal(config, dry_run=False)

    assert rc == 0
    assert events == ["full-minus", "g3-load", "refresh", "sigma", "compare"]


def test_execute_stops_before_sigma_and_comparison_when_refresh_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    events: list[str] = []

    monkeypatch.setattr(
        "pipeline.orchestrator.incremental_rehearsal.execute_full_rehearsal",
        lambda *_args, **_kwargs: events.append("full-minus") or 0,
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.incremental_rehearsal._validate_and_load",
        lambda *_args, **_kwargs: events.append("g3-load")
        or SimpleNamespace(epoch="2026-05", observed_periods={"2026-05"}),
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.incremental_rehearsal._execute_steps",
        lambda _steps: events.append("refresh") or 17,
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.incremental_rehearsal._check_market_sigma",
        lambda *_args: events.append("sigma"),
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.incremental_rehearsal.run_comparison",
        lambda *_args: events.append("compare") or 0,
    )

    rc = execute_incremental_rehearsal(config, dry_run=False)

    assert rc == 17
    assert events == ["full-minus", "g3-load", "refresh"]


def test_rehearse_incremental_cli_dry_run_is_write_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)

    rc = cli.main(
        [
            "rehearse-incremental",
            "--full-input-manifest",
            str(config.full_input_manifest),
            "--submission-manifest",
            str(config.submission_manifest),
            "--target-db",
            config.target_db,
            "--cache-db",
            config.cache_db,
            "--source-db",
            config.source_db,
            "--reference-db",
            config.reference_db,
            "--reference-cache-db",
            config.reference_cache_db,
            "--work-dir",
            str(config.work_dir),
            "--comparison-output",
            str(config.comparison_output),
            "--dry-run",
        ]
    )

    assert rc == 0
    assert not config.work_dir.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["gate"] == "R-2"
    assert payload["writes_operating"] is False
    assert payload["phases"][-1] == "exact-comparison"
