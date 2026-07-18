from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.orchestrator import cli
from pipeline.orchestrator.full_rehearsal import (
    FullRehearsalConfig,
    RehearsalContractError,
    build_full_rehearsal_plan,
    load_input_manifest,
)


def _write_sources(tmp_path: Path) -> Path:
    ubist = tmp_path / "ubist"
    iqvia = tmp_path / "iqvia"
    ubist.mkdir()
    iqvia.mkdir()
    (ubist / "ubist-2026-05.xlsx").write_bytes(b"xlsx")
    (iqvia / "iqvia-2026-q2.csv").write_text("period,brand,value\n", encoding="utf-8")
    master = tmp_path / "mi-master.xlsx"
    master.write_bytes(b"xlsx")
    manifest = tmp_path / "inputs.json"
    manifest.write_text(
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
    return manifest


def _config(tmp_path: Path, manifest: Path) -> FullRehearsalConfig:
    return FullRehearsalConfig(
        input_manifest=manifest,
        target_db="jw_mart_rehearsal_r1_20260718",
        cache_db="jw_mart_s6_rehearsal_r1_20260718",
        source_db="jw_mart_d2_stage_20260630_r2",
        work_dir=tmp_path / "work",
    )


def test_input_manifest_requires_all_explicit_full_sources(tmp_path: Path) -> None:
    manifest = tmp_path / "inputs.json"
    manifest.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

    with pytest.raises(RehearsalContractError, match="ubist_source_dir"):
        load_input_manifest(manifest)


def test_input_manifest_rejects_empty_source_directories(tmp_path: Path) -> None:
    manifest = _write_sources(tmp_path)
    for path in (tmp_path / "ubist").iterdir():
        path.unlink()

    with pytest.raises(RehearsalContractError, match="UBIST source files"):
        load_input_manifest(manifest)


@pytest.mark.parametrize(
    ("target_db", "cache_db"),
    [
        ("jw_mart", "jw_mart_s6_rehearsal_r1"),
        ("jw_mart_d2_stage_20260630_r2", "jw_mart_s6_rehearsal_r1"),
        ("jw_mart_rehearsal_r1", "jw_mart"),
        ("not_isolated", "jw_mart_s6_rehearsal_r1"),
        ("jw_mart_rehearsal_r1", "not_isolated"),
    ],
)
def test_config_rejects_operating_or_nonisolated_targets(
    tmp_path: Path, target_db: str, cache_db: str
) -> None:
    manifest = _write_sources(tmp_path)

    with pytest.raises(RehearsalContractError):
        FullRehearsalConfig(
            input_manifest=manifest,
            target_db=target_db,
            cache_db=cache_db,
            source_db="jw_mart_d2_stage_20260630_r2",
            work_dir=tmp_path / "work",
        ).validate()


def test_plan_builds_raw_to_mart_then_separate_cache_chain(tmp_path: Path) -> None:
    manifest = _write_sources(tmp_path)
    plan = build_full_rehearsal_plan(_config(tmp_path, manifest))

    assert [step.key for step in plan] == [
        "load_ubist",
        "load_iqvia",
        "catalog",
        "enrich",
        "general_mart",
        "strategic_mart",
        "bridge",
        "cache",
    ]
    assert all(not step.writes_operating for step in plan)
    assert "--ubist-source-dir" in plan[0].argv
    assert "--iqvia-source-dir" in plan[1].argv
    assert "--iqvia-nsa-dir" in plan[1].argv
    assert "jw_mart_rehearsal_r1_20260718" in plan[4].argv
    assert "jw_mart_s6_rehearsal_r1_20260718" in plan[-1].argv
    assert "jw_mart_rehearsal_r1_20260718" in plan[-1].argv


def test_rehearse_full_dry_run_prints_plan_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write_sources(tmp_path)
    work_dir = tmp_path / "never-created"

    def fail_subprocess(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("dry-run must not execute subprocesses")

    monkeypatch.setattr("pipeline.orchestrator.full_rehearsal.subprocess.run", fail_subprocess)
    rc = cli.main(
        [
            "rehearse-full",
            "--input-manifest",
            str(manifest),
            "--target-db",
            "jw_mart_rehearsal_r1_20260718",
            "--cache-db",
            "jw_mart_s6_rehearsal_r1_20260718",
            "--source-db",
            "jw_mart_d2_stage_20260630_r2",
            "--work-dir",
            str(work_dir),
            "--dry-run",
        ]
    )

    assert rc == 0
    assert not work_dir.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["key"] == "load_ubist"
    assert payload[-1]["key"] == "cache"
    assert all(row["writes_operating"] is False for row in payload)
