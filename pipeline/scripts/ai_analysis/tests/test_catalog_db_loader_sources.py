from __future__ import annotations

from bundle_builder.catalog_db_loader import detect_available_sources


class _Cursor:
    def __init__(self) -> None:
        self.sql = ""

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, sql: str, _params: tuple) -> None:
        self.sql = sql

    def fetchall(self) -> list[dict[str, str]]:
        return [{"source": "ubist"}, {"source": "iqvia_nsa"}]


class _Connection:
    def __init__(self) -> None:
        self.cursor_obj = _Cursor()

    def cursor(self) -> _Cursor:
        return self.cursor_obj


def test_detect_available_sources_reads_marts_only() -> None:
    conn = _Connection()

    assert detect_available_sources("리바로젯", conn) == ["UBIST", "IQVIA"]
    assert "mart_strategic_ml_brand_metric" in conn.cursor_obj.sql
    assert "mart_strategic_cd_brand_metric" in conn.cursor_obj.sql
    assert "cache_cause" not in conn.cursor_obj.sql
