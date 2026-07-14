from __future__ import annotations

from pipeline.scripts.deploy import analysis_cache_blue_green as blue_green


class RecordingCursor:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.statements.append(sql)


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self.statements)


def test_blue_green_allowlist_is_the_generation_pair() -> None:
    assert blue_green.BLUE_GREEN_PUBLISH_TABLES == (
        "mart_analysis_level_block",
        "cache_brands",
    )


def test_switch_cli_requires_expected_source_epoch() -> None:
    try:
        blue_green.parse_args(
            [
                "--target-db",
                "jw_mart_stage",
                "switch",
                "--run-id",
                "20260714_220000",
                "--expected-brands-sha256",
                "a" * 64,
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("switch must require the approved MALB source epoch")


def test_prepare_creates_only_two_staging_tables(monkeypatch) -> None:
    conn = RecordingConnection()
    existing = set(blue_green.LIVE_TABLES)
    monkeypatch.setattr(
        blue_green,
        "table_exists",
        lambda _conn, _db, table: table in existing,
    )

    blue_green.prepare_staging_tables(conn, target_db="jw_mart_stage")

    assert conn.statements == [
        "CREATE TABLE `jw_mart_stage`.`mart_analysis_level_block_staging` "
        "LIKE `jw_mart_stage`.`mart_analysis_level_block`",
        "CREATE TABLE `jw_mart_stage`.`cache_brands_staging` LIKE `jw_mart_stage`.`cache_brands`",
    ]
    assert all("DROP" not in statement for statement in conn.statements)
    assert all("INSERT INTO" not in statement for statement in conn.statements)


def test_prepare_fails_before_sql_when_staging_exists(monkeypatch) -> None:
    conn = RecordingConnection()
    existing = {
        *blue_green.LIVE_TABLES,
        "mart_analysis_level_block_staging",
    }
    monkeypatch.setattr(
        blue_green,
        "table_exists",
        lambda _conn, _db, table: table in existing,
    )

    try:
        blue_green.prepare_staging_tables(conn, target_db="jw_mart_stage")
    except RuntimeError as exc:
        assert "staging table already exists" in str(exc)
    else:
        raise AssertionError("existing staging identity must stop preparation")

    assert conn.statements == []


def test_switch_uses_one_atomic_rename_after_validation(monkeypatch) -> None:
    conn = RecordingConnection()
    monkeypatch.setattr(
        blue_green,
        "validate_staging_tables",
        lambda *args, **kwargs: blue_green.StagingValidation(
            malb_rows=3138,
            malb_source_epoch="epoch",
            cache_rows=1,
            brand_count=25,
            cache_sha256="a" * 64,
        ),
    )
    monkeypatch.setattr(
        blue_green,
        "table_exists",
        lambda _conn, _db, table: table in {
            "mart_analysis_level_block",
            "mart_analysis_level_block_staging",
            "cache_brands",
            "cache_brands_staging",
        },
    )

    summary = blue_green.switch_blue_green_tables(
        conn,
        target_db="jw_mart_stage",
        run_id="20260714_220000",
        expected_brands_sha256="a" * 64,
        expected_source_epoch="epoch",
    )

    assert len(conn.statements) == 1
    statement = conn.statements[0]
    assert statement.startswith("RENAME TABLE ")
    assert "mart_analysis_level_block_old_20260714_220000" in statement
    assert "cache_brands_old_20260714_220000" in statement
    assert statement.count(" TO ") == 4
    assert summary.validation.malb_rows == 3138


def test_switch_fails_before_sql_when_backup_exists(monkeypatch) -> None:
    conn = RecordingConnection()
    monkeypatch.setattr(
        blue_green,
        "validate_staging_tables",
        lambda *args, **kwargs: blue_green.StagingValidation(3138, "epoch", 1, 25, "a" * 64),
    )
    monkeypatch.setattr(blue_green, "table_exists", lambda _conn, _db, _table: True)

    try:
        blue_green.switch_blue_green_tables(
            conn,
            target_db="jw_mart_stage",
            run_id="20260714_220000",
            expected_brands_sha256="a" * 64,
            expected_source_epoch="epoch",
        )
    except RuntimeError as exc:
        assert "backup table already exists" in str(exc)
    else:
        raise AssertionError("existing backup must stop the switch")

    assert conn.statements == []


def test_switch_rejects_run_id_that_would_overflow_mysql_identifier(monkeypatch) -> None:
    conn = RecordingConnection()
    monkeypatch.setattr(
        blue_green,
        "validate_staging_tables",
        lambda *args, **kwargs: blue_green.StagingValidation(3138, "epoch", 1, 25, "a" * 64),
    )

    try:
        blue_green.switch_blue_green_tables(
            conn,
            target_db="jw_mart_stage",
            run_id="x" * 64,
            expected_brands_sha256="a" * 64,
            expected_source_epoch="epoch",
        )
    except ValueError as exc:
        assert "identifier exceeds 64 characters" in str(exc)
    else:
        raise AssertionError("oversized backup identity must fail before SQL")

    assert conn.statements == []


def test_rollback_uses_one_reverse_atomic_rename(monkeypatch) -> None:
    conn = RecordingConnection()
    existing = {
        "mart_analysis_level_block",
        "mart_analysis_level_block_old_20260714_220000",
        "cache_brands",
        "cache_brands_old_20260714_220000",
    }
    monkeypatch.setattr(
        blue_green,
        "table_exists",
        lambda _conn, _db, table: table in existing,
    )

    summary = blue_green.rollback_blue_green_tables(
        conn,
        target_db="jw_mart_stage",
        run_id="20260714_220000",
    )

    assert len(conn.statements) == 1
    statement = conn.statements[0]
    assert statement.startswith("RENAME TABLE ")
    assert "mart_analysis_level_block_failed_20260714_220000" in statement
    assert "cache_brands_failed_20260714_220000" in statement
    assert statement.count(" TO ") == 4
    assert summary.run_id == "20260714_220000"
