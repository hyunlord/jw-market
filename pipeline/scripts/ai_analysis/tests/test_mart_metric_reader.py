from __future__ import annotations

from bundle_builder.mart_metric_reader import fetch_metric_rows


class _Cursor:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self._index = -1

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, sql: str, _params: tuple) -> None:
        assert "cache_cause" not in sql
        self.queries.append(sql)
        self._index += 1

    def fetchone(self) -> dict:
        if "SHOW TABLES" in self.queries[self._index]:
            return {"table": "catalog_strategic_brand"}
        if "COUNT(DISTINCT" in self.queries[self._index]:
            return {"member_count": 7}
        if "mart_strategic_cd_brand_metric" in self.queries[self._index]:
            return {"id": 11, "brand_key": "livalo", "brand_name": "리바로"}
        return {"id": 22, "cd_market_id": "cd_007", "market_size_series": "{}"}

    def fetchall(self) -> list[dict]:
        return [
            {"id": 11, "brand_key": "livalo", "brand_name": "리바로"},
            {"id": 12, "brand_key": "lipitor", "brand_name": "리피토"},
        ]


class _Connection:
    def __init__(self) -> None:
        self.cursor_obj = _Cursor()

    def cursor(self) -> _Cursor:
        return self.cursor_obj


def test_fetch_cd_metric_rows_uses_only_strategic_marts() -> None:
    conn = _Connection()

    rows = fetch_metric_rows(
        "리바로",
        "cd_007",
        "competitive_dynamics",
        "IQVIA",
        "sales",
        conn,
    )

    assert rows is not None
    assert rows.brand_row["brand_name"] == "리바로"
    assert rows.market_row["cd_market_id"] == "cd_007"
    assert [row["brand_key"] for row in rows.sibling_rows] == ["livalo", "lipitor"]
    assert rows.catalog_member_count == 7
    assert len(conn.cursor_obj.queries) == 5
    assert all(
        "mart_strategic_cd_" in sql or "catalog_strategic_brand" in sql or "SHOW TABLES" in sql
        for sql in conn.cursor_obj.queries
    )
    assert any("WHERE cd_id = %s" in sql for sql in conn.cursor_obj.queries)
