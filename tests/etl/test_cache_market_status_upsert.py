from __future__ import annotations

from pymysql.err import OperationalError

from pipeline.scripts.etl import build_cache_market_status, cache_build_common


class _NoDeleteCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params):
        if sql.startswith("REPLACE INTO"):
            raise OperationalError(1142, "DELETE command denied")
        self.executed.append((sql, params))


class _Connection:
    def __init__(self) -> None:
        self.cursor_value = _NoDeleteCursor()
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def close(self) -> None:
        self.closed = True


def test_market_status_upsert_does_not_require_delete_privilege(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(cache_build_common, "mariadb_connect", lambda: connection)

    cache_build_common.upsert_rows(
        "cache_market_status",
        ["query_key", "response_json", "payload_size", "build_sha", "input_manifest_json"],
        [{
            "query_key": "default",
            "response_json": '{"brand_cards":[]}',
            "payload_size": 18,
            "build_sha": "new-sha",
            "input_manifest_json": '{"source":"fixture"}',
        }],
    )

    sql, params = connection.cursor_value.executed[0]
    assert sql == (
        "INSERT INTO `cache_market_status` "
        "(`query_key`, `response_json`, `payload_size`, `build_sha`, `input_manifest_json`) "
        "VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE "
        "`query_key` = VALUES(`query_key`), "
        "`response_json` = VALUES(`response_json`), "
        "`payload_size` = VALUES(`payload_size`), "
        "`build_sha` = VALUES(`build_sha`), "
        "`input_manifest_json` = VALUES(`input_manifest_json`)"
    )
    assert params == (
        "default",
        '{"brand_cards":[]}',
        18,
        "new-sha",
        '{"source":"fixture"}',
    )
    assert connection.closed is True


def test_market_status_builder_uses_table_specific_upsert() -> None:
    assert build_cache_market_status.upsert_rows is cache_build_common.upsert_rows
