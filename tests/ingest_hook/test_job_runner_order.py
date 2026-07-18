from __future__ import annotations

import pytest

from pipeline.scripts.ingest_hook import job_runner
from ingest_fixtures import write_submission


def test_real_job_checks_sigma_only_after_refresh(
    sqlite_ledger, bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = write_submission(bucket)
    events: list[str] = []

    monkeypatch.setattr(
        job_runner,
        "_real_load",
        lambda *_args: events.append("load") or {
            "target_dir": "/tmp/isolated",
            "epoch_rows": 6,
            "staging_verify": False,
        },
    )
    monkeypatch.setattr(
        job_runner,
        "_run_commands",
        lambda label, _argv: events.append(label),
    )
    monkeypatch.setattr(
        job_runner,
        "_check_market_sigma",
        lambda _spec, _report: events.append("sigma"),
    )

    rc = job_runner.run(
        manifest_path,
        input_root=bucket,
        ledger=sqlite_ledger,
        rehearsal_root=None,
    )

    assert rc == 0
    assert events == ["load", "refresh", "sigma"]


def test_real_job_does_not_check_sigma_or_complete_after_refresh_failure(
    sqlite_ledger, bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = write_submission(bucket)
    events: list[str] = []

    monkeypatch.setattr(
        job_runner,
        "_real_load",
        lambda *_args: events.append("load") or {
            "target_dir": "/tmp/isolated",
            "epoch_rows": 6,
            "staging_verify": False,
        },
    )

    def run_command(label: str, _argv: tuple[str, ...]) -> None:
        events.append(label)
        if label == "refresh":
            raise RuntimeError("refresh failed")

    monkeypatch.setattr(job_runner, "_run_commands", run_command)
    monkeypatch.setattr(
        job_runner,
        "_check_market_sigma",
        lambda _spec, _report: events.append("sigma"),
    )

    rc = job_runner.run(
        manifest_path,
        input_root=bucket,
        ledger=sqlite_ledger,
        rehearsal_root=None,
    )

    assert rc == 1
    assert events == ["load", "refresh"]
    entry = next(
        row
        for row in (
            sqlite_ledger.status("2026-07", "ubist", sha)
            for sha in _all_shas(sqlite_ledger)
        )
        if row is not None
    )
    assert entry.status == "failed"


def _all_shas(ledger) -> list[str]:
    cursor = ledger._execute("SELECT manifest_sha FROM ingest_ledger")
    return [row[0] for row in cursor.fetchall()]
