"""CronJob activation preflight must fingerprint the MariaDB ledger only."""
from __future__ import annotations

import json

from pipeline.scripts.ingest_hook import ledger_fingerprint


class _Cursor:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows
        self.executed: list[str] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.executed.append(" ".join(sql.split()))

    def fetchall(self) -> list[dict[str, str]]:
        return self.rows


class _Connection:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.cursor_instance = _Cursor(rows)
        self.rollback_calls = 0
        self.close_calls = 0

    def cursor(self) -> _Cursor:
        return self.cursor_instance

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


def _environment() -> dict[str, str]:
    return {
        "MARIADB_HOST": "llmops-mariadb-service.llmops.svc.cluster.local",
        "MARIADB_PORT": "3306",
        "MARIADB_DATABASE": "jw_mart_d2_stage_20260630_r2",
        "MARIADB_USER": "llmops",
        "MARIADB_PASSWORD": "must-not-appear",
    }


def _rows() -> list[dict[str, str]]:
    return [
        {
            "epoch": "2026-02",
            "category": "ubist",
            "manifest_sha": "a" * 64,
            "status": "complete",
        },
        {
            "epoch": "2026-03",
            "category": "ubist",
            "manifest_sha": "b" * 64,
            "status": "failed",
        },
    ]


def test_report_only_reads_mariadb_in_a_read_only_transaction(capsys) -> None:
    connection = _Connection(_rows())
    captured_connect: dict[str, str | int | bool] = {}

    def connect(**kwargs):
        captured_connect.update(kwargs)
        return connection

    rc = ledger_fingerprint.main(
        ["--report-only"],
        environ=_environment(),
        connect=connect,
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["activation_allowed"] is False
    assert payload["storage"]["engine"] == "mariadb"
    assert payload["storage"]["host"] == _environment()["MARIADB_HOST"]
    assert payload["status_counts"] == {"complete": 1, "failed": 1}
    assert payload["total"] == 2
    assert "must-not-appear" not in json.dumps(payload)
    assert captured_connect["autocommit"] is False
    assert connection.cursor_instance.executed[0] == "SET TRANSACTION READ ONLY"
    assert connection.rollback_calls == 1
    assert connection.close_calls == 1


def test_gate_allows_only_matching_target_and_fingerprint(capsys) -> None:
    report_connection = _Connection(_rows())
    report = ledger_fingerprint.collect_fingerprint(
        ledger_fingerprint.target_from_env(_environment()),
        connect=lambda **_kwargs: report_connection,
    )

    rc = ledger_fingerprint.main(
        [
            "--expected-host",
            _environment()["MARIADB_HOST"],
            "--expected-database",
            _environment()["MARIADB_DATABASE"],
            "--expected-fingerprint",
            report.identity_fingerprint,
        ],
        environ=_environment(),
        connect=lambda **_kwargs: _Connection(_rows()),
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["activation_allowed"] is True
    assert payload["identity_fingerprint"] == report.identity_fingerprint


def test_gate_fails_closed_on_fingerprint_mismatch(capsys) -> None:
    rc = ledger_fingerprint.main(
        [
            "--expected-host",
            _environment()["MARIADB_HOST"],
            "--expected-database",
            _environment()["MARIADB_DATABASE"],
            "--expected-fingerprint",
            "0" * 64,
        ],
        environ=_environment(),
        connect=lambda **_kwargs: _Connection(_rows()),
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc != 0
    assert payload["activation_allowed"] is False
    assert "fingerprint mismatch" in payload["reason"]


def test_gate_rejects_any_sqlite_or_shadow_ledger_configuration(capsys) -> None:
    environment = _environment()
    environment["INGEST_SHADOW_LEDGER_SQLITE"] = "/market-output/shadow/ledger.db"

    rc = ledger_fingerprint.main(
        ["--report-only"],
        environ=environment,
        connect=lambda **_kwargs: _Connection(_rows()),
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc != 0
    assert payload["activation_allowed"] is False
    assert "SQLite" in payload["reason"]


def test_gate_reports_connection_failure_without_exposing_credentials(capsys) -> None:
    def fail_connect(**_kwargs):
        raise RuntimeError("must-not-appear")

    rc = ledger_fingerprint.main(
        ["--report-only"],
        environ=_environment(),
        connect=fail_connect,
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert rc != 0
    assert payload["activation_allowed"] is False
    assert payload["reason"] == "MariaDB fingerprint query failed: RuntimeError"
    assert "must-not-appear" not in output
