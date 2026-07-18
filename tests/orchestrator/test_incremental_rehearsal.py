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
    prepare_incremental_inputs,
)
from pipeline.orchestrator.full_rehearsal import RehearsalContractError


def _write_config(tmp_path: Path) -> IncrementalRehearsalConfig:
    raw = tmp_path / "raw"
    ubist = raw / "ubist"
    iqvia = raw / "iqvia"
    submission_root = raw / "submission"
    holdout = submission_root / "holdout" / "may.xlsx"
    baseline = ubist / "history" / "history.xlsx"
    holdout.parent.mkdir(parents=True)
    baseline.parent.mkdir(parents=True)
    iqvia.mkdir(parents=True)
    holdout.write_bytes(b"may-workbook")
    baseline.write_bytes(b"history-workbook")
    (iqvia / "nsa.csv").write_text("period,brand,value\n", encoding="utf-8")
    master = raw / "mi-master.xlsx"
    master.write_bytes(b"mi-master")
    sidecar = raw / "sidecars" / "year=2026" / "month=05" / "data.parquet"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_bytes(b"canonical-may-parquet")
    full_manifest = raw / "full-inputs.json"
    full_manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "ubist_source_dir": str(ubist),
                "iqvia_source_dir": str(iqvia),
                "mi_master": str(master),
                "ubist_parquet_sidecars": [
                    {
                        "path": str(sidecar),
                        "relative_path": "year=2026/month=05/data.parquet",
                        "sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest(),
                    }
                ],
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
                        "path": "holdout/may.xlsx",
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
        submission_source_dir=submission_root,
        target_db="jw_mart_rehearsal_r2_20260718",
        cache_db="jw_mart_s6_rehearsal_r2_20260718",
        source_db="jw_mart_d2_stage_20260630_r2",
        reference_db="jw_mart_rehearsal_r1_20260718b",
        reference_cache_db="jw_mart_s6_rehearsal_r1_20260718b",
        work_dir=tmp_path / "work",
        comparison_output=tmp_path / "evidence" / "r2-comparison.json",
    )


def test_prepare_holds_out_submission_epoch_sidecar_and_keeps_xlsx_separate(tmp_path):
    config = _write_config(tmp_path)

    prepared = prepare_incremental_inputs(config)

    baseline_ubist = prepared.baseline_ubist_dir
    assert [path.relative_to(baseline_ubist).as_posix() for path in baseline_ubist.rglob("*.xlsx")] == [
        "history/history.xlsx"
    ]
    assert [item.relative_path.as_posix() for item in prepared.held_out_sidecars] == [
        "year=2026/month=05/data.parquet"
    ]
    baseline_manifest = json.loads(prepared.baseline_manifest.read_text(encoding="utf-8"))
    assert baseline_manifest["schema_version"] == 1
    assert "ubist_parquet_sidecars" not in baseline_manifest
    assert (config.submission_source_dir / "holdout" / "may.xlsx").read_bytes() == b"may-workbook"


def test_prepare_preserves_sidecars_outside_submission_epoch(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    full_manifest = json.loads(config.full_input_manifest.read_text(encoding="utf-8"))
    april = config.full_input_manifest.parent / "sidecars" / "year=2026" / "month=04" / "data.parquet"
    april.parent.mkdir(parents=True)
    april.write_bytes(b"canonical-april-parquet")
    full_manifest["ubist_parquet_sidecars"].append(
        {
            "path": str(april),
            "relative_path": "year=2026/month=04/data.parquet",
            "sha256": hashlib.sha256(april.read_bytes()).hexdigest(),
        }
    )
    config.full_input_manifest.write_text(json.dumps(full_manifest), encoding="utf-8")

    prepared = prepare_incremental_inputs(config)

    baseline_manifest = json.loads(prepared.baseline_manifest.read_text(encoding="utf-8"))
    assert baseline_manifest["schema_version"] == 2
    assert [row["relative_path"] for row in baseline_manifest["ubist_parquet_sidecars"]] == [
        "year=2026/month=04/data.parquet"
    ]


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
    assert "install_ubist_sidecars" not in [step.key for step in plan]


def test_prepare_rejects_submission_without_matching_full_sidecar(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    payload = json.loads(config.full_input_manifest.read_text(encoding="utf-8"))
    payload["ubist_parquet_sidecars"] = []
    payload["schema_version"] = 1
    config.full_input_manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RehearsalContractError, match="2026-05 sidecar"):
        prepare_incremental_inputs(config)


def test_prepare_rejects_multiple_sidecars_for_submission_epoch(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    payload = json.loads(config.full_input_manifest.read_text(encoding="utf-8"))
    duplicate_epoch = (
        config.full_input_manifest.parent
        / "sidecars"
        / "year=2026"
        / "month=05"
        / "second.parquet"
    )
    duplicate_epoch.write_bytes(b"second-canonical-may-parquet")
    payload["ubist_parquet_sidecars"].append(
        {
            "path": str(duplicate_epoch),
            "relative_path": "year=2026/month=05/second.parquet",
            "sha256": hashlib.sha256(duplicate_epoch.read_bytes()).hexdigest(),
        }
    )
    config.full_input_manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RehearsalContractError, match="exactly one 2026-05 sidecar"):
        prepare_incremental_inputs(config)


def test_prepare_rejects_missing_submission_source_file(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    (config.submission_source_dir / "holdout" / "may.xlsx").unlink()

    with pytest.raises(RehearsalContractError, match="submission source is missing"):
        prepare_incremental_inputs(config)


def test_prepare_rejects_submission_source_sha_mismatch(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    (config.submission_source_dir / "holdout" / "may.xlsx").write_bytes(b"changed")

    with pytest.raises(RehearsalContractError, match="SHA256 mismatch"):
        prepare_incremental_inputs(config)


def test_prepare_rejects_submission_period_outside_epoch(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    payload = json.loads(config.submission_manifest.read_text(encoding="utf-8"))
    payload["files"][0]["period_start"] = "2026-04"
    config.submission_manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RehearsalContractError, match="cover epoch 2026-05 exactly"):
        prepare_incremental_inputs(config)


def test_prepare_rejects_submission_path_escape(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    payload = json.loads(config.submission_manifest.read_text(encoding="utf-8"))
    payload["files"][0]["path"] = "../may.xlsx"
    config.submission_manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RehearsalContractError, match="unsafe submission source path"):
        prepare_incremental_inputs(config)


def test_prepare_rejects_submission_symlink_escape(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    source = config.submission_source_dir / "holdout" / "may.xlsx"
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(outside)

    with pytest.raises(RehearsalContractError, match="escapes submission source directory"):
        prepare_incremental_inputs(config)


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
    comparison_contract: dict[str, str] = {}
    load_roots: list[Path] = []

    monkeypatch.setattr(
        "pipeline.orchestrator.incremental_rehearsal.execute_full_rehearsal",
        lambda *_args, **_kwargs: events.append("full-minus") or 0,
    )
    def validate_and_load(*args, **_kwargs):
        events.append("g3-load")
        load_roots.append(args[2])
        return SimpleNamespace(epoch="2026-05", observed_periods={"2026-05"})

    monkeypatch.setattr(
        "pipeline.orchestrator.incremental_rehearsal._validate_and_load",
        validate_and_load,
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.incremental_rehearsal._execute_steps",
        lambda _steps: events.append("refresh") or 0,
    )
    monkeypatch.setattr(
        "pipeline.orchestrator.incremental_rehearsal._check_market_sigma",
        lambda *_args: events.append("sigma"),
    )
    def compare(*_args, **kwargs) -> int:
        events.append("compare")
        comparison_contract.update(kwargs)
        return 0

    monkeypatch.setattr(
        "pipeline.orchestrator.incremental_rehearsal.run_comparison",
        compare,
    )

    rc = execute_incremental_rehearsal(config, dry_run=False)

    assert rc == 0
    assert events == ["full-minus", "g3-load", "refresh", "sigma", "compare"]
    assert load_roots == [config.submission_source_dir]
    assert comparison_contract == {
        "gate": "R-2",
        "environment": "isolated-full-then-incremental",
    }


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
            "--submission-source-dir",
            str(config.submission_source_dir),
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
