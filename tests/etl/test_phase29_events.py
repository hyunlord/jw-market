from __future__ import annotations

from pipeline.scripts.etl.phase29_events import (
    _filter_cut_b_rows,
    get_brand_events_cut_a,
    get_brand_events_cut_b,
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


def test_cut_b_query_drops_derivation_and_lookback_restrictions() -> None:
    conn = _FakeConn(counts=[1])

    result = get_brand_events_cut_b(conn, "리바로")  # type: ignore[arg-type]

    select_sql = next(sql for sql in conn.statements if "FROM event_brand_scores s" in sql)

    assert result == []
    assert "s.derivation = %s" not in select_sql
    assert "DATE_SUB" not in select_sql


def test_cut_a_reuses_one_query_per_lookback(monkeypatch) -> None:
    rows = [
        {
            "event_id": f"event-{index}",
            "news_id": f"news-{index}",
            "brand_name": "가드메트",
            "brand_canonical": "가드메트",
            "score": score,
            "tag": "자본/경영",
            "source_processor": None,
            "published_date": f"2026-01-{index:02d}",
            "title": f"title-{index}",
        }
        for index, score in enumerate((50, 49, 48, 47, 46), start=1)
    ]
    calls: list[tuple[int, int | None]] = []

    def fake_query_events(
        _conn: object,
        _brand: str,
        *,
        min_score: int,
        lookback_months: int | None,
        limit: int | None,
        derivation: str | None = None,
    ) -> list[dict[str, object]]:
        assert limit is None
        assert derivation is None
        calls.append((min_score, lookback_months))
        return [row for row in rows if int(row["score"]) >= min_score]

    monkeypatch.setattr("pipeline.scripts.etl.phase29_events._query_events", fake_query_events)

    events, lookback, threshold = get_brand_events_cut_a(
        object(),  # type: ignore[arg-type]
        "가드메트",
        lookback_candidates=[6, 12],
    )

    assert calls == [(50, 6), (0, 6)]
    assert [event["score"] for event in events] == [50, 49, 48, 47, 46]
    assert lookback == 6
    assert threshold == 46


def test_cut_a_keeps_single_query_when_initial_threshold_has_coverage(monkeypatch) -> None:
    rows = [
        {
            "event_id": f"event-{index}",
            "news_id": f"news-{index}",
            "brand_name": "리바로",
            "brand_canonical": "리바로",
            "score": score,
            "tag": "자본/경영",
            "source_processor": None,
            "published_date": f"2026-02-{index:02d}",
            "title": f"title-{index}",
        }
        for index, score in enumerate((55, 54, 53, 52, 51), start=1)
    ]
    calls: list[int] = []

    def fake_query_events(
        _conn: object,
        _brand: str,
        *,
        min_score: int,
        lookback_months: int | None,
        limit: int | None,
        derivation: str | None = None,
    ) -> list[dict[str, object]]:
        assert lookback_months == 6
        assert limit is None
        assert derivation is None
        calls.append(min_score)
        return [row for row in rows if int(row["score"]) >= min_score]

    monkeypatch.setattr("pipeline.scripts.etl.phase29_events._query_events", fake_query_events)

    events, lookback, threshold = get_brand_events_cut_a(
        object(),  # type: ignore[arg-type]
        "리바로",
        lookback_candidates=[6],
    )

    assert calls == [50]
    assert len(events) == 5
    assert lookback == 6
    assert threshold == 50
