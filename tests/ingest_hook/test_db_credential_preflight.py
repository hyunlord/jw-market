from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook import db_credential_preflight, stage_log_runner


PASSWORD_ENV = {
    "DB_ROOT_PASSWORD": "same-secret",
    "MARIADB_PASSWORD": "same-secret",
    "AGENT3_DB_PASSWORD": "same-secret",
}


class _Cursor:
    def __init__(self, *, row=(1,)):
        self.row = row
        self.queries: list[str] = []

    def execute(self, query: str) -> None:
        self.queries.append(query)

    def fetchone(self):
        return self.row

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self, *, row=(1,)):
        self.cursor_value = _Cursor(row=row)
        self.closed = False

    def cursor(self) -> _Cursor:
        return self.cursor_value

    def close(self) -> None:
        self.closed = True


def test_empty_password_blocks_before_database_probe():
    env = {**PASSWORD_ENV, "AGENT3_DB_PASSWORD": ""}
    connected = False

    def connect():
        nonlocal connected
        connected = True
        return _Connection()

    with pytest.raises(
        db_credential_preflight.DBCredentialPreflightError,
        match="missing_or_empty=AGENT3_DB_PASSWORD",
    ):
        db_credential_preflight.run_preflight(environ=env, connect=connect)

    assert connected is False


def test_mismatched_passwords_are_rejected_without_exposing_values():
    env = {**PASSWORD_ENV, "AGENT3_DB_PASSWORD": "different-secret"}

    with pytest.raises(
        db_credential_preflight.DBCredentialPreflightError
    ) as caught:
        db_credential_preflight.run_preflight(
            environ=env,
            connect=lambda: _Connection(),
        )

    message = str(caught.value)
    assert "password_values=mismatch" in message
    assert "same-secret" not in message
    assert "different-secret" not in message


def test_valid_passwords_and_select_one_preserve_normal_flow():
    connection = _Connection()

    db_credential_preflight.run_preflight(
        environ=PASSWORD_ENV,
        connect=lambda: connection,
    )

    assert connection.cursor_value.queries == ["SELECT 1"]
    assert connection.closed is True


def test_select_one_failure_blocks_before_expensive_stages():
    def connect():
        raise RuntimeError("database account is locked")

    with pytest.raises(
        db_credential_preflight.DBCredentialPreflightError
    ) as caught:
        db_credential_preflight.run_preflight(
            environ=PASSWORD_ENV,
            connect=connect,
        )

    message = str(caught.value)
    assert "database_probe=failed" in message
    assert "database account is locked" not in message


def test_shortlong_runner_failure_is_named_after_mart_probe_succeeds():
    mart = _Connection()
    bundle = _Connection()

    def runner_connect():
        raise RuntimeError("access denied for runner account")

    with pytest.raises(
        db_credential_preflight.DBCredentialPreflightError
    ) as caught:
        db_credential_preflight.run_preflight(
            environ=PASSWORD_ENV,
            connect=lambda: mart,
            additional_connectors={
                "shortlong_bundle": lambda: bundle,
                "shortlong_runner": runner_connect,
            },
        )

    message = str(caught.value)
    assert "connector=shortlong_runner" in message
    assert "database_probe=failed" in message
    assert "access denied for runner account" not in message
    assert mart.closed is True
    assert bundle.closed is True


def test_all_database_connectors_are_probed_once():
    connections = {
        "mart": _Connection(),
        "shortlong_bundle": _Connection(),
        "shortlong_runner": _Connection(),
    }

    db_credential_preflight.run_preflight(
        environ=PASSWORD_ENV,
        connect=lambda: connections["mart"],
        additional_connectors={
            name: (lambda connector=connection: connector)
            for name, connection in connections.items()
            if name != "mart"
        },
    )

    for connection in connections.values():
        assert connection.cursor_value.queries == ["SELECT 1"]
        assert connection.closed is True


def test_default_shortlong_connector_inventory_matches_runtime_ports():
    connectors = db_credential_preflight._default_shortlong_connectors()

    assert tuple(connectors) == ("shortlong_bundle", "shortlong_runner")


def test_stage_runner_does_not_spawn_job_when_preflight_fails(
    tmp_path: Path, monkeypatch, capsys
):
    root = tmp_path / "logs"
    monkeypatch.setattr(stage_log_runner.config, "log_root", lambda: root)
    monkeypatch.setenv("DB_ROOT_PASSWORD", "same-secret")
    monkeypatch.setenv("MARIADB_PASSWORD", "same-secret")
    monkeypatch.delenv("AGENT3_DB_PASSWORD", raising=False)

    def forbidden_popen(*_args, **_kwargs):
        raise AssertionError("job_runner must not start after preflight failure")

    monkeypatch.setattr(stage_log_runner.subprocess, "Popen", forbidden_popen)

    rc = stage_log_runner.run(
        manifest=tmp_path / "manifest.json",
        run_id="run1",
        job_name="jw-ingest-ubist-eecd1a6a-run1",
    )

    assert rc == 2
    full_log = (
        root / "jw-ingest-ubist-eecd1a6a-run1" / "full.log"
    ).read_text(encoding="utf-8")
    assert "preflight=db_credentials status=fail" in full_log
    assert "missing_or_empty=AGENT3_DB_PASSWORD" in full_log
    assert "same-secret" not in full_log
    assert "same-secret" not in capsys.readouterr().out
