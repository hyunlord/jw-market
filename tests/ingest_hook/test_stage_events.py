"""Stage-event recording + status API extension (A-track observability).

Covers the review gates:
  V-1 happy path -> stages recorded in code order, exposed on /ingest/status
  V-2 failure injection -> failing stage recorded `failed` with reason (g3, post_gate)
  V-4 conditional skips recorded `skipped` with reason (rehearsal)
  V-5 retry accumulates per run_id (no overwrite of prior attempts)
  V-6 stage-record write failure is ignored ONLY behind REQUIRE_STAGE_LEDGER_STRICT=0
      (the former unconditional best-effort contract, kept as the escape hatch)
  V-7 /ingest/status keeps its original fields (backward compatible)
  B-4 stage recording is fail-closed by default, in both directions
  D-2 an already-failing run keeps its result line when the signal stage row is lost
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from pipeline.scripts.ingest_hook import config, job_runner
from pipeline.scripts.ingest_hook.app import IngestService, create_app
from pipeline.scripts.ingest_hook.ledger import (
    STAGE_COMPLETE,
    STAGE_FAILED,
    STAGE_SKIPPED,
    StageLedgerWriteError,
)
from ingest_fixtures import GOOD_ROWS, write_submission


@pytest.fixture
def service(sqlite_ledger, bucket, fake_transport) -> IngestService:
    return IngestService(sqlite_ledger, bucket, transport=fake_transport)


@pytest.fixture
def client(service) -> TestClient:
    return TestClient(create_app(service))


def _events(ledger, epoch, sha, category="ubist"):
    return ledger.stage_events(epoch, category, sha)


def test_v1_happy_path_records_stages_in_code_order(service, bucket, tmp_path):
    manifest_path = write_submission(bucket)
    service.ledger.receive("2026-07", "ubist", _sha(manifest_path), manifest_path=str(manifest_path))
    sha = _sha(manifest_path)

    rc = job_runner.run(manifest_path, input_root=bucket, ledger=service.ledger, rehearsal_root=tmp_path / "s")
    assert rc == 0

    events = _events(service.ledger, "2026-07", sha)
    by_stage = {e.stage: e for e in events}
    # order is the declared code order
    assert [e.stage for e in events] == [
        "g3", "load", "load_verify", "mart_build", "sigma", "post_gate",
        "mart_publish", "refresh", "signal",
    ]
    assert by_stage["g3"].status == STAGE_COMPLETE
    assert by_stage["load"].status == STAGE_COMPLETE
    assert by_stage["post_gate"].status == STAGE_COMPLETE
    # rehearsal skips these three with a reason (V-4)
    assert by_stage["load_verify"].status == STAGE_SKIPPED
    assert by_stage["mart_build"].status == STAGE_SKIPPED
    assert by_stage["sigma"].status == STAGE_SKIPPED
    assert by_stage["mart_publish"].status == STAGE_SKIPPED
    assert by_stage["refresh"].status == STAGE_SKIPPED
    assert "rehearsal" in (by_stage["refresh"].reason or "")


def test_v1_stages_exposed_on_status_api(service, client, bucket, tmp_path):
    manifest_path = write_submission(bucket)
    payload = client.post(
        "/ingest/webhook", json={"manifest_path": str(manifest_path.relative_to(bucket))}
    ).json()
    # run the Job's work inline (rehearsal) so stage rows exist, then read status
    entry = service.ledger.status(payload["epoch"], "ubist", payload["manifest_sha"])
    assert entry is not None and entry.run_id is not None
    job_runner.run(
        manifest_path,
        input_root=bucket,
        ledger=service.ledger,
        rehearsal_root=tmp_path / "s",
        run_id=entry.run_id,
    )
    status = client.get(
        "/ingest/status",
        params={"epoch": payload["epoch"], "category": "ubist", "manifest_sha": payload["manifest_sha"]},
    ).json()
    stage_names = [s["stage"] for s in status["stages"]]
    assert stage_names == [
        "job_submit", "g3", "load", "load_verify", "mart_build", "sigma", "post_gate",
        "mart_publish", "refresh", "signal",
    ]
    assert status["current_stage"] is None  # completed run has no in-flight stage
    assert len(status["signals"]) == 1
    assert status["signals"][0]["event"] == "complete"
    assert status["signals"][0]["mode"] == "staging"


def test_v2_g3_failure_records_g3_failed_with_reason(sqlite_ledger, bucket, tmp_path):
    # broken schema -> G3 fails; the g3 stage must be recorded failed with the reason.
    manifest_path = write_submission(bucket, header=("period", "level", "name", "amount"))
    sha = _sha(manifest_path)
    rc = job_runner.run(manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=tmp_path / "s")
    assert rc == 1
    events = _events(sqlite_ledger, "2026-07", sha)
    g3 = next(e for e in events if e.stage == "g3")
    assert g3.status == STAGE_FAILED
    assert "G3Error" in (g3.reason or "")
    # no later stage should be recorded (failure stopped the run at g3)
    assert [e.stage for e in events] == ["g3", "signal"]
    signals = sqlite_ledger.signal_events("2026-07", "ubist", sha)
    assert len(signals) == 1
    assert signals[0].event == "gate_failed"
    assert "G3Error" in (signals[0].payload["failure_reason"] or "")


def test_v2_gate_failure_records_post_gate_failed(sqlite_ledger, bucket, tmp_path):
    rows = [
        ("2026-07", "Class", "리바로", 10.0),
        ("2026-07", "Class", "리바로젯", 20.0),
        ("2026-07", "전체", "-", 99.0),  # whole != Σ parts -> post-gate PG-1 fails
    ]
    manifest_path = write_submission(bucket, rows=rows)
    sha = _sha(manifest_path)
    rc = job_runner.run(manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=tmp_path / "s")
    assert rc == 1
    by_stage = {e.stage: e for e in _events(sqlite_ledger, "2026-07", sha)}
    assert by_stage["g3"].status == STAGE_COMPLETE
    assert by_stage["load"].status == STAGE_COMPLETE
    assert by_stage["post_gate"].status == STAGE_FAILED
    assert "PG-1" in (by_stage["post_gate"].reason or "")
    signals = sqlite_ledger.signal_events("2026-07", "ubist", sha)
    assert len(signals) == 1
    assert signals[0].event == "gate_failed"
    assert "PG-1" in (signals[0].payload["failure_reason"] or "")


def test_v5_retry_accumulates_per_run_id(sqlite_ledger):
    # Directly exercise the recorder: two run_ids, same seq, both retained (S-3).
    identity = ("2026-07", "ubist", "a" * 64)
    sqlite_ledger.record_stage(*identity, run_id="runA", seq=1, stage="g3", status="failed", reason="boom")
    sqlite_ledger.record_stage(*identity, run_id="runB", seq=1, stage="g3", status="complete")
    events = _events(sqlite_ledger, "2026-07", "a" * 64)
    runs = {e.run_id: e.status for e in events if e.stage == "g3"}
    assert runs == {"runA": "failed", "runB": "complete"}  # prior attempt preserved, not overwritten


def test_v6_stage_record_failure_is_ignored_only_when_strict_is_disabled(
    monkeypatch, sqlite_ledger, bucket, tmp_path
):
    """The legacy S-4 best-effort contract, now reachable only behind the flag.

    Kept verbatim as the ``REQUIRE_STAGE_LEDGER_STRICT=0`` escape hatch so the old
    behavior stays covered; the default is fail-closed (see the B-4 cases below).
    """
    _skip_observation_preflight(monkeypatch)
    monkeypatch.setenv(config.ENV_REQUIRE_STAGE_LEDGER_STRICT, "0")
    # Drop the stage table so every record_stage INSERT errors; the load must still complete.
    sqlite_ledger._execute("DROP TABLE ingest_stage_event")  # test-only sabotage
    manifest_path = write_submission(bucket)
    rc = job_runner.run(manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=tmp_path / "s")
    assert rc == 0  # observation failure ignored on request; load unaffected
    sha = _sha(manifest_path)
    assert sqlite_ledger.status("2026-07", "ubist", sha).status == "complete"


# -- B-4 failure injection: stage recording is fail-closed by default ----------


def _fail_stage_writes(ledger, *, only_stage: str | None = None, exc: Exception | None = None):
    """Make ingest_stage_event WRITES raise, optionally just one stage's write.

    Patches ``_execute`` rather than ``record_stage`` so the real strict-mode policy
    inside ``record_stage`` is the thing under test.

    Scoped to INSERT/UPDATE on purpose: the scenario is "the write fails", and the
    observation preflight reads the same table's schema before the run starts. Failing
    reads too would trip that separate gate and this test would no longer exercise
    record_stage at all. The preflight's own read-failure behaviour is covered by
    test_observation_preflight.py.
    """
    original = ledger._execute
    failure = exc or sqlite3.OperationalError("no such table: ingest_stage_event")

    def patched(sql, params=()):
        is_write = sql.lstrip().upper().startswith(("INSERT", "UPDATE"))
        if is_write and "ingest_stage_event" in sql and (only_stage is None or only_stage in params):
            raise failure
        return original(sql, params)

    ledger._execute = patched


def _skip_observation_preflight(monkeypatch):
    """Isolate record_stage policy from the separate observation-preflight gate.

    These cases delete or sabotage ingest_stage_event, which the preflight is designed to
    catch first — correctly, since the ingest genuinely must not start. That refusal is
    asserted in test_observation_preflight.py. Here the subject is what record_stage does
    once a run IS underway, so the earlier gate is stood down explicitly rather than
    weakened.
    """
    monkeypatch.setattr(job_runner.observation_preflight, "verify", lambda _ledger: {})


def test_b4_1_missing_stage_table_fails_the_run_with_a_named_reason(
    monkeypatch, capsys, sqlite_ledger, bucket, tmp_path
):
    _skip_observation_preflight(monkeypatch)
    monkeypatch.delenv(config.ENV_REQUIRE_STAGE_LEDGER_STRICT, raising=False)  # default = 1
    sqlite_ledger._execute("DROP TABLE ingest_stage_event")
    manifest_path = write_submission(bucket)

    rc = job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=tmp_path / "s"
    )
    # Before the D-2 guard this escaped as a bare StageLedgerWriteError traceback and the
    # reason was lost. The run still fails, and now the named reason survives in the
    # reported result line.
    assert rc == 1
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "result=failed" in combined
    assert "StageLedgerWriteError" in combined
    assert "ingest_stage_event" in combined
    assert "_DDL_STAGE_MYSQL" in combined
    assert "result=complete " not in combined

    # The terminal ledger transition still happened, so the category is not stranded
    # in `running` — that is exactly what the status="failed" carve-out protects.
    assert sqlite_ledger.status("2026-07", "ubist", _sha(manifest_path)).status == "failed"


def test_b4_2_successful_recording_keeps_the_existing_behavior(
    monkeypatch, sqlite_ledger, bucket, tmp_path
):
    monkeypatch.delenv(config.ENV_REQUIRE_STAGE_LEDGER_STRICT, raising=False)  # default = 1
    manifest_path = write_submission(bucket)
    rc = job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=tmp_path / "s"
    )
    assert rc == 0
    sha = _sha(manifest_path)
    assert sqlite_ledger.status("2026-07", "ubist", sha).status == "complete"
    assert [e.stage for e in _events(sqlite_ledger, "2026-07", sha)] == [
        "g3", "load", "load_verify", "mart_build", "sigma", "post_gate",
        "mart_publish", "refresh", "signal",
    ]


def test_b4_3_permission_or_connection_error_also_fails_closed(
    monkeypatch, capsys, sqlite_ledger, bucket, tmp_path
):
    _skip_observation_preflight(monkeypatch)
    monkeypatch.delenv(config.ENV_REQUIRE_STAGE_LEDGER_STRICT, raising=False)  # default = 1
    _fail_stage_writes(
        sqlite_ledger,
        exc=sqlite3.OperationalError(
            "INSERT command denied to user 'jw_mart_d2_writer' for table 'ingest_stage_event'"
        ),
    )
    manifest_path = write_submission(bucket)

    rc = job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=tmp_path / "s"
    )
    assert rc == 1
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "result=failed" in combined
    assert "command denied" in combined
    assert "result=complete " not in combined
    assert sqlite_ledger.status("2026-07", "ubist", _sha(manifest_path)).status == "failed"


def test_b4_4_flag_zero_restores_the_legacy_ignored_behavior(
    monkeypatch, sqlite_ledger, bucket, tmp_path
):
    _skip_observation_preflight(monkeypatch)
    monkeypatch.setenv(config.ENV_REQUIRE_STAGE_LEDGER_STRICT, "0")
    _fail_stage_writes(sqlite_ledger)
    manifest_path = write_submission(bucket)
    rc = job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=tmp_path / "s"
    )
    assert rc == 0
    assert sqlite_ledger.status("2026-07", "ubist", _sha(manifest_path)).status == "complete"


def test_b4_5_load_committed_but_recording_lost_does_not_report_success(
    monkeypatch, capsys, sqlite_ledger, bucket, tmp_path
):
    """Only the final stage row fails, after the ledger is already complete.

    The data is committed, so the ledger must stay `complete`, but the run must not
    print result=complete or exit 0 — the step evidence is unknown.
    """
    monkeypatch.delenv(config.ENV_REQUIRE_STAGE_LEDGER_STRICT, raising=False)  # default = 1
    manifest_path = write_submission(bucket)
    _fail_stage_writes(sqlite_ledger, only_stage="signal")

    rc = job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=tmp_path / "s"
    )
    assert rc == 1
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "result=committed_with_postcommit_error" in combined
    assert "result=complete " not in combined
    sha = _sha(manifest_path)
    assert sqlite_ledger.status("2026-07", "ubist", sha).status == "complete"


def test_d2_already_failing_run_keeps_its_result_line_when_the_signal_row_is_lost(
    monkeypatch, capsys, sqlite_ledger, bucket, tmp_path
):
    """A gate failure plus a lost signal-stage row must still print result=gate_failed.

    Before the guard, the StageLedgerWriteError raised from tracker.complete("signal")
    escaped run()'s except handler and replaced the reported outcome with a traceback.
    The ledger was already terminal so nothing was lost, but the reason the run failed
    was no longer in the log.
    """
    monkeypatch.delenv(config.ENV_REQUIRE_STAGE_LEDGER_STRICT, raising=False)  # default = 1
    rows = [
        ("2026-07", "Class", "리바로", 10.0),
        ("2026-07", "Class", "리바로젯", 20.0),
        ("2026-07", "전체", "-", 99.0),  # whole != Σ parts -> post-gate PG-1 fails
    ]
    manifest_path = write_submission(bucket, rows=rows)
    _fail_stage_writes(sqlite_ledger, only_stage="signal")

    rc = job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=tmp_path / "s"
    )
    assert rc == 1
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "result=gate_failed" in combined
    assert "PG-1" in combined
    # the lost stage row is reported, not hidden
    assert "stage row for the signal step was not recorded" in combined
    assert sqlite_ledger.status("2026-07", "ubist", _sha(manifest_path)).status == "gate_failed"


def test_d2_guard_does_not_mask_the_success_path(
    monkeypatch, capsys, sqlite_ledger, bucket, tmp_path
):
    """Same injection on a SUCCEEDING run must still refuse to report success."""
    monkeypatch.delenv(config.ENV_REQUIRE_STAGE_LEDGER_STRICT, raising=False)  # default = 1
    manifest_path = write_submission(bucket)
    _fail_stage_writes(sqlite_ledger, only_stage="signal")

    rc = job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=tmp_path / "s"
    )
    assert rc == 1
    combined = capsys.readouterr()
    assert "result=committed_with_postcommit_error" in (combined.out + combined.err)
    assert "stage row for the signal step was not recorded" not in (combined.out + combined.err)


def test_b4_6_failed_stage_row_never_preempts_the_terminal_transition(
    monkeypatch, sqlite_ledger, bucket, tmp_path
):
    """status="failed" writes are reported, not raised (queue-safety carve-out)."""
    monkeypatch.delenv(config.ENV_REQUIRE_STAGE_LEDGER_STRICT, raising=False)  # default = 1
    identity = ("2026-07", "ubist", "c" * 64)
    _fail_stage_writes(sqlite_ledger)
    # No exception: a lost `failed` row must not stop the caller from reaching
    # mark_failed / mark_gate_failed.
    sqlite_ledger.record_stage(
        *identity, run_id="runF", seq=1, stage="g3", status="failed", reason="boom"
    )
    with pytest.raises(StageLedgerWriteError):
        sqlite_ledger.record_stage(
            *identity, run_id="runF", seq=1, stage="g3", status="complete"
        )


def test_v7_status_api_is_backward_compatible(client, bucket):
    manifest_path = write_submission(bucket)
    payload = client.post(
        "/ingest/webhook", json={"manifest_path": str(manifest_path.relative_to(bucket))}
    ).json()
    status = client.get(
        "/ingest/status",
        params={"epoch": payload["epoch"], "category": "ubist", "manifest_sha": payload["manifest_sha"]},
    ).json()
    # every original field is still present, unchanged in name
    for key in ("epoch", "category", "manifest_sha", "status", "reason", "job_name",
                "uploaded_by", "received_at", "finished_at"):
        assert key in status, key
    # additive keys
    assert "stages" in status and isinstance(status["stages"], list)
    assert "current_stage" in status
    assert "log_ref" in status and "durable_log_hint" in status["log_ref"]


def _sha(manifest_path) -> str:
    from pipeline.scripts.ingest_hook.contract import load_manifest
    return load_manifest(manifest_path).manifest_sha
