from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.orchestrator import cli
from pipeline.orchestrator import full_rehearsal as fr
from pipeline.orchestrator.full_rehearsal import (
    FullRehearsalConfig,
    RehearsalContractError,
    build_full_rehearsal_plan,
    execute_full_rehearsal,
    load_input_manifest,
)


def _write_sources(tmp_path: Path, *, with_sidecar: bool = False) -> Path:
    ubist = tmp_path / "ubist"
    iqvia = tmp_path / "iqvia"
    ubist.mkdir()
    iqvia.mkdir()
    (ubist / "ubist-2026-05.xlsx").write_bytes(b"xlsx")
    (iqvia / "iqvia-2026-q2.csv").write_text("period,brand,value\n", encoding="utf-8")
    master = tmp_path / "mi-master.xlsx"
    master.write_bytes(b"xlsx")
    manifest = tmp_path / "inputs.json"
    payload: dict[str, object] = {
        "schema_version": 1,
        "ubist_source_dir": str(ubist),
        "iqvia_source_dir": str(iqvia),
        "mi_master": str(master),
    }
    if with_sidecar:
        sidecar = tmp_path / "sidecar.parquet"
        sidecar.write_bytes(b"parquet")
        payload["schema_version"] = 2
        payload["ubist_parquet_sidecars"] = [
            {
                "path": str(sidecar),
                "relative_path": "year=2026/month=05/data.parquet",
                "sha256": "37a0fe5ae24a60682faa103d3808c86efe98a83dd414af81d9b01eef26a3be87",
            }
        ]
    manifest.write_text(
        json.dumps(payload),
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


def test_input_manifest_rejects_tampered_sidecar(tmp_path: Path) -> None:
    manifest = _write_sources(tmp_path, with_sidecar=True)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    Path(payload["ubist_parquet_sidecars"][0]["path"]).write_bytes(b"tampered")

    with pytest.raises(RehearsalContractError, match="sidecar SHA256 mismatch"):
        load_input_manifest(manifest)


def test_input_manifest_rejects_duplicate_sidecar_destination(tmp_path: Path) -> None:
    manifest = _write_sources(tmp_path, with_sidecar=True)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["ubist_parquet_sidecars"].append(payload["ubist_parquet_sidecars"][0])
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RehearsalContractError, match="duplicate UBIST sidecar"):
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
    assert "--ubist-source-dir" in plan[0].argv
    assert "--iqvia-source-dir" in plan[1].argv
    assert "--iqvia-nsa-dir" in plan[1].argv
    assert "jw_mart_rehearsal_r1_20260718" in plan[4].argv
    assert dict(plan[5].env)["MARIADB_DATABASE"] == "jw_mart_rehearsal_r1_20260718"
    assert dict(plan[5].env)["MARIADB_SOURCE_DATABASE"] == "jw_mart_rehearsal_r1_20260718"
    assert plan[7].argv.count("jw_mart_rehearsal_r1_20260718") == 2
    assert dict(plan[10].env) == {
        "BRIDGE_DB_NAME": "jw_mart_rehearsal_r1_20260718",
        "DB_NAME": "jw_mart_rehearsal_r1_20260718",
        "GENERAL_DIMENSION_DB_NAME": "jw_mart_rehearsal_r1_20260718",
        "MALB_TARGET_DB": "jw_mart_rehearsal_r1_20260718",
        "MALB_TARGET_TABLE": "mart_analysis_level_block",
        "STRATEGIC_DIMENSION_DB_NAME": "jw_mart_rehearsal_r1_20260718",
    }
    assert "jw_mart_s6_rehearsal_r1_20260718" in plan[11].argv
    assert "jw_mart_rehearsal_r1_20260718" in plan[11].argv
    assert dict(plan[12].env)["MARIADB_DATABASE"] == "jw_mart_s6_rehearsal_r1_20260718"
    assert dict(plan[12].env)["DB_NAME"] == "jw_mart_s6_rehearsal_r1_20260718"
    assert "pipeline.scripts.etl.build_cache_deep_analysis_general" in plan[12].argv
    assert dict(plan[13].env)["MARIADB_DATABASE"] == "jw_mart_s6_rehearsal_r1_20260718"
    assert plan[13].argv[-1] == "jw_mart_d2_stage_20260630_r2"


def test_catalog_step_seeds_target_priority_and_molecule_inputs(tmp_path: Path) -> None:
    # The s2 catalog stage must be told where the two git-tracked seeds live, or a
    # fresh work_dir aborts in run_target_priority / catalog_postfix (R-1 blocker).
    manifest = _write_sources(tmp_path)
    plan = build_full_rehearsal_plan(_config(tmp_path, manifest))
    catalog = next(step for step in plan if step.key == "catalog")
    assert "--cache-dir" in catalog.argv
    assert "--inputs-dir" in catalog.argv
    cache_dir = catalog.argv[catalog.argv.index("--cache-dir") + 1]
    inputs_dir = catalog.argv[catalog.argv.index("--inputs-dir") + 1]
    assert cache_dir.endswith("data/cache")
    assert inputs_dir.endswith("inputs")


def test_catalog_consumers_share_one_canonical_catalog_root(tmp_path: Path) -> None:
    manifest = _write_sources(tmp_path)
    plan = build_full_rehearsal_plan(_config(tmp_path, manifest))
    expected = str((tmp_path / "work" / "output" / "catalog").resolve())

    for key in ("catalog", "enrich", "general_mart", "strategic_mart", "bridge"):
        step = next(item for item in plan if item.key == key)
        assert "--catalog-root" in step.argv, f"{key} lost the canonical catalog contract"
        index = step.argv.index("--catalog-root")
        assert step.argv[index + 1] == expected

    cache = next(item for item in plan if item.key == "cache")
    assert "--catalog-root" not in cache.argv


def test_plan_installs_pinned_ubist_sidecar_before_downstream_stages(tmp_path: Path) -> None:
    manifest = _write_sources(tmp_path, with_sidecar=True)
    plan = build_full_rehearsal_plan(_config(tmp_path, manifest))

    assert [step.key for step in plan[:4]] == [
        "load_ubist",
        "install_ubist_sidecars",
        "load_iqvia",
        "catalog",
    ]
    install = plan[1]
    assert "pipeline.orchestrator.full_rehearsal_ubist_sidecars" in install.argv
    assert str(manifest) in install.argv
    assert str((tmp_path / "work" / "ubist").resolve()) in install.argv
    assert all(not step.writes_operating for step in plan)


def test_load_ubist_excludes_pinned_sidecar_months(tmp_path: Path) -> None:
    manifest = _write_sources(tmp_path, with_sidecar=True)
    plan = build_full_rehearsal_plan(_config(tmp_path, manifest))

    load_ubist = plan[0]
    assert load_ubist.key == "load_ubist"
    argv = load_ubist.argv
    # The pinned month is skipped by s1 and left for install_ubist_sidecars.
    assert argv.count("--exclude-ubist-month") == 1
    idx = argv.index("--exclude-ubist-month")
    assert argv[idx + 1] == "2026-05"
    # It still runs in replace mode and reads the raw source dir for all other months.
    assert "--ubist-mode" in argv and argv[argv.index("--ubist-mode") + 1] == "replace"
    assert "--ubist-source-dir" in argv


def test_load_ubist_without_sidecars_excludes_nothing(tmp_path: Path) -> None:
    manifest = _write_sources(tmp_path)  # schema_version 1, no sidecars
    plan = build_full_rehearsal_plan(_config(tmp_path, manifest))

    # Regression guard: the plain (non-sidecar) load path is byte-for-byte
    # unchanged — no exclusion flags are injected.
    assert "--exclude-ubist-month" not in plan[0].argv


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
    assert payload[-1]["key"] == "brand_elements_cache"
    assert all(row["writes_operating"] is False for row in payload)


def test_cli_accepts_content_addressed_checkpoint_resume_contract(tmp_path: Path) -> None:
    args = cli._build_parser().parse_args(
        [
            "rehearse-full",
            "--input-manifest",
            str(tmp_path / "input_manifest.json"),
            "--input-inventory",
            str(tmp_path / "input_inventory.json"),
            "--target-db",
            "jw_mart_rehearsal_test",
            "--cache-db",
            "jw_mart_s6_rehearsal_test",
            "--source-db",
            "jw_mart_source",
            "--work-dir",
            str(tmp_path / "work"),
            "--checkpoint",
            str(tmp_path / "checkpoints"),
            "--start-at",
            "s2",
        ]
    )

    assert args.checkpoint == tmp_path / "checkpoints"
    assert args.start_at == "s2"
    assert args.input_inventory == tmp_path / "input_inventory.json"


class _FakeResult:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def test_execute_emits_stage_markers_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write_sources(tmp_path, with_sidecar=True)
    config = _config(tmp_path, manifest)
    monkeypatch.setattr(fr.subprocess, "run", lambda *a, **k: _FakeResult(0))

    rc = execute_full_rehearsal(config, dry_run=False)

    assert rc == 0
    out = capsys.readouterr().out
    # Every planned step brackets a start/end rc=0 marker, ending with a complete line.
    assert "[stage] load_ubist start (1/" in out
    assert "[stage] install_ubist_sidecars start (2/" in out
    assert "[stage] install_ubist_sidecars end rc=0" in out
    assert "[stage] rehearse-full complete rc=0" in out


def test_execute_marks_failing_step_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write_sources(tmp_path, with_sidecar=True)
    config = _config(tmp_path, manifest)

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]
        # Fail exactly at install_ubist_sidecars (the historical R-1 failure step).
        # argv elements carry the full module path, so match on substring.
        return _FakeResult(2 if any("full_rehearsal_ubist_sidecars" in a for a in argv) else 0)

    monkeypatch.setattr(fr.subprocess, "run", run)

    rc = execute_full_rehearsal(config, dry_run=False)

    assert rc == 2
    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert "[stage] install_ubist_sidecars start (2/" in out
    assert "[stage] install_ubist_sidecars end rc=2" in out
    # Stopped at the failing step: downstream stages never start.
    assert "[stage] load_iqvia start" not in out
    assert "[stage] rehearse-full complete" not in out
    assert "rehearsal failed step=install_ubist_sidecars rc=2" in err


def test_s2_resume_restores_checkpoint_and_never_runs_s1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _write_sources(tmp_path)
    inventory = tmp_path / "input_inventory.json"
    inventory.write_text(json.dumps({"objects": []}), encoding="utf-8")
    config = FullRehearsalConfig(
        input_manifest=manifest,
        target_db="jw_mart_rehearsal_r1_20260718",
        cache_db="jw_mart_s6_rehearsal_r1_20260718",
        source_db="jw_mart_d2_stage_20260630_r2",
        work_dir=tmp_path / "work",
        input_inventory=inventory,
        checkpoint_root=tmp_path / "checkpoints",
        start_at="s2",
    )
    monkeypatch.setenv("R1_IMAGE_DIGEST", f"sha256:{'a' * 64}")
    monkeypatch.setenv("R1_GIT_COMMIT", "b" * 40)
    monkeypatch.setenv("R1_SOURCE_SUBPATH", "snapshot")
    restored: list[str] = []
    executed: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        fr,
        "_restore_s1_checkpoint",
        lambda _config, checkpoint_id: restored.append(checkpoint_id or ""),
    )
    monkeypatch.setattr(
        fr.subprocess,
        "run",
        lambda argv, **_kwargs: executed.append(tuple(argv)) or _FakeResult(0),
    )

    assert execute_full_rehearsal(config, dry_run=False) == 0
    assert len(restored) == 1
    rendered = "\n".join(" ".join(argv) for argv in executed)
    assert "--stage s1" not in rendered
    assert "--stage s2" in rendered


def test_checkpoint_is_published_only_after_iqvia_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _write_sources(tmp_path)
    inventory = tmp_path / "input_inventory.json"
    inventory.write_text(json.dumps({"objects": []}), encoding="utf-8")
    config = FullRehearsalConfig(
        input_manifest=manifest,
        target_db="jw_mart_rehearsal_r1_20260718",
        cache_db="jw_mart_s6_rehearsal_r1_20260718",
        source_db="jw_mart_d2_stage_20260630_r2",
        work_dir=tmp_path / "work",
        input_inventory=inventory,
        checkpoint_root=tmp_path / "checkpoints",
    )
    monkeypatch.setenv("R1_IMAGE_DIGEST", f"sha256:{'a' * 64}")
    monkeypatch.setenv("R1_GIT_COMMIT", "b" * 40)
    monkeypatch.setenv("R1_SOURCE_SUBPATH", "snapshot")
    events: list[str] = []

    def run(argv, **_kwargs):  # type: ignore[no-untyped-def]
        if "--source" in argv:
            events.append(argv[argv.index("--source") + 1])
        return _FakeResult(0)

    monkeypatch.setattr(fr.subprocess, "run", run)
    monkeypatch.setattr(
        fr,
        "_publish_s1_checkpoint",
        lambda _config, _checkpoint_id: events.append("publish"),
    )

    assert execute_full_rehearsal(config, dry_run=False) == 0
    assert events[:3] == ["ubist", "iqvia", "publish"]
