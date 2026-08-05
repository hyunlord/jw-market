from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from pipeline.scripts.ingest_hook import rearm_runner, ubist_mart_activation


IDENTITY = ("2026-05", "ubist", "a" * 64)


def _prepared_failed(
    sqlite_ledger, tmp_path: Path, *, expires_at: str = "2099-01-01T00:00:00Z",
    include_integrity: bool = True,
):
    run_id = "20260805014118770360"
    live = tmp_path / "ubist"
    failed = tmp_path / f".ubist_failed_{run_id}"
    candidate = tmp_path / f".ubist_candidate_{run_id}"
    backup = tmp_path / f".ubist_backup_{run_id}"
    journal = tmp_path / f".ubist_activation_{run_id}.json"
    live.mkdir()
    failed.mkdir()
    (failed / "part.parquet").write_bytes(b"payload")
    (failed / "_manifest.json").write_text('{"ok":true}', encoding="utf-8")
    journal.write_text(json.dumps({
        "version": 2, "run_id": run_id, "phase": "recovered",
        "epoch": IDENTITY[0], "category": IDENTITY[1], "manifest_sha": IDENTITY[2],
        "source_db": "source", "target_db": "jw_mart_ingest_shadow_target",
        "build_db": "jw_mart_ingest_shadow_build", "live_root": str(live),
        "candidate_root": str(candidate), "backup_root": str(backup),
        "tables": list(ubist_mart_activation.NUMERIC_TABLES),
    }), encoding="utf-8")
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    sqlite_ledger.mark_running(*IDENTITY, job_name="build", run_id=run_id)
    inventory = ubist_mart_activation.inventory_corpus(failed)
    candidate_payload = {
        "activation_journal": str(journal), "candidate_root": str(candidate),
        "build_db": "jw_mart_ingest_shadow_build", "target_db": "jw_mart_ingest_shadow_target",
    }
    if include_integrity:
        candidate_payload.update({
            "candidate_integrity": {"file_count": inventory.file_count,
                                    "total_bytes": inventory.total_bytes,
                                    "manifest_sha": inventory.manifest_sha},
            "build_table_integrity": [
                {"table": table, "row_count": 1, "crc_sum": 2, "crc_xor": 3}
                for table in ubist_mart_activation.NUMERIC_TABLES
            ],
        })
    sqlite_ledger.mark_awaiting_approval(
        *IDENTITY, run_id=run_id,
        candidate=candidate_payload,
        prepared_at="2026-08-05T00:00:00Z", expires_at=expires_at,
    )
    sqlite_ledger.mark_publish_running(
        *IDENTITY, build_run_id=run_id, publish_job_name="publish-job",
        approved_by="pl", approved_at="2026-08-05T00:01:00Z",
    )
    sqlite_ledger.mark_failed(*IDENTITY, reason="1105")
    return run_id, failed, candidate, journal, inventory


def _call(sqlite_ledger, run_id, inventory, *, actor="operator", **overrides):
    args = dict(
        ledger=sqlite_ledger, epoch=IDENTITY[0], category=IDENTITY[1],
        manifest_sha=IDENTITY[2], build_run_id=run_id, actor=actor,
        expected_file_count=inventory.file_count, expected_total_bytes=inventory.total_bytes,
        expected_manifest_sha=inventory.manifest_sha,
        now=datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc),
        read_build_fingerprints=lambda _name: tuple(
            ubist_mart_activation.BuildTableFingerprint(table, 1, 2, 3)
            for table in ubist_mart_activation.NUMERIC_TABLES
        ),
    )
    args.update(overrides)
    return rearm_runner.rearm(**args)


def _make_legacy_candidate(sqlite_ledger, tmp_path: Path):
    run_id, failed, candidate, journal, _inventory = _prepared_failed(
        sqlite_ledger, tmp_path, include_integrity=False
    )
    parquet = failed / "year=2026" / "month=05" / "data.parquet"
    parquet.parent.mkdir(parents=True)
    (failed / "part.parquet").unlink()
    pq.write_table(pa.table({"value": [1, 2]}), parquet)
    (failed / "_manifest.json").write_text(json.dumps({
        "partitions": [{
            "period_yyyymm": "2026-05",
            "path": "year=2026/month=05/data.parquet",
            "row_count": 2,
        }],
    }), encoding="utf-8")
    (failed / "post_gate_report.json").write_text(json.dumps({
        "status": "pass", "epoch": IDENTITY[0], "category": IDENTITY[1],
        "run_id": run_id,
    }), encoding="utf-8")
    return run_id, failed, candidate, journal, ubist_mart_activation.inventory_corpus(failed)


def test_rearm_exact_identity_restores_candidate_and_records_audit(sqlite_ledger, tmp_path):
    run_id, failed, candidate, journal, inventory = _prepared_failed(sqlite_ledger, tmp_path)
    result = _call(sqlite_ledger, run_id, inventory)
    assert result.status == "awaiting_approval"
    assert candidate.is_dir() and not failed.exists()
    assert json.loads(journal.read_text())["phase"] == "awaiting_approval"
    transition = sqlite_ledger.status_transitions(*IDENTITY)[-1]
    assert (transition.previous_status, transition.status) == ("failed", "awaiting_approval")
    assert (transition.actor, transition.source) == ("operator", "audited_publish_rearm")
    assert transition.evidence["build_run_id"] == run_id
    assert sqlite_ledger.prepared_candidate(*IDENTITY).publish_job_name is None


@pytest.mark.parametrize("field,value", [
    ("manifest_sha", "b" * 64), ("build_run_id", "other-run"),
])
def test_rearm_rejects_identity_mismatch_without_mutation(sqlite_ledger, tmp_path, field, value):
    run_id, failed, candidate, journal, inventory = _prepared_failed(sqlite_ledger, tmp_path)
    with pytest.raises(rearm_runner.RearmRejected, match="identity"):
        _call(sqlite_ledger, run_id, inventory, **{field: value})
    assert failed.is_dir() and not candidate.exists()
    assert json.loads(journal.read_text())["phase"] == "recovered"


def test_rearm_rejects_expired_candidate_without_mutation(sqlite_ledger, tmp_path):
    run_id, failed, candidate, journal, inventory = _prepared_failed(
        sqlite_ledger, tmp_path, expires_at="2026-08-05T00:30:00Z"
    )
    with pytest.raises(rearm_runner.RearmRejected, match="expired"):
        _call(sqlite_ledger, run_id, inventory)
    assert failed.is_dir() and not candidate.exists()
    assert sqlite_ledger.status(*IDENTITY).status == "failed"


def test_rearm_rejects_corpus_integrity_mismatch(sqlite_ledger, tmp_path):
    run_id, failed, candidate, _journal, inventory = _prepared_failed(sqlite_ledger, tmp_path)
    (failed / "part.parquet").write_bytes(b"tampered")
    with pytest.raises(rearm_runner.RearmRejected, match="integrity"):
        _call(sqlite_ledger, run_id, inventory)
    assert failed.is_dir() and not candidate.exists()


def test_rearm_rejects_completed_run(sqlite_ledger, tmp_path):
    run_id, failed, candidate, _journal, inventory = _prepared_failed(sqlite_ledger, tmp_path)
    sqlite_ledger.mark_complete(*IDENTITY, row_counts={})
    with pytest.raises(rearm_runner.RearmRejected, match="failed"):
        _call(sqlite_ledger, run_id, inventory)
    assert failed.is_dir() and not candidate.exists()


def test_rearm_rejects_changed_build_schema(sqlite_ledger, tmp_path):
    run_id, failed, candidate, _journal, inventory = _prepared_failed(sqlite_ledger, tmp_path)
    changed = tuple(
        ubist_mart_activation.BuildTableFingerprint(table, 99, 2, 3)
        for table in ubist_mart_activation.NUMERIC_TABLES
    )
    with pytest.raises(rearm_runner.RearmRejected, match="build-table integrity"):
        _call(sqlite_ledger, run_id, inventory, read_build_fingerprints=lambda _name: changed)
    assert failed.is_dir() and not candidate.exists()


def test_rearm_reconstructs_legacy_integrity_inside_audited_transition(sqlite_ledger, tmp_path):
    run_id, failed, candidate, _journal, inventory = _make_legacy_candidate(
        sqlite_ledger, tmp_path
    )
    result = _call(
        sqlite_ledger, run_id, inventory,
        allow_legacy_integrity_reconstruction=True,
    )
    assert result.status == "awaiting_approval"
    assert candidate.is_dir() and not failed.exists()
    payload = sqlite_ledger.prepared_candidate(*IDENTITY).payload
    assert payload["candidate_integrity"] == {
        "file_count": inventory.file_count,
        "total_bytes": inventory.total_bytes,
        "manifest_sha": inventory.manifest_sha,
    }
    assert len(payload["build_table_integrity"]) == 6
    transition = sqlite_ledger.status_transitions(*IDENTITY)[-1]
    assert transition.evidence["integrity_origin"] == "legacy_manifest_reconstruction"
    assert transition.evidence["legacy_manifest"]["partition_count"] == 1


def test_rearm_rejects_legacy_candidate_without_explicit_flag(sqlite_ledger, tmp_path):
    run_id, failed, candidate, _journal, inventory = _make_legacy_candidate(
        sqlite_ledger, tmp_path
    )
    with pytest.raises(rearm_runner.RearmRejected, match="legacy integrity"):
        _call(sqlite_ledger, run_id, inventory)
    assert failed.is_dir() and not candidate.exists()


def test_rearm_rejects_legacy_manifest_row_mismatch(sqlite_ledger, tmp_path):
    run_id, failed, candidate, _journal, inventory = _make_legacy_candidate(
        sqlite_ledger, tmp_path
    )
    manifest = json.loads((failed / "_manifest.json").read_text())
    manifest["partitions"][0]["row_count"] = 3
    (failed / "_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    inventory = ubist_mart_activation.inventory_corpus(failed)
    with pytest.raises(rearm_runner.RearmRejected, match="row count"):
        _call(
            sqlite_ledger, run_id, inventory,
            allow_legacy_integrity_reconstruction=True,
        )
    assert failed.is_dir() and not candidate.exists()


def test_rearm_rejects_symlinked_corpus_file(sqlite_ledger, tmp_path):
    run_id, failed, candidate, _journal, inventory = _prepared_failed(sqlite_ledger, tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (failed / "link").symlink_to(outside)
    with pytest.raises(rearm_runner.RearmRejected, match="symlink"):
        _call(sqlite_ledger, run_id, inventory)
    assert failed.is_dir() and not candidate.exists()


def test_rearm_resumes_after_crash_following_candidate_rename(sqlite_ledger, tmp_path):
    run_id, failed, candidate, journal, inventory = _prepared_failed(sqlite_ledger, tmp_path)
    failed.rename(candidate)
    rearm_runner.update_activation_journal(journal, "rearm_started")
    result = _call(sqlite_ledger, run_id, inventory)
    assert result.status == "awaiting_approval"
    assert candidate.is_dir() and not failed.exists()
    assert json.loads(journal.read_text())["phase"] == "awaiting_approval"


def test_rearm_compensates_files_when_ledger_transition_fails(sqlite_ledger, tmp_path, monkeypatch):
    run_id, failed, candidate, journal, inventory = _prepared_failed(sqlite_ledger, tmp_path)
    monkeypatch.setattr(sqlite_ledger, "rearm_failed_candidate", lambda *a, **k: False)
    with pytest.raises(rearm_runner.RearmRejected, match="changed"):
        _call(sqlite_ledger, run_id, inventory)
    assert failed.is_dir() and not candidate.exists()
    assert json.loads(journal.read_text())["phase"] == "recovered"


def test_recovery_finishes_journal_after_ledger_rearm_commit(sqlite_ledger, tmp_path, monkeypatch):
    run_id, failed, candidate, journal, inventory = _prepared_failed(sqlite_ledger, tmp_path)
    real_update = rearm_runner.update_activation_journal

    def fail_final_journal(path, phase):
        if phase == "awaiting_approval":
            raise OSError("injected final journal failure")
        real_update(path, phase)

    monkeypatch.setattr(rearm_runner, "update_activation_journal", fail_final_journal)
    with pytest.raises(OSError, match="final journal"):
        _call(sqlite_ledger, run_id, inventory)
    assert sqlite_ledger.status(*IDENTITY).status == "awaiting_approval"
    assert candidate.is_dir() and not failed.exists()
    assert json.loads(journal.read_text())["phase"] == "rearm_started"

    monkeypatch.setattr(rearm_runner, "update_activation_journal", real_update)
    ubist_mart_activation.recover_incomplete_activations(
        object(), output_root=tmp_path,
        ledger_status=lambda *_identity: sqlite_ledger.status(*IDENTITY).status,
    )
    assert json.loads(journal.read_text())["phase"] == "awaiting_approval"
