from __future__ import annotations

from pipeline.scripts.deploy import brand_activity_topic_blue_green as blue_green


class RecordingCursor:
    def __init__(self, statements: list[tuple[str, object]]) -> None:
        self.statements = statements
        self.rows: list[object] = []

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.statements.append((sql, params))

    def fetchone(self) -> object:
        return self.rows.pop(0)


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self.statements)


def test_prepare_creates_only_the_approved_staging_pair(monkeypatch) -> None:
    conn = RecordingConnection()
    existing = set(blue_green.LIVE_TABLES)
    monkeypatch.setattr(blue_green, "table_exists", lambda _conn, _db, table: table in existing)

    statements = blue_green.prepare_staging_tables(conn, target_db="jw_brand_activity_stage")

    assert statements == (
        "CREATE TABLE `jw_brand_activity_stage`.`mart_brand_activity_topics_staging` "
        "LIKE `jw_brand_activity_stage`.`mart_brand_activity_topics`",
        "CREATE TABLE `jw_brand_activity_stage`.`mart_brand_activity_topic_runs_staging` "
        "LIKE `jw_brand_activity_stage`.`mart_brand_activity_topic_runs`",
    )
    assert all("REPLACE" not in sql for sql, _ in conn.statements)


def test_switch_uses_one_atomic_rename_after_validation(monkeypatch) -> None:
    conn = RecordingConnection()
    monkeypatch.setattr(
        blue_green,
        "validate_staging_tables",
        lambda *args, **kwargs: blue_green.StagingValidation(
            topic_rows=11,
            topic_brand_count=116,
            run_rows=1,
            invalid_json_rows=0,
        ),
    )
    existing = {*blue_green.LIVE_TABLES, *blue_green.STAGING_TABLES.values()}
    monkeypatch.setattr(blue_green, "table_exists", lambda _conn, _db, table: table in existing)

    summary = blue_green.switch_blue_green_tables(
        conn,
        target_db="jw_brand_activity_stage",
        run_id="20260715_010203",
        expected_topic_rows=11,
        expected_topic_brand_count=116,
        expected_topic_run_id="brand_activity_20260715",
    )

    assert summary.validation is not None
    assert summary.validation.topic_brand_count == 116
    assert len(conn.statements) == 1
    statement, params = conn.statements[0]
    assert params is None
    assert statement.startswith("RENAME TABLE ")
    assert statement.count(" TO ") == 4
    assert "mart_brand_activity_topics_old_20260715_010203" in statement
    assert "mart_brand_activity_topic_runs_old_20260715_010203" in statement


def test_switch_stops_before_sql_when_validation_fails(monkeypatch) -> None:
    conn = RecordingConnection()

    def fail_validation(*args: object, **kwargs: object) -> blue_green.StagingValidation:
        raise RuntimeError("topic brand census mismatch")

    monkeypatch.setattr(blue_green, "validate_staging_tables", fail_validation)

    try:
        blue_green.switch_blue_green_tables(
            conn,
            target_db="jw_brand_activity_stage",
            run_id="20260715_010203",
            expected_topic_rows=11,
            expected_topic_brand_count=116,
            expected_topic_run_id="brand_activity_20260715",
        )
    except RuntimeError as exc:
        assert "census mismatch" in str(exc)
    else:
        raise AssertionError("invalid staging data must stop before rename")

    assert conn.statements == []
