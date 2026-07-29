"""Sweep counters distinguish actionable, skipped, launched, and failed work."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook import sweep as sweep_module
from pipeline.scripts.ingest_hook.contract import Manifest, ManifestFile


def _manifest(*, complete: bool = True, sha: str = "a" * 64) -> Manifest:
    return Manifest(
        contract_version="v2",
        epoch="2026-03",
        category="ubist",
        complete=complete,
        files=(ManifestFile(path="demo.xlsx", sha256="b" * 64),),
        manifest_path="/input/manifest.json",
        manifest_sha=sha,
    )


def _one_manifest(monkeypatch: pytest.MonkeyPatch, manifest: Manifest) -> None:
    monkeypatch.setattr(
        sweep_module,
        "_iter_manifests",
        lambda _root, _s3: iter(
            [(Path(manifest.manifest_path), lambda: manifest)]
        ),
    )


def test_sweep_noop_contract_excludes_current_rows_from_found(
    sqlite_ledger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest()
    sqlite_ledger.receive(
        manifest.epoch,
        manifest.category,
        manifest.manifest_sha,
        manifest_path=manifest.manifest_path,
    )
    sqlite_ledger.mark_complete(
        manifest.epoch,
        manifest.category,
        manifest.manifest_sha,
        row_counts={"demo.xlsx": 1},
    )
    _one_manifest(monkeypatch, manifest)

    result = sweep_module.sweep(sqlite_ledger, tmp_path)

    assert result["found"] == 0
    assert result["kicked"] == 0
    assert result["skipped"] == 1
    assert result["failed"] == 0


def test_sweep_counts_only_a_real_job_name_as_kicked(
    sqlite_ledger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest()
    _one_manifest(monkeypatch, manifest)
    monkeypatch.setattr(
        sweep_module.IngestService,
        "promote",
        lambda _service, _category: None,
    )

    result = sweep_module.sweep(sqlite_ledger, tmp_path)

    assert result["found"] == 1
    assert result["kicked"] == 0
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert result["actions"][-1]["action"] == "deferred"


def test_sweep_counts_promotion_exception_as_failed_not_kicked(
    sqlite_ledger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest()
    _one_manifest(monkeypatch, manifest)

    def fail_promotion(_service, _category):
        raise RuntimeError("injected submission failure")

    monkeypatch.setattr(sweep_module.IngestService, "promote", fail_promotion)

    result = sweep_module.sweep(sqlite_ledger, tmp_path)

    assert result["found"] == 1
    assert result["kicked"] == 0
    assert result["skipped"] == 0
    assert result["failed"] == 1
    assert result["actions"][-1]["action"] == "failed"


def test_sweep_counts_successful_promotion_once(
    sqlite_ledger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest()
    _one_manifest(monkeypatch, manifest)
    monkeypatch.setattr(
        sweep_module.IngestService,
        "promote",
        lambda _service, _category: "jw-ingest-ubist-test",
    )

    result = sweep_module.sweep(sqlite_ledger, tmp_path)

    assert result["found"] == 1
    assert result["kicked"] == 1
    assert result["skipped"] == 0
    assert result["failed"] == 0
