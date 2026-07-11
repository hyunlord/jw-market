from __future__ import annotations

from pipeline.scripts.etl.phase29_events import (
    _filter_cut_b_rows,
    _filter_news_exposure_rows,
    _query_events,
    ensure_events_raw_table,
    format_event,
)


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self.conn = conn
        self.rowcount = 0

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: object | None = None) -> None:
        self.conn.statements.append(sql)
        self.conn.params.append(params)
        normalized = " ".join(sql.split())
        if normalized.startswith("INSERT INTO events_raw"):
            self.rowcount = 119
        else:
            self.rowcount = 0

    def fetchone(self) -> dict[str, int]:
        return {"cnt": self.conn.fetchone_counts.pop(0) if self.conn.fetchone_counts else 0}

    def fetchall(self) -> list[dict[str, object]]:
        return []


class _FakeConn:
    def __init__(self, counts: list[int] | None = None) -> None:
        self.statements: list[str] = []
        self.params: list[object | None] = []
        self.fetchone_counts = list(counts or [])

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


def test_news_and_cut_b_filters_share_processor_policy() -> None:
    rows = [
        {"id": "legacy-news", "tag": "자본/경영", "score": 43, "source_processor": None},
        {"id": "new-news-low", "tag": "자본/경영", "score": 52, "source_processor": "workflow_196_rev5674"},
        {"id": "new-news-edge", "tag": "자본/경영", "score": 53, "source_processor": "workflow_196_rev5674"},
        {"id": "other", "tag": "기타", "score": 100, "source_processor": None},
    ]

    news = _filter_news_exposure_rows(rows)

    assert [row["id"] for row in news] == ["legacy-news", "new-news-low", "new-news-edge"]

    cut_b_rows = [
        {"id": "legacy-80", "tag": "자본/경영", "score": 80, "source_processor": None},
        {"id": "new-87", "tag": "자본/경영", "score": 87, "source_processor": "workflow_196_rev5674"},
        {"id": "new-88", "tag": "자본/경영", "score": 88, "source_processor": "workflow_196_rev5674"},
    ]

    cut_b = _filter_cut_b_rows(cut_b_rows)

    assert [row["id"] for row in cut_b] == ["legacy-80", "new-88"]


def test_formatted_event_keeps_effective_cut_threshold_without_internal_processor() -> None:
    row = {
        "event_id": "e1",
        "news_id": "n1",
        "brand_name": "Brand",
        "score": 88,
        "tag": "신약/R&D",
        "source_processor": "workflow_196_rev5674",
    }

    event = format_event(row, cut_threshold=88)

    assert event["cut_threshold"] == 88
    assert "source_processor" not in event


def test_events_raw_sync_inserts_missing_rows_without_rewriting_existing_rows() -> None:
    conn = _FakeConn()

    result = ensure_events_raw_table(conn)  # type: ignore[arg-type]
    combined_sql = "\n".join(conn.statements)

    assert result == {"inserted": 119, "gap": 0}
    assert "LEFT JOIN events_raw e ON e.news_id = n.news_id" in combined_sql
    assert "WHERE e.news_id IS NULL" in combined_sql
    assert "ON DUPLICATE KEY UPDATE" not in combined_sql


def test_query_events_applies_policy_predicate_in_source_sql() -> None:
    conn = _FakeConn(counts=[1])

    _query_events(  # type: ignore[arg-type]
        conn,
        "리바로",
        min_score=50,
        lookback_months=6,
        limit=10,
        derivation="llm_direct",
    )

    select_sql = next(sql for sql in conn.statements if "FROM event_brand_scores s" in sql)
    select_params = next(params for sql, params in zip(conn.statements, conn.params) if "FROM event_brand_scores s" in sql)

    assert "s.tag <> %s" in select_sql
    assert "s.brand_canonical = %s" in select_sql
    assert "COALESCE(s.brand_canonical, s.brand_name)" not in select_sql
    assert "s.source_processor = %s" in select_sql
    assert "s.source_processor IN (%s, %s)" in select_sql
    assert (
        "s.source_processor <> %s AND s.source_processor <> %s AND s.source_processor <> %s"
        in select_sql
    )
    assert "workflow_196_rev5674" in select_params
    assert "기타" in select_params
