from __future__ import annotations

import subprocess

import pytest

from pipeline.orchestrator.stages import STAGE_BY_KEY
from pipeline.scripts.ingest_hook import job_runner, ubist_mart_activation
from pipeline.scripts.ingest_hook.category_map import resolve_category


IDENTITY = ("2026-06", "ubist", "f" * 64)


class _Process:
    def __init__(self, waits: list[int | BaseException]):
        self._waits = iter(waits)
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        result = next(self._waits)
        if isinstance(result, BaseException):
            raise result
        return result

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def _tracker(sqlite_ledger) -> job_runner._StageTracker:
    sqlite_ledger.receive(*IDENTITY, manifest_path="_manifests/ubist/manifest.json")
    return job_runner._StageTracker(sqlite_ledger, IDENTITY, "run-recovery")


def test_locked_refresh_keeps_session_lock_alive_until_command_completes(
    monkeypatch,
) -> None:
    process = _Process(
        [
            subprocess.TimeoutExpired(("python", "-m", "refresh"), 30),
            0,
        ]
    )
    ownership_checks: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda _argv: process)
    monkeypatch.setattr(
        ubist_mart_activation,
        "require_writer_lock_owner",
        lambda conn, **_kwargs: ownership_checks.append(conn),
    )
    connection = object()

    job_runner._run_commands_with_writer_lock(
        "refresh",
        ("python", "-m", "refresh"),
        connection=connection,
        lock_name=ubist_mart_activation.WRITER_LOCK_NAME,
        heartbeat_seconds=30,
    )

    assert ownership_checks == [connection, connection, connection]
    assert process.terminated is False


def test_locked_refresh_stops_child_when_session_lock_is_lost(monkeypatch) -> None:
    process = _Process(
        [
            subprocess.TimeoutExpired(("python", "-m", "refresh"), 30),
            0,
        ]
    )
    checks = iter((None, RuntimeError("single-writer lock ownership lost")))
    monkeypatch.setattr(subprocess, "Popen", lambda _argv: process)

    def require_owner(*_args, **_kwargs) -> None:
        result = next(checks)
        if isinstance(result, BaseException):
            raise result

    monkeypatch.setattr(
        ubist_mart_activation,
        "require_writer_lock_owner",
        require_owner,
    )

    with pytest.raises(RuntimeError, match="ownership lost"):
        job_runner._run_commands_with_writer_lock(
            "refresh",
            ("python", "-m", "refresh"),
            connection=object(),
            lock_name=ubist_mart_activation.WRITER_LOCK_NAME,
            heartbeat_seconds=30,
        )

    assert process.terminated is True


def test_cleanup_failure_does_not_replace_primary_ledger_reason(
    sqlite_ledger, monkeypatch, capsys
) -> None:
    sqlite_ledger.receive(*IDENTITY, manifest_path="_manifests/ubist/manifest.json")
    primary_reason = "RuntimeError: shortlong command failed rc=3"
    sqlite_ledger.mark_failed(*IDENTITY, reason=primary_reason)
    monkeypatch.setattr(
        ubist_mart_activation,
        "release_writer_lock",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("MySQL server has gone away")
        ),
    )

    job_runner._release_writer_lock_preserving_primary(
        object(),
        lock_name=ubist_mart_activation.WRITER_LOCK_NAME,
        primary_failure_reason=primary_reason,
    )

    assert sqlite_ledger.status(*IDENTITY).reason == primary_reason
    stderr = capsys.readouterr().err
    assert primary_reason in stderr
    assert "MySQL server has gone away" in stderr


def test_recovery_refresh_failure_is_recorded_as_refresh_stage(
    sqlite_ledger, monkeypatch
) -> None:
    current_tracker = _tracker(sqlite_ledger)
    current_tracker.enter("refresh")
    current_tracker.done()
    tracker = job_runner._recovery_tracker(
        sqlite_ledger,
        IDENTITY,
        run_id="run-recovery",
        phase="failure",
    )
    monkeypatch.setattr(
        job_runner,
        "_run_commands_with_writer_lock",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("shortlong command failed rc=3")
        ),
    )

    with pytest.raises(RuntimeError, match="shortlong"):
        job_runner._run_recovery_refresh(
            tracker=tracker,
            argv=("python", "-m", "pipeline.orchestrator"),
            connection=object(),
            lock_name=ubist_mart_activation.WRITER_LOCK_NAME,
        )

    refresh_events = [
        event for event in sqlite_ledger.stage_events(*IDENTITY)
        if event.stage == "refresh"
    ]
    assert len(refresh_events) == 2
    refresh = next(event for event in refresh_events if event.status == "failed")
    assert refresh.run_id == "run-recovery:failure-recovery"
    assert refresh.status == "failed"
    assert refresh.reason == "RuntimeError: shortlong command failed rc=3"


def test_recovery_refresh_uses_general_density_route_and_completes(
    sqlite_ledger, monkeypatch
) -> None:
    tracker = _tracker(sqlite_ledger)
    argv = resolve_category("ubist").refresh_argv
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        job_runner,
        "_run_commands_with_writer_lock",
        lambda _label, command, **_kwargs: calls.append(command),
    )

    job_runner._run_recovery_refresh(
        tracker=tracker,
        argv=argv,
        connection=object(),
        lock_name=ubist_mart_activation.WRITER_LOCK_NAME,
    )

    refresh = next(
        event for event in sqlite_ledger.stage_events(*IDENTITY)
        if event.stage == "refresh"
    )
    assert refresh.status == "complete"
    assert calls == [argv]
    shortlong = STAGE_BY_KEY["shortlong"].commands(
        "incremental", (), False, "recovery-route"
    )
    assert all(
        command.argv[command.argv.index("--brand-source") + 1]
        == "general-density"
        and command.argv[command.argv.index("--bundle-kind") + 1] == "general"
        for command in shortlong
    )
