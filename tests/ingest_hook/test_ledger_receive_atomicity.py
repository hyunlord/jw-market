"""MariaDB receive() must serialize one logical identity across connections."""
from __future__ import annotations

import threading

from pipeline.scripts.ingest_hook.ledger import Ledger

IDENTITY = ("2026-03", "ubist", "d" * 64)


class _SharedMariaDBState:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], tuple] = {}
        self.history: list[tuple] = []
        self.transaction_lock = threading.Lock()
        self.initial_selects = threading.Barrier(2)


class _MariaDBCursor:
    def __init__(self, connection: "_MariaDBConnection") -> None:
        self.connection = connection
        self.rowcount = 0
        self._row: tuple | None = None

    def execute(self, sql: str, params: tuple = ()) -> None:
        normalized = " ".join(sql.split())
        state = self.connection.state

        if normalized.startswith("SELECT epoch, category, manifest_sha"):
            key = (str(params[0]), str(params[1]), str(params[2]))
            if "FOR UPDATE" not in normalized:
                state.initial_selects.wait(timeout=2)
            self._row = state.rows.get(key)
            return

        if normalized.startswith("INSERT INTO ingest_ledger"):
            self.connection.begin()
            key = (str(params[0]), str(params[1]), str(params[2]))
            existing = state.rows.get(key)
            if existing is not None:
                if "ON DUPLICATE KEY UPDATE" not in normalized:
                    raise RuntimeError("uq_ledger_identity duplicate")
                self.rowcount = 0
                return
            state.rows[key] = (
                params[0],
                params[1],
                params[2],
                params[3],
                params[4],
                params[5],
                None,
                None,
                None,
                None,
                params[6],
                None,
                None,
            )
            self.rowcount = 1
            return

        if normalized.startswith("SELECT status, job_name, run_id FROM ingest_ledger"):
            self.connection.begin()
            key = (str(params[0]), str(params[1]), str(params[2]))
            row = state.rows.get(key)
            self._row = None if row is None else (row[5], row[7], row[8])
            return

        if normalized.startswith("UPDATE ingest_ledger SET status="):
            key = (str(params[-4]), str(params[-3]), str(params[-2]))
            row = state.rows[key]
            expected_status = str(params[-1]) if "AND status=" in normalized else row[5]
            if row[5] != expected_status:
                self.rowcount = 0
                return
            mutable = list(row)
            mutable[5] = params[0]
            mutable[6] = params[1]
            mutable[4] = params[3]
            mutable[10] = params[2]
            state.rows[key] = tuple(mutable)
            self.rowcount = 1
            return

        if normalized.startswith("INSERT INTO ingest_status_transition"):
            state.history.append(params)
            self.rowcount = 1
            return

        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self) -> tuple | None:
        return self._row


class _MariaDBConnection:
    def __init__(self, state: _SharedMariaDBState) -> None:
        self.state = state
        self._owns_lock = False

    def cursor(self) -> _MariaDBCursor:
        return _MariaDBCursor(self)

    def begin(self) -> None:
        if not self._owns_lock:
            self.state.transaction_lock.acquire()
            self._owns_lock = True

    def commit(self) -> None:
        self._release()

    def rollback(self) -> None:
        self._release()

    def ping(self, reconnect: bool = False) -> None:
        del reconnect

    def _release(self) -> None:
        if self._owns_lock:
            self._owns_lock = False
            self.state.transaction_lock.release()


def test_receive_is_duplicate_safe_across_two_mariadb_connections() -> None:
    state = _SharedMariaDBState()
    ledgers = [
        Ledger(_MariaDBConnection(state), dialect="mysql"),
        Ledger(_MariaDBConnection(state), dialect="mysql"),
    ]
    start = threading.Barrier(2)
    decisions = []
    errors: list[BaseException] = []

    def receive(ledger: Ledger) -> None:
        start.wait()
        try:
            decisions.append(
                ledger.receive(*IDENTITY, manifest_path="/input/manifest.json")
            )
        except BaseException as exc:  # noqa: BLE001 - preserve thread failures for assertion.
            errors.append(exc)

    threads = [threading.Thread(target=receive, args=(ledger,)) for ledger in ledgers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(decision.action for decision in decisions) == ["noop", "queued"]
    assert len(state.rows) == 1
    assert len(state.history) == 1


def test_mariadb_receive_uses_duplicate_safe_insert_lock_and_status_cas() -> None:
    state = _SharedMariaDBState()
    state.initial_selects = threading.Barrier(1)
    connection = _MariaDBConnection(state)
    ledger = Ledger(connection, dialect="mysql")

    ledger.receive(*IDENTITY, manifest_path="/input/manifest.json")
    row = list(state.rows[IDENTITY])
    row[5] = "failed"
    state.rows[IDENTITY] = tuple(row)

    decision = ledger.receive(*IDENTITY, manifest_path="/input/retry.json")

    assert decision.action == "queued"
    assert state.rows[IDENTITY][5] == "queued"
    assert [transition[5] for transition in state.history] == ["queued", "queued"]
