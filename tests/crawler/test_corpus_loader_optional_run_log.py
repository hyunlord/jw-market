from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.scripts.agent_2 import corpus_loader


class _Cursor:
    rowcount = 0
    lastrowid = 0

    def __init__(self) -> None:
        self.executed: list[str] = []
        self._result: dict[str, Any] | None = None

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, _params: object = None) -> None:
        normalized = " ".join(sql.split())
        self.executed.append(normalized)
        if "information_schema.TABLES" in normalized:
            self._result = {"table_exists": 0}
            return
        if "agent_run_log" in normalized:
            raise AssertionError("missing optional telemetry table must not be written")

    def fetchone(self) -> dict[str, Any] | None:
        return self._result


class _Connection:
    def __init__(self) -> None:
        self.cursor_instance = _Cursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


def test_load_continues_when_optional_agent_run_log_table_is_absent(
    monkeypatch: Any, tmp_path: Path
) -> None:
    connection = _Connection()
    monkeypatch.setattr(corpus_loader.pymysql, "connect", lambda **_kwargs: connection)
    monkeypatch.setattr(corpus_loader, "processed_files", lambda _corpus: [])

    result = corpus_loader.load_to_db(
        tmp_path,
        resolver=object(),  # No files means the resolver is not consulted.
        db_host="db",
        db_port=3306,
        db_user="user",
        db_password="secret",
        db_name="mart",
        batch_size=100,
        processed_by="test",
        tier=1,
        collected_at=None,
    )

    assert result["run_log_available"] is False
    assert result["run_id"] is None
    assert result["processed_json_found"] == 0
    assert any("information_schema.TABLES" in sql for sql in connection.cursor_instance.executed)
    assert not any("INSERT INTO agent_run_log" in sql for sql in connection.cursor_instance.executed)
