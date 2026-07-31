from __future__ import annotations

import re
import sqlite3
from typing import Any

import pytest

from pipeline.scripts.rollback.ledger import PromotionLedger
from pipeline.scripts.rollback.models import TableBackup
from pipeline.scripts.rollback.recording import (
    PromotionIdentity,
    record_mysql_component,
)

_QUALIFIED_TABLE_RE = re.compile(r"`([^`]+)`\.`([^`]+)`")


class _FakeMySQLCursor:
    def __init__(self, conn: _FakeMySQLConnection) -> None:
        self._conn = conn
        self._one: tuple[object, ...] | None = None

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self._conn.executed.append((sql, params))
        match = _QUALIFIED_TABLE_RE.search(sql)
        if match is None:
            raise AssertionError(f"unqualified promotion ledger SQL: {sql}")
        schema, table = match.groups()
        normalized = " ".join(sql.split())

        if normalized.startswith("CREATE TABLE"):
            self._conn.tables.setdefault(schema, set()).add(table)
            return
        if normalized.startswith(
            "SELECT epoch, ingest_run_id, serving_db, generation_db FROM"
        ):
            row = self._conn.generations.get((schema, str(params[0])))
            self._one = row[1:5] if row is not None else None
            return
        if normalized.startswith("INSERT INTO") and table == "promotion_generation":
            self._conn.generations[(schema, str(params[0]))] = tuple(params)
            return
        if normalized.startswith("SELECT promotion_run_id") and table == "promotion_generation":
            self._one = self._conn.generations.get((schema, str(params[0])))
            return
        raise AssertionError(f"unsupported fake MySQL statement: {sql}")

    def fetchone(self) -> tuple[object, ...] | None:
        return self._one


class _FakeMySQLConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.tables: dict[str, set[str]] = {}
        self.generations: dict[tuple[str, str], tuple[object, ...]] = {}
        self.commits = 0

    def cursor(self) -> _FakeMySQLCursor:
        return _FakeMySQLCursor(self)

    def commit(self) -> None:
        self.commits += 1


class _QualificationOnlyCursor:
    def __init__(self, conn: _QualificationOnlyConnection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        if _QUALIFIED_TABLE_RE.search(sql) is None:
            raise AssertionError(f"unqualified promotion ledger SQL: {sql}")
        self._conn.executed.append((sql, params))

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class _QualificationOnlyConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def cursor(self) -> _QualificationOnlyCursor:
        return _QualificationOnlyCursor(self)

    def commit(self) -> None:
        pass


@pytest.mark.parametrize("schema_db", [None, "", "mart-name", "mart.name", "mart`name"])
def test_mysql_ledger_rejects_missing_or_unsafe_schema(schema_db: str | None) -> None:
    conn = _FakeMySQLConnection()

    with pytest.raises(ValueError, match="schema_db"):
        PromotionLedger(conn, dialect="mysql", schema_db=schema_db)

    assert conn.executed == []


def test_mysql_ledger_creates_only_contract_tables_in_designated_schema() -> None:
    conn = _FakeMySQLConnection()
    ledger = PromotionLedger(conn, dialect="mysql", schema_db="serving_blue")

    ledger.ensure_tables()

    assert conn.tables == {
        "serving_blue": {
            "promotion_generation",
            "promotion_component",
            "promotion_rollback_event",
            "promotion_fdm_rollback_state",
            "promotion_fdm_activation_journal",
        }
    }
    assert len(conn.executed) == 5
    assert all("`serving_blue`." in sql for sql, _ in conn.executed)


def test_mysql_ledgers_keep_generation_records_isolated_by_schema() -> None:
    conn = _FakeMySQLConnection()
    blue = PromotionLedger(conn, dialect="mysql", schema_db="serving_blue")
    green = PromotionLedger(conn, dialect="mysql", schema_db="serving_green")
    blue.ensure_tables()
    green.ensure_tables()

    blue.record_generation("run-blue", "epoch-blue", "ingest-blue", "serving_blue", "gen_blue")
    green.record_generation(
        "run-green", "epoch-green", "ingest-green", "serving_green", "gen_green"
    )

    assert blue.generation("run-blue") is not None
    assert blue.generation("run-green") is None
    assert green.generation("run-green") is not None
    assert green.generation("run-blue") is None
    assert set(conn.generations) == {
        ("serving_blue", "run-blue"),
        ("serving_green", "run-green"),
    }


def test_all_mysql_ledger_ddl_and_dml_are_schema_qualified() -> None:
    conn = _QualificationOnlyConnection()
    ledger = PromotionLedger(conn, dialect="mysql", schema_db="serving_all_paths")

    ledger.ensure_tables()
    ledger.record_component(
        promotion_run_id="run-all",
        component="general",
        epoch="epoch-all",
        ingest_run_id="ingest-all",
        target_db="serving_all_paths",
        generation_db="generation-all",
        tables=(TableBackup("live", "backup", 1, "1:1:1"),),
    )
    assert ledger.generation("latest-good") is None
    assert ledger.generation("run-all") is None
    assert ledger.generation_for_epoch("epoch-all") is None
    assert ledger.generations() == ()
    assert ledger.components("run-all") == {}
    ledger.record_rollback("run-all", actor="test", reason="qualification")
    assert ledger.rollback_events("run-all") == ()

    assert len(conn.executed) == 18
    assert all("`serving_all_paths`." in sql for sql, _ in conn.executed)


def test_sqlite_ledger_keeps_existing_unqualified_round_trip() -> None:
    conn = sqlite3.connect(":memory:")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    ledger = PromotionLedger(conn, dialect="sqlite")

    ledger.ensure_tables()
    ledger.record_generation(
        "run-sqlite",
        "epoch-sqlite",
        "ingest-sqlite",
        "serving_sqlite",
        "generation_sqlite",
    )

    generation = ledger.generation("run-sqlite")
    assert generation is not None
    assert generation.promotion_run_id == "run-sqlite"
    assert all("`" not in statement for statement in statements)


def test_mysql_recording_uses_serving_db_for_ledger_schema(monkeypatch: Any) -> None:
    from pipeline.scripts.rollback import mysql_ops, recording

    captured: dict[str, object] = {}

    class _Ledger:
        def __init__(self, _conn: object, *, dialect: str, schema_db: str) -> None:
            captured["dialect"] = dialect
            captured["schema_db"] = schema_db

        def ensure_tables(self) -> None:
            captured["ensured"] = True

        def record_component(self, **kwargs: object) -> None:
            captured["component"] = kwargs

    class _Inspector:
        def __init__(self, _conn: object) -> None:
            pass

        def exists(self, db_name: str, table_name: str) -> bool:
            captured["exists"] = (db_name, table_name)
            return True

        def count(self, db_name: str, table_name: str) -> int:
            captured["count"] = (db_name, table_name)
            return 1

        def digest(self, db_name: str, table_name: str) -> str:
            captured["digest"] = (db_name, table_name)
            return "1:1:1"

    monkeypatch.setattr(recording, "PromotionLedger", _Ledger)
    monkeypatch.setattr(mysql_ops, "MySQLMart", _Inspector)
    identity = PromotionIdentity(
        promotion_run_id="run-serving",
        epoch="epoch-serving",
        ingest_run_id="ingest-serving",
        serving_db="serving_authoritative",
        generation_db="generation_ephemeral",
    )

    record_mysql_component(
        object(),
        identity=identity,
        component="general",
        table_pairs=(("live_table", "backup_table"),),
    )

    assert captured["dialect"] == "mysql"
    assert captured["schema_db"] == "serving_authoritative"
    assert captured["ensured"] is True
    assert captured["exists"] == ("serving_authoritative", "backup_table")
