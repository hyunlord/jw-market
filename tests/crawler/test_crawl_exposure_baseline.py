from __future__ import annotations

from typing import Any

import pytest

from pipeline.etl.io.mart.agent2_eligibility import AGENT2_ELIGIBILITY_REVISION
from pipeline.scripts.crawler.crawl_exposure_baseline import (
    BaselineOrphanError,
    load_eligible_baseline_rows,
)


class BaselineCursor:
    def __init__(self, *, orphan_count: int = 0) -> None:
        self.orphan_count = orphan_count
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self) -> "BaselineCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.calls.append((sql, params))

    def fetchone(self) -> dict[str, int]:
        return {"orphans": self.orphan_count}

    def fetchall(self) -> list[dict[str, Any]]:
        return [
            {"brand_canonical": "리바로", "news_id": "n-1"},
            {"brand_canonical": "리바로", "news_id": "n-2"},
        ]


class BaselineConnection:
    def __init__(self, *, orphan_count: int = 0) -> None:
        self.cursor_obj = BaselineCursor(orphan_count=orphan_count)

    def cursor(self) -> BaselineCursor:
        return self.cursor_obj


def test_baseline_uses_central_sql_predicate_and_distinct_news_identity() -> None:
    conn = BaselineConnection()

    result = load_eligible_baseline_rows(conn)

    assert result.eligibility_revision == AGENT2_ELIGIBILITY_REVISION
    assert result.rows == (
        {"brand_canonical": "리바로", "news_id": "n-1"},
        {"brand_canonical": "리바로", "news_id": "n-2"},
    )
    orphan_sql, _ = conn.cursor_obj.calls[0]
    select_sql, _ = conn.cursor_obj.calls[1]
    assert "COUNT(*) AS orphans" in orphan_sql
    assert "LEFT JOIN news_raw" in orphan_sql
    assert "SELECT DISTINCT" in select_sql
    assert "JOIN news_raw" in select_sql
    assert "workflow_196_rev5674" not in select_sql
    assert "score >= 50" not in select_sql


def test_baseline_orphan_census_fails_closed_before_snapshot() -> None:
    conn = BaselineConnection(orphan_count=2)

    with pytest.raises(BaselineOrphanError, match="orphan_count=2"):
        load_eligible_baseline_rows(conn)

    assert len(conn.cursor_obj.calls) == 1
