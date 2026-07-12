from __future__ import annotations

from contextlib import nullcontext

from pipeline.scripts.agent3.db import DbConfig
from pipeline.scripts.agent3.repository import Agent3Repository, _market_scope_variants


class _Cursor:
    def __init__(self) -> None:
        self.sql = ""
        self.params: tuple[str, ...] = ()

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[str, ...]) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self) -> list[dict[str, str]]:
        return [
            {
                "brand_key": "리바로브이",
                "brand_name": "리바로 브이",
                "source": "ubist",
                "atc4_code": "C10A",
                "atc4_desc": "고지혈증 치료제",
                "raw_value_history": '{"2026-05": 500000000}',
            }
        ]


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor


def test_market_scope_variants_include_ubist_canonical_alias() -> None:
    # Given
    rows = [{"source": "ubist", "atc4_code": "C10A"}]

    # When
    scopes = _market_scope_variants(rows)

    # Then
    assert scopes == {"ubist": ("C010A", "C10A")}


def test_load_market_metric_rows_queries_only_requested_market_scopes(monkeypatch) -> None:
    # Given
    cursor = _Cursor()
    connection = _Connection(cursor)
    monkeypatch.setattr(
        "pipeline.scripts.agent3.repository.connect",
        lambda _config: nullcontext(connection),
    )
    repository = Agent3Repository(
        DbConfig(host="unused", port=3306, user="unused", password="", database="unused")
    )
    general_rows = [
        {"source": "ubist", "atc4_code": "C10A"},
        {"source": "iqvia_nsa", "atc4_code": "C10A"},
    ]

    # When
    result = repository.load_market_metric_rows(general_rows)

    # Then
    assert "source=%s AND UPPER(atc4_code) IN" in cursor.sql
    assert cursor.params == ("iqvia_nsa", "C10A", "ubist", "C010A", "C10A")
    assert len(result) == 1
    assert "atc4_desc" in cursor.sql
    assert result[0].atc4_desc == "고지혈증 치료제"
    assert result[0].raw_value_history == {"2026-05": 500_000_000.0}
