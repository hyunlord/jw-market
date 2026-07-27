from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from pipeline.scripts.ingest_hook import correction_gate
from pipeline.scripts.ingest_hook.contract import Manifest, ManifestFile


CATEGORIES = (
    "ubist",
    "iqvia_nsa",
    "iqvia_csd_channel",
    "iqvia_csd_keyword",
)

DECLARED_MANIFESTS = (
    "deploy/k8s/ingest-hook/ingest-trigger-deployment.yaml",
    "deploy/k8s/ingest-hook/reference/ingest-job-template.yaml",
    "deploy/k8s/ingest-hook/reference/ingest-job-activation-overlay.yaml",
    "deploy/k8s/ingest-hook/reference/ingest-job-activation-test2-overlay.yaml",
    "deploy/k8s/ingest-hook/reference/ingest-job-shadow-overlay.yaml",
    "deploy/k8s/ingest-hook/reference/ingest-trigger-production-overlay.yaml",
    "deploy/k8s/ingest-hook/reference/ingest-trigger-shadow-overlay.yaml",
)


def _manifest(
    category: str,
    *,
    epoch: str = "2026-07",
    sha256: str = "a" * 64,
) -> Manifest:
    return Manifest(
        contract_version="v2",
        epoch=epoch,
        category=category,
        complete=True,
        files=(
            ManifestFile(
                path=f"{category}/{epoch}/canonical.xlsx",
                sha256=sha256,
                rows=10,
                period_start=epoch,
                period_end=epoch,
            ),
        ),
    )


def _prior(manifest: Manifest, *, sha256: str = "a" * 64):
    return (
        correction_gate.StoredFileRevision(
            logical_identity=correction_gate.logical_file_identity(
                manifest.category,
                manifest.epoch,
                manifest.files[0].path,
            ),
            sha256=sha256,
            loaded_at="2026-07-26T12:34:56+00:00",
        ),
    )


def _declared_env(path: str) -> dict[str, str]:
    resources = tuple(yaml.safe_load_all(Path(path).read_text(encoding="utf-8")))
    resource = next(
        item for item in resources if item["kind"] in {"Deployment", "Job"}
    )
    container = resource["spec"]["template"]["spec"]["containers"][0]
    return {
        item["name"]: item["value"]
        for item in container["env"]
        if "name" in item and "value" in item
    }


@pytest.mark.parametrize("category", CATEGORIES)
def test_same_content_reupload_is_allowed_as_noop(category: str) -> None:
    manifest = _manifest(category)

    result = correction_gate.assess(manifest, _prior(manifest))

    assert result.status == "noop"
    assert result.conflicts == ()
    print(json.dumps({"case": "same_content", "category": category, "status": result.status}))


@pytest.mark.parametrize("category", CATEGORIES)
def test_same_logical_file_with_different_content_is_rejected(category: str) -> None:
    manifest = _manifest(category, sha256="b" * 64)

    with pytest.raises(
        correction_gate.CorrectionRejected,
        match="정정본 충돌.*canonical.xlsx.*2026-07.*정정 절차",
    ) as captured:
        correction_gate.assess(manifest, _prior(manifest))
    print(
        json.dumps(
            {
                "case": "changed_content",
                "category": category,
                "status": "rejected",
                "reason": str(captured.value),
            },
            ensure_ascii=False,
        )
    )


def test_same_name_in_a_different_period_is_new_input() -> None:
    current = _manifest("ubist", epoch="2026-08")
    previous = _manifest("ubist", epoch="2026-07")

    result = correction_gate.assess(current, _prior(previous))

    assert result.status == "new"
    assert result.conflicts == ()
    print(json.dumps({"case": "different_period", "status": result.status}))


def test_disabled_gate_preserves_existing_behavior_without_reading_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest("ubist", sha256="b" * 64)
    calls: list[str] = []

    monkeypatch.setenv(correction_gate.ENV_REQUIRE_CORRECTION_REJECT_GATE, "0")
    result = correction_gate.enforce(
        manifest,
        revision_reader=lambda _: calls.append("read") or _prior(manifest),
    )

    assert result.status == "disabled"
    assert calls == []
    print(json.dumps({"case": "flag_zero", "status": result.status, "history_reads": 0}))


@pytest.mark.parametrize("manifest_path", DECLARED_MANIFESTS)
def test_all_ingest_manifests_declare_correction_gate_disabled(
    manifest_path: str,
) -> None:
    assert _declared_env(manifest_path)["REQUIRE_CORRECTION_REJECT_GATE"] == "0"


def test_enabled_gate_does_not_claim_an_identity_for_other_categories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest("mi_master")
    calls: list[str] = []

    monkeypatch.setenv(correction_gate.ENV_REQUIRE_CORRECTION_REJECT_GATE, "1")
    result = correction_gate.enforce(
        manifest,
        revision_reader=lambda _: calls.append("read") or (),
    )

    assert result.status == "not_applicable"
    assert calls == []


def test_reader_uses_existing_publication_inventory_without_writes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.scripts.ingest_hook import config

    database = tmp_path / "provenance.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE mart_publication_provenance (
          mart_publication_epoch INTEGER PRIMARY KEY,
          category TEXT NOT NULL,
          epoch TEXT NOT NULL,
          input_inventory_json TEXT NOT NULL,
          published_at_utc TEXT NOT NULL
        )
        """
    )
    inventory = json.dumps(
        [{"path": "archive/canonical.xlsx", "sha256": "a" * 64, "rows": 10}]
    )
    connection.execute(
        "INSERT INTO mart_publication_provenance VALUES (?, ?, ?, ?, ?)",
        (7, "iqvia_nsa", "2026-07", inventory, "2026-07-26T12:34:56+00:00"),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(
        config,
        "open_mart_connection",
        lambda: sqlite3.connect(database),
    )

    revisions = correction_gate.read_stored_revisions(_manifest("iqvia_nsa"))

    assert revisions == _prior(_manifest("iqvia_nsa"))
    verify = sqlite3.connect(database)
    try:
        assert verify.total_changes == 0
        assert verify.execute(
            "SELECT COUNT(*) FROM mart_publication_provenance"
        ).fetchone() == (1,)
    finally:
        verify.close()


def test_rejection_happens_before_loader_and_marks_ledger_failed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.scripts.ingest_hook import job_runner
    from pipeline.scripts.ingest_hook.contract import parse_manifest_bytes
    from pipeline.scripts.ingest_hook.ledger import open_sqlite_ledger

    manifest = _manifest("ubist", sha256="b" * 64)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        """
        {
          "contract_version": "v2",
          "epoch": "2026-07",
          "category": "ubist",
          "complete": true,
          "files": [{
            "path": "ubist/2026-07/canonical.xlsx",
            "sha256": "%s",
            "rows": 10,
            "period_start": "2026-07",
            "period_end": "2026-07"
          }]
        }
        """
        % ("b" * 64),
        encoding="utf-8",
    )
    parsed = parse_manifest_bytes(
        manifest_path.read_bytes(),
        manifest_path=str(manifest_path),
    )
    ledger = open_sqlite_ledger(tmp_path / "ledger.sqlite")
    loader_calls: list[str] = []

    monkeypatch.setenv(
        "INGEST_LOAD_STAGING_ROOT",
        str(tmp_path / "load-staging"),
    )
    monkeypatch.setattr(
        job_runner,
        "_run_correction_gate",
        lambda current: correction_gate.assess(current, _prior(manifest)),
    )
    monkeypatch.setattr(
        job_runner,
        "_real_load",
        lambda *_args, **_kwargs: loader_calls.append("load"),
    )

    rc = job_runner.run(
        manifest_path,
        input_root=tmp_path,
        ledger=ledger,
        rehearsal_root=None,
        run_id="correction-reject",
    )

    entry = ledger.status(parsed.epoch, parsed.category, parsed.manifest_sha)
    assert rc == 1
    assert loader_calls == []
    assert entry is not None
    assert entry.status == "failed"
    assert "CorrectionRejected" in (entry.reason or "")
    print(
        json.dumps(
            {
                "case": "reject_side_effects",
                "loader_calls": len(loader_calls),
                "raw_row_growth": 0,
                "ledger_status": entry.status,
            }
        )
    )
