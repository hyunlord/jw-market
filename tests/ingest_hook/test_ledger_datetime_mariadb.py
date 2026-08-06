"""Opt-in MariaDB contract tests for ingest ledger datetime values."""
from __future__ import annotations

import os
from datetime import datetime

import pytest

from pipeline.scripts.ingest_hook.ledger import Ledger


TARGET_DB = os.environ.get("INGEST_LEDGER_INTEGRATION_DB", "")
pytestmark = pytest.mark.skipif(
    not TARGET_DB.startswith("jw_ingest_"),
    reason="requires disposable INGEST_LEDGER_INTEGRATION_DB=jw_ingest_*",
)

IDENTITY = ("2026-06", "ubist", "d" * 64)


def _connect(database: str | None = None):
    import pymysql

    return pymysql.connect(
        host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT", "3306")),
        user=os.environ.get("MARIADB_USER", "root"),
        password=os.environ.get("MARIADB_PASSWORD", ""),
        database=database,
        charset="utf8mb4",
        autocommit=False,
    )


@pytest.fixture()
def mysql_ledger():
    admin = _connect()
    with admin.cursor() as cursor:
        cursor.execute(f"DROP SCHEMA IF EXISTS `{TARGET_DB}`")
        cursor.execute(
            f"CREATE SCHEMA `{TARGET_DB}` DEFAULT CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_unicode_ci"
        )
    admin.commit()
    admin.close()

    connection = _connect(TARGET_DB)
    ledger = Ledger(connection, dialect="mysql")
    ledger.ensure_table()
    try:
        yield ledger, connection
    finally:
        connection.close()
        admin = _connect()
        with admin.cursor() as cursor:
            cursor.execute(f"DROP SCHEMA IF EXISTS `{TARGET_DB}`")
        admin.commit()
        admin.close()


def test_candidate_and_approval_datetimes_are_stored_as_naive_utc(mysql_ledger) -> None:
    ledger, connection = mysql_ledger
    ledger.receive(*IDENTITY, manifest_path="_manifests/ubist/2026-06/manifest.json")
    ledger.mark_running(*IDENTITY, job_name="build-job", run_id="build-run")

    ledger.mark_awaiting_approval(
        *IDENTITY,
        run_id="build-run",
        candidate={"run_id": "build-run"},
        prepared_at="2026-08-06T11:00:15.663977+09:00",
        expires_at="2026-08-07T11:00:15.999999+09:00",
    )
    assert ledger.mark_publish_running(
        *IDENTITY,
        build_run_id="build-run",
        publish_job_name="publish-job",
        approved_by="pl",
        approved_at="2026-08-06T11:05:16.123456+09:00",
    )
    ledger.mark_complete(*IDENTITY, row_counts={"ubist": 2_043_451})
    ledger.record_signal(
        *IDENTITY,
        run_id="build-run",
        event="complete",
        mode="incremental",
        rows_loaded=2_043_451,
        delivery_status="delivered",
        attempts=1,
        reason=None,
        payload={"status": "complete"},
    )

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT prepared_at, expires_at, approved_at FROM ingest_publish_candidate"
        )
        prepared_at, expires_at, approved_at = cursor.fetchone()
        cursor.execute("SELECT received_at, started_at, finished_at FROM ingest_ledger")
        received_at, started_at, finished_at = cursor.fetchone()
        cursor.execute("SELECT created_at FROM ingest_status_transition ORDER BY id")
        transition_created_at = [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT created_at FROM ingest_signal_event")
        signal_created_at = cursor.fetchone()[0]

    assert prepared_at == datetime(2026, 8, 6, 2, 0, 15)
    assert expires_at == datetime(2026, 8, 7, 2, 0, 15)
    assert approved_at == datetime(2026, 8, 6, 2, 5, 16)
    assert started_at == approved_at
    assert isinstance(received_at, datetime)
    assert isinstance(finished_at, datetime)
    assert transition_created_at
    assert all(isinstance(value, datetime) for value in transition_created_at)
    assert isinstance(signal_created_at, datetime)
    assert all(
        value.microsecond == 0
        for value in (
            received_at,
            started_at,
            finished_at,
            *transition_created_at,
            signal_created_at,
        )
    )


def test_stage_datetimes_are_stored_as_naive_utc(mysql_ledger) -> None:
    ledger, connection = mysql_ledger

    ledger.record_stage(
        *IDENTITY,
        run_id="build-run",
        seq=1,
        stage="g3",
        status="complete",
        started_at="2026-08-06T11:00:15.663977+09:00",
        finished_at="2026-08-06T11:01:16.999999+09:00",
        duration_ms=61_336,
    )

    with connection.cursor() as cursor:
        cursor.execute("SELECT started_at, finished_at FROM ingest_stage_event")
        row = cursor.fetchone()

    assert row == (
        datetime(2026, 8, 6, 2, 0, 15),
        datetime(2026, 8, 6, 2, 1, 16),
    )
