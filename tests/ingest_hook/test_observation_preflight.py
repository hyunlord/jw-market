"""Observation-schema preflight: the ingest refuses to start on an unusable ledger.

Covers the B-4 gates:
  B-4-0 the COMPARATOR ITSELF is checked before its verdicts are trusted
  B-4-1 all four tables present            -> pass
  B-4-2 ingest_signal_event absent         -> stop, naming the artifact file
  B-4-3 ingest_stage_event absent          -> stop, naming the artifact file
  B-4-4 table present but columns differ    -> stop
  B-4-5 the tracked activation artifacts match the code DDL they claim to create

B-4-0 exists because the previous round shipped a schema comparator whose normalisation
was incomplete and which therefore reported drift on three tables that were in fact
identical. A comparator is only useful once it has been shown to say "different" for
things that are different and "same" for things that are the same, so that is asserted
first and directly, on known inputs.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook import job_runner, observation_preflight
from pipeline.scripts.ingest_hook.ledger import (
    OBSERVATION_TABLES,
    ddl_column_names,
)
from ingest_fixtures import write_submission

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER_SOURCE = REPO_ROOT / "pipeline/scripts/ingest_hook/ledger.py"


# -- B-4-0 verify the comparator on known inputs -------------------------------


def test_b40_column_name_extractor_ignores_key_clauses():
    ddl = """
    CREATE TABLE IF NOT EXISTS demo (
      id            BIGINT AUTO_INCREMENT PRIMARY KEY,
      epoch         VARCHAR(32)  NOT NULL,
      duration_ms   BIGINT       NULL,
      UNIQUE KEY uq_demo (epoch, id),
      KEY idx_demo (epoch)
    )
    """
    # names only, in order, with UNIQUE KEY / KEY clauses excluded
    assert ddl_column_names(ddl) == ["id", "epoch", "duration_ms"]


def test_b40_column_name_extractor_survives_mariadb_rewriting():
    """The two spellings MariaDB turns one into the other must give the same names.

    This is exactly what the previous round's comparator got wrong: it compared full
    definitions, so `AUTO_INCREMENT PRIMARY KEY` vs `NOT NULL AUTO_INCREMENT` +
    `PRIMARY KEY (id)` read as drift. Names are invariant under that rewriting.
    """
    as_written = "CREATE TABLE t (\n  id BIGINT AUTO_INCREMENT PRIMARY KEY,\n  reason TEXT NULL\n)"
    as_mariadb_reports_it = (
        "CREATE TABLE `t` (\n  `id` bigint(20) NOT NULL AUTO_INCREMENT,\n"
        "  `reason` text DEFAULT NULL,\n  PRIMARY KEY (`id`)\n)"
    )
    assert ddl_column_names(as_written) == ddl_column_names(as_mariadb_reports_it) == ["id", "reason"]


def test_b40_comparator_detects_a_real_difference(sqlite_ledger):
    """Positive control: an actually-altered table must be reported as differing."""
    baseline = sqlite_ledger.observation_schema_report()
    assert baseline["ingest_stage_event"]["verdict"] == "ok"

    sqlite_ledger._execute("ALTER TABLE ingest_stage_event ADD COLUMN bogus_extra TEXT")
    altered = sqlite_ledger.observation_schema_report()["ingest_stage_event"]
    assert altered["verdict"] == "columns_differ"
    assert altered["unexpected_columns"] == ["bogus_extra"]
    assert altered["missing_columns"] == []


# -- B-4-1..4 the gate ---------------------------------------------------------


def test_b41_all_four_tables_present_passes(sqlite_ledger):
    report = observation_preflight.verify(sqlite_ledger)
    assert sorted(report) == sorted(OBSERVATION_TABLES)
    assert {entry["verdict"] for entry in report.values()} == {"ok"}


@pytest.mark.parametrize(
    "table, artifact",
    [
        ("ingest_signal_event", "ingest-signal-event.sql"),
        ("ingest_stage_event", "ingest-stage-event.sql"),
        ("ingest_ledger", "ingest-ledger.sql"),
        ("ingest_status_transition", "ingest-status-transition.sql"),
    ],
)
def test_b42_b43_absent_table_stops_and_names_the_artifact(sqlite_ledger, table, artifact):
    sqlite_ledger._execute(f"DROP TABLE {table}")
    with pytest.raises(observation_preflight.ObservationPreflightError) as excinfo:
        observation_preflight.verify(sqlite_ledger)
    message = str(excinfo.value)
    assert table in message
    assert artifact in message, message
    assert "MISSING" in message


def test_b44_present_but_wrong_columns_stops(sqlite_ledger):
    # sqlite cannot DROP COLUMN on old versions, so rebuild the table one column short.
    sqlite_ledger._execute("DROP TABLE ingest_signal_event")
    sqlite_ledger._execute(
        "CREATE TABLE ingest_signal_event ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, epoch TEXT NOT NULL,"
        " category TEXT NOT NULL, manifest_sha TEXT NOT NULL)"
    )
    with pytest.raises(observation_preflight.ObservationPreflightError) as excinfo:
        observation_preflight.verify(sqlite_ledger)
    message = str(excinfo.value)
    assert "SCHEMA MISMATCH" in message
    assert "missing columns" in message
    assert "payload_json" in message  # a column the code writes but the table lacks


def test_b44_extra_column_is_also_a_mismatch(sqlite_ledger):
    sqlite_ledger._execute("ALTER TABLE ingest_signal_event ADD COLUMN stowaway TEXT")
    with pytest.raises(observation_preflight.ObservationPreflightError) as excinfo:
        observation_preflight.verify(sqlite_ledger)
    assert "unexpected columns" in str(excinfo.value)
    assert "stowaway" in str(excinfo.value)


# -- the gate is wired into the ingest entry point ------------------------------


def test_preflight_blocks_the_run_before_any_ledger_write(sqlite_ledger, bucket, tmp_path, capsys):
    sqlite_ledger._execute("DROP TABLE ingest_signal_event")
    manifest_path = write_submission(bucket)

    rc = job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=tmp_path / "s"
    )
    assert rc == 3  # distinct from contract (2) and load failure (1)
    combined = capsys.readouterr()
    assert "gate=observation_preflight status=fail" in combined.err
    assert "ingest-signal-event.sql" in combined.err
    # nothing was recorded: the ledger schema is what we refused to trust
    from pipeline.scripts.ingest_hook.contract import load_manifest

    sha = load_manifest(manifest_path).manifest_sha
    assert sqlite_ledger.status("2026-07", "ubist", sha) is None


def test_preflight_pass_is_announced_and_the_run_proceeds(sqlite_ledger, bucket, tmp_path, capsys):
    manifest_path = write_submission(bucket)
    rc = job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=tmp_path / "s"
    )
    assert rc == 0
    assert "gate=observation_preflight status=pass" in capsys.readouterr().out


# -- B-4-5 tracked artifacts match the code they claim to create ----------------


@pytest.mark.parametrize(
    "table, constant",
    [
        ("ingest_ledger", "_DDL_MYSQL"),
        ("ingest_stage_event", "_DDL_STAGE_MYSQL"),
        ("ingest_signal_event", "_DDL_SIGNAL_MYSQL"),
        ("ingest_status_transition", "_DDL_TRANSITION_MYSQL"),
    ],
)
def test_b45_activation_artifact_matches_the_code_ddl(table, constant):
    """Every table has a tracked artifact and it is the code DDL verbatim.

    Guards the 2026-07-22 failure mode from the other direction: a future DDL edit that
    does not update the artifact now fails here instead of drifting apart silently.
    """
    artifact = REPO_ROOT / OBSERVATION_TABLES[table]
    assert artifact.is_file(), f"{table} has no tracked activation artifact"

    source = LEDGER_SOURCE.read_text(encoding="utf-8")
    ddl = re.search(rf'{constant} = """(.*?)"""', source, re.S).group(1).strip()
    statement = "\n".join(
        line for line in artifact.read_text(encoding="utf-8").splitlines()
        if not line.startswith("--")
    ).strip().rstrip(";")
    assert statement == ddl
