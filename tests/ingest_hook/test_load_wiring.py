"""J5 — upload -> loader wiring and the M-2 silent-failure gate.

The loader subprocess (pipeline.etl.run) needs pyarrow/openpyxl and is exercised
for real in the cluster gate; here _run_commands is stubbed so the wiring
(argv building, target routing, fail-closed) and the M-2 gate (verify_epoch_loaded)
are tested deterministically with zero external deps.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook import config, job_runner
from pipeline.scripts.ingest_hook.category_map import resolve_category
from pipeline.scripts.ingest_hook.contract import load_manifest
from pipeline.scripts.ingest_hook.load_verify import LoadVerifyError, verify_epoch_loaded
from ingest_fixtures import write_submission

UBIST = resolve_category("ubist")


def _write_load_manifest(target_dir: Path, epoch: str, rows: int) -> None:
    """Simulate what the real UBIST loader writes to its target dir."""
    year, month = epoch.split("-")
    part = target_dir / f"year={year}" / f"month={month}" / "data.parquet"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"PAR1")  # placeholder parquet bytes; M-2 only checks existence
    (target_dir / "_manifest.json").write_text(
        json.dumps({"schema_version": "1.0", "partitions": [
            {"period_yyyymm": epoch, "path": f"year={year}/month={month}/data.parquet", "row_count": rows}
        ]}),
        encoding="utf-8",
    )


# ─── M-2 gate unit tests ─────────────────────────────────────────────
def test_verify_passes_when_epoch_present(tmp_path):
    _write_load_manifest(tmp_path, "2026-03", 42)
    assert verify_epoch_loaded("ubist_parquet_manifest", tmp_path, "2026-03") == 42


def test_verify_fails_when_no_manifest(tmp_path):
    with pytest.raises(LoadVerifyError, match="no manifest"):
        verify_epoch_loaded("ubist_parquet_manifest", tmp_path, "2026-03")


def test_verify_fails_when_epoch_absent(tmp_path):
    _write_load_manifest(tmp_path, "2026-02", 10)
    with pytest.raises(LoadVerifyError, match="absent from load output"):
        verify_epoch_loaded("ubist_parquet_manifest", tmp_path, "2026-03")


def test_verify_fails_on_zero_rows(tmp_path):
    (tmp_path).mkdir(exist_ok=True)
    (tmp_path / "_manifest.json").write_text(
        json.dumps({"partitions": [{"period_yyyymm": "2026-03", "row_count": 0}]}), encoding="utf-8"
    )
    with pytest.raises(LoadVerifyError, match="<= 0"):
        verify_epoch_loaded("ubist_parquet_manifest", tmp_path, "2026-03")


def test_verify_fails_when_parquet_missing(tmp_path):
    (tmp_path / "_manifest.json").write_text(
        json.dumps({"partitions": [{"period_yyyymm": "2026-03", "path": "year=2026/month=03/data.parquet", "row_count": 5}]}),
        encoding="utf-8",
    )
    with pytest.raises(LoadVerifyError, match="parquet is missing"):
        verify_epoch_loaded("ubist_parquet_manifest", tmp_path, "2026-03")


# ─── _real_load wiring tests (loader stubbed) ────────────────────────
@pytest.fixture
def staging_env(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_LOAD_STAGING_ROOT, str(tmp_path / "staging"))
    monkeypatch.delenv(config.ENV_LOAD_TARGET_ROOT, raising=False)
    return tmp_path


def _manifest(bucket, **kw):
    return load_manifest(write_submission(bucket, **kw))


def test_real_load_injects_file_and_target(staging_env, bucket, monkeypatch):
    manifest = _manifest(bucket, epoch="2026-03")
    seen = {}

    def fake_run(label, argv):
        seen["argv"] = argv
        # simulate the loader honoring --target-dir
        target = Path(argv[argv.index("--target-dir") + 1])
        _write_load_manifest(target, "2026-03", 7)

    monkeypatch.setattr(job_runner, "_run_commands", fake_run)
    result = job_runner._real_load(manifest, UBIST, bucket)

    assert "--file" in seen["argv"] and "--target-dir" in seen["argv"]
    assert seen["argv"][seen["argv"].index("--file") + 1].endswith("data.csv")
    assert result["epoch_rows"] == 7
    assert result["staging_verify"] is True


def test_real_load_silent_failure_is_caught(staging_env, bucket, monkeypatch):
    manifest = _manifest(bucket, epoch="2026-03")
    # loader runs but writes nothing to the target (the exact silent-failure shape)
    monkeypatch.setattr(job_runner, "_run_commands", lambda label, argv: None)
    with pytest.raises(LoadVerifyError, match="no manifest|absent"):
        job_runner._real_load(manifest, UBIST, bucket)


def test_real_load_unwired_category_fails_closed(staging_env, bucket, monkeypatch):
    manifest = _manifest(bucket, category="iqvia", epoch="2026-Q1",
                         rows=[("2026-Q1", "Class", "x", 1.0), ("2026-Q1", "전체", "-", 1.0)])
    monkeypatch.setattr(job_runner, "_run_commands", lambda label, argv: None)
    iqvia = resolve_category("iqvia")
    with pytest.raises(RuntimeError, match="no upload wiring"):
        job_runner._real_load(manifest, iqvia, bucket)


def test_real_load_skeleton_no_op(staging_env, bucket):
    manifest = _manifest(bucket, category="skeleton", epoch="2026-03")
    result = job_runner._real_load(manifest, resolve_category("skeleton"), bucket)
    assert result["target_dir"] is None


def test_load_output_root_fail_closed_without_env(monkeypatch):
    monkeypatch.delenv(config.ENV_LOAD_STAGING_ROOT, raising=False)
    monkeypatch.delenv(config.ENV_LOAD_TARGET_ROOT, raising=False)
    with pytest.raises(RuntimeError, match="no output root"):
        config.load_output_root()


# ─── full run() in staging-verify mode (loader stubbed) ──────────────
def test_run_real_staging_verify_completes(staging_env, bucket, sqlite_ledger, monkeypatch):
    manifest_path = write_submission(bucket)  # default epoch 2026-07 matches GOOD_ROWS periods
    manifest = load_manifest(manifest_path)

    def fake_run(label, argv):
        if label == "load":
            target = Path(argv[argv.index("--target-dir") + 1])
            _write_load_manifest(target, "2026-07", 9)
        # refresh must NOT be called in staging-verify
        elif label == "refresh":
            raise AssertionError("refresh ran in staging-verify mode")

    monkeypatch.setattr(job_runner, "_run_commands", fake_run)
    rc = job_runner.run(manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=None)
    assert rc == 0
    entry = sqlite_ledger.status(manifest.epoch, "ubist", manifest.manifest_sha)
    assert entry.status == "complete"
    assert entry.row_counts.get("epoch:2026-07") == 9


def test_run_real_silent_failure_marks_failed(staging_env, bucket, sqlite_ledger, monkeypatch):
    manifest_path = write_submission(bucket)
    manifest = load_manifest(manifest_path)
    monkeypatch.setattr(job_runner, "_run_commands", lambda label, argv: None)  # loads nothing
    rc = job_runner.run(manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=None)
    assert rc == 1
    entry = sqlite_ledger.status(manifest.epoch, "ubist", manifest.manifest_sha)
    assert entry.status == "failed"
    assert "LoadVerifyError" in entry.reason
