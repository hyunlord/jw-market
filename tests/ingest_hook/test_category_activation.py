from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook import category_activation as activation


def _manifest(
    path: Path,
    *,
    category: str = "iqvia_nsa",
    epoch: str = "2026-Q1",
    tables: list[dict[str, str]] | None = None,
) -> Path:
    payload = {
        "schema_version": "ingest-table-load-v1",
        "category": category,
        "epoch": epoch,
        "loader": "fixture",
        "primary": {
            "schema": "jw_ingest_stage_test",
            "table": "iqvia_nsa_quarterly_raw",
            "kind": "append",
            "rows_before": 0,
            "rows_after": 2,
            "rows_loaded": 2,
            "source_rows": 2,
            "difference_reasons": [],
        },
        "tables": tables
        or [
            {
                "schema": "jw_ingest_stage_test",
                "table": "iqvia_nsa_quarterly_raw",
                "kind": "append",
                "rows_before": 0,
                "rows_after": 2,
                "rows_loaded": 2,
                "source_rows": 2,
                "difference_reasons": [],
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class Cursor:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def __enter__(self) -> Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> int:
        self.connection.statements.append((sql, params))
        return 1

    def fetchone(self) -> tuple[int]:
        value = self.connection.fetchone_values.pop(0)
        if isinstance(value, tuple):
            return value
        return (value,)

    def fetchall(self) -> list[tuple[str, str]]:
        return [
            ("id", "auto_increment"),
            ("period_label", ""),
            ("period_ym", ""),
            ("payload", ""),
        ]

    def close(self) -> None:
        return None


class Connection:
    def __init__(self, fetchone_values: list[int | tuple[int]] | None = None) -> None:
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []
        self.fetchone_values = fetchone_values or [2, 2, 2, 2]
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> Cursor:
        return Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _shadow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        activation.ENV_BUILD_PREFIX,
        "jw_ingest_shadow_category_build",
    )
    monkeypatch.setenv(
        activation.ENV_TARGET_IQVIA_NSA_DB,
        "jw_ingest_shadow_iqvia",
    )


def _published_result(
    *,
    category: str = "iqvia_nsa",
    epoch: str = "2026-Q1",
    run_id: str = "run_1",
    build_schema: str = "jw_ingest_shadow_category_build_run_1",
    tables: tuple[str, ...] = ("iqvia_nsa_quarterly_raw",),
    target_tables: tuple[str, ...] = (
        "jw_ingest_shadow_iqvia.iqvia_nsa_quarterly_raw",
    ),
) -> activation.ActivationResult:
    return activation.ActivationResult(
        category=category,
        epoch=epoch,
        run_id=run_id,
        build_schema=build_schema,
        tables=tables,
        target_tables=target_tables,
        row_counts={table: 2 for table in tables},
        dry_run=False,
        published=True,
    )


def test_supports_only_bounded_production_categories() -> None:
    assert activation.supports("iqvia_nsa") is True
    assert activation.supports("iqvia_csd_channel") is True
    assert activation.supports("iqvia_csd_keyword") is True
    assert activation.supports("mi_master") is True
    assert activation.supports("ubist") is False


def test_activate_requires_declarative_target_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path / "_manifest.json")
    monkeypatch.delenv(activation.ENV_TARGET_IQVIA_NSA_DB, raising=False)

    with pytest.raises(activation.ActivationError, match=activation.ENV_TARGET_IQVIA_NSA_DB):
        activation.activate("iqvia_nsa", manifest, "2026-Q1", "run-1")


def test_activate_rejects_manifest_tables_outside_category_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(
        tmp_path / "_manifest.json",
        tables=[
            {
                "schema": "jw_ingest_stage_test",
                "table": "unexpected_table",
                "kind": "append",
                "rows_before": 0,
                "rows_after": 1,
                "rows_loaded": 1,
                "source_rows": 1,
                "difference_reasons": [],
            }
        ],
    )
    _shadow_env(monkeypatch)

    with pytest.raises(activation.ActivationError, match="allowlist"):
        activation.activate("iqvia_nsa", manifest, "2026-Q1", "run-1")


def test_dry_run_returns_plan_without_opening_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _manifest(tmp_path / "_manifest.json")
    _shadow_env(monkeypatch)
    monkeypatch.setattr(
        activation,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("connected")),
    )

    result = activation.activate("iqvia_nsa", tmp_path, "2026-Q1", "run-1", dry_run=True)

    assert result.category == "iqvia_nsa"
    assert result.dry_run is True
    assert result.published is False
    assert result.build_schema == "jw_ingest_shadow_category_build_run_1"
    assert result.tables == ("iqvia_nsa_quarterly_raw",)


def test_activate_nsa_uses_20_quarter_candidate_and_atomic_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path / "_manifest.json")
    _shadow_env(monkeypatch)
    connection = Connection(fetchone_values=[2])
    monkeypatch.setattr(activation, "connect", lambda *_args, **_kwargs: connection)

    result = activation.activate("iqvia_nsa", manifest, "2026-Q1", "run-1")

    copy_sql, copy_params = next(
        (sql, params)
        for sql, params in connection.statements
        if sql.startswith("INSERT INTO `jw_ingest_shadow_category_build_run_1`")
    )
    assert "`period_label` IN" in copy_sql
    assert copy_params == (
        "2021Q2",
        "2021Q3",
        "2021Q4",
        "2022Q1",
        "2022Q2",
        "2022Q3",
        "2022Q4",
        "2023Q1",
        "2023Q2",
        "2023Q3",
        "2023Q4",
        "2024Q1",
        "2024Q2",
        "2024Q3",
        "2024Q4",
        "2025Q1",
        "2025Q2",
        "2025Q3",
        "2025Q4",
        "2026Q1",
    )
    rename_sql = [sql for sql, _params in connection.statements if sql.startswith("RENAME TABLE")]
    assert rename_sql == [
        "RENAME TABLE `jw_ingest_shadow_iqvia`.`iqvia_nsa_quarterly_raw` "
        "TO `jw_ingest_shadow_iqvia`.`iqvia_nsa_quarterly_raw__old_run_1`, "
        "`jw_ingest_shadow_category_build_run_1`.`iqvia_nsa_quarterly_raw` "
        "TO `jw_ingest_shadow_iqvia`.`iqvia_nsa_quarterly_raw`"
    ]
    assert result.published is True
    assert result.row_counts == {"iqvia_nsa_quarterly_raw": 2}


def test_finalize_drops_only_published_backup_and_build_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _shadow_env(monkeypatch)
    connection = Connection()
    monkeypatch.setattr(activation, "connect", lambda *_args, **_kwargs: connection)

    activation.finalize(_published_result())

    statements = [sql for sql, _params in connection.statements]
    assert statements == [
        "DROP TABLE IF EXISTS "
        "`jw_ingest_shadow_iqvia`.`iqvia_nsa_quarterly_raw__old_run_1`",
        "DROP SCHEMA IF EXISTS `jw_ingest_shadow_category_build_run_1`",
    ]
    assert connection.commits == 1
    assert connection.closed is True


def test_nsa_candidate_preserves_unsubmitted_quarters_and_replaces_staged_periods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path / "_manifest.json")
    _shadow_env(monkeypatch)
    connection = Connection(fetchone_values=[4])
    monkeypatch.setattr(activation, "connect", lambda *_args, **_kwargs: connection)

    activation.activate("iqvia_nsa", manifest, "2026-Q1", "run-1")

    inserts = [
        (sql, params)
        for sql, params in connection.statements
        if sql.startswith("INSERT INTO `jw_ingest_shadow_category_build_run_1`")
    ]
    assert len(inserts) == 2
    existing_sql, existing_params = inserts[0]
    staged_sql, staged_params = inserts[1]
    assert (
        "FROM `jw_ingest_shadow_iqvia`.`iqvia_nsa_quarterly_raw` existing"
        in existing_sql
    )
    assert "NOT EXISTS" in existing_sql
    assert (
        "staged.`period_label` = existing.`period_label`"
        in existing_sql
    )
    assert len(existing_params or ()) == 20
    assert (
        "FROM `jw_ingest_stage_test`.`iqvia_nsa_quarterly_raw` staged"
        in staged_sql
    )
    assert len(staged_params or ()) == 20
    assert all("`id`" not in sql for sql, _params in inserts)
    assert all("SELECT *" not in sql for sql, _params in inserts)


def test_csd_stage_candidate_replaces_only_submitted_months(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(
        tmp_path / "_manifest.json",
        category="iqvia_csd_channel",
        epoch="2026-03",
        tables=[
            {
                "schema": "jw_ingest_stage_raw",
                "table": "raw_csd_channel_dynamics",
                "kind": "append",
                "rows_before": 0,
                "rows_after": 2,
                "rows_loaded": 2,
                "source_rows": 2,
                "difference_reasons": [],
            },
            {
                "schema": "jw_ingest_stage_stage",
                "table": "csd_channel_dynamics_stage",
                "kind": "replace",
                "rows_before": 0,
                "rows_after": 2,
                "rows_loaded": 2,
                "source_rows": 2,
                "difference_reasons": [],
            },
        ],
    )
    monkeypatch.setenv(activation.ENV_BUILD_PREFIX, "jw_ingest_shadow_category_build")
    monkeypatch.setenv(activation.ENV_TARGET_CSD_RAW_DB, "jw_ingest_shadow_csd_raw")
    monkeypatch.setenv(activation.ENV_TARGET_CSD_STAGE_DB, "jw_ingest_shadow_csd_stage")
    connection = Connection(fetchone_values=[3, 3])
    monkeypatch.setattr(activation, "connect", lambda *_args, **_kwargs: connection)

    activation.activate("iqvia_csd_channel", manifest, "2026-03", "run-1")

    stage_inserts = [
        sql
        for sql, _params in connection.statements
        if sql.startswith("INSERT INTO")
        and "csd_channel_dynamics_stage" in sql
    ]
    assert any(
        "FROM `jw_ingest_shadow_csd_stage`.`csd_channel_dynamics_stage` existing"
        in sql
        and "NOT EXISTS" in sql
        and "staged.`period_ym` = existing.`period_ym`" in sql
        for sql in stage_inserts
    )
    assert any(
        "FROM `jw_ingest_stage_stage`.`csd_channel_dynamics_stage` staged"
        in sql
        for sql in stage_inserts
    )


def test_keyword_targets_raw_and_stage_shadow_schemas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(
        tmp_path / "_manifest.json",
        category="iqvia_csd_keyword",
        epoch="2026-03",
        tables=[
            {
                "schema": "jw_ingest_stage_raw",
                "table": "raw_keyword_events",
                "kind": "append",
                "rows_before": 0,
                "rows_after": 2,
                "rows_loaded": 2,
                "source_rows": 2,
                "difference_reasons": [],
            },
            {
                "schema": "jw_ingest_stage_stage",
                "table": "km_keyword_event_stage",
                "kind": "replace",
                "rows_before": 0,
                "rows_after": 2,
                "rows_loaded": 2,
                "source_rows": 2,
                "difference_reasons": [],
            },
        ],
    )
    monkeypatch.setenv(activation.ENV_BUILD_PREFIX, "jw_ingest_shadow_category_build")
    monkeypatch.setenv(activation.ENV_TARGET_CSD_RAW_DB, "jw_ingest_shadow_csd_raw")
    monkeypatch.setenv(activation.ENV_TARGET_CSD_STAGE_DB, "jw_ingest_shadow_csd_stage")

    result = activation.activate(
        "iqvia_csd_keyword", manifest, "2026-03", "run-1", dry_run=True
    )

    assert result.target_tables == (
        "jw_ingest_shadow_csd_raw.raw_keyword_events",
        "jw_ingest_shadow_csd_stage.km_keyword_event_stage",
    )


def test_keyword_stage_candidate_preserves_existing_ids_by_stable_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(
        tmp_path / "_manifest.json",
        category="iqvia_csd_keyword",
        epoch="2026-03",
        tables=[
            {
                "schema": "jw_ingest_stage_raw",
                "table": "raw_keyword_events",
                "kind": "append",
                "rows_before": 0,
                "rows_after": 2,
                "rows_loaded": 2,
                "source_rows": 2,
                "difference_reasons": [],
            },
            {
                "schema": "jw_ingest_stage_stage",
                "table": "km_keyword_event_stage",
                "kind": "replace",
                "rows_before": 0,
                "rows_after": 2,
                "rows_loaded": 2,
                "source_rows": 2,
                "difference_reasons": [],
            },
        ],
    )
    monkeypatch.setenv(activation.ENV_BUILD_PREFIX, "jw_ingest_shadow_category_build")
    monkeypatch.setenv(activation.ENV_TARGET_CSD_RAW_DB, "jw_ingest_shadow_csd_raw")
    monkeypatch.setenv(activation.ENV_TARGET_CSD_STAGE_DB, "jw_ingest_shadow_csd_stage")
    connection = Connection(fetchone_values=[2, 2])
    monkeypatch.setattr(activation, "connect", lambda *_args, **_kwargs: connection)

    activation.activate("iqvia_csd_keyword", manifest, "2026-03", "run-1")

    keyword_inserts = [
        sql
        for sql, _params in connection.statements
        if "km_keyword_event_stage" in sql and sql.startswith("INSERT INTO")
    ]
    assert any(
        "LEFT JOIN `jw_ingest_shadow_csd_stage`.`km_keyword_event_stage`" in sql
        for sql in keyword_inserts
    )
    assert any("existing.`id` IS NOT NULL" in sql for sql in keyword_inserts)
    assert any("existing.`id` IS NULL" in sql for sql in keyword_inserts)


def test_restore_fails_closed_when_backup_table_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _shadow_env(monkeypatch)
    connection = Connection(fetchone_values=[1, 0])
    monkeypatch.setattr(activation, "connect", lambda *_args, **_kwargs: connection)
    result = _published_result()

    with pytest.raises(activation.ActivationError, match="rollback backup missing"):
        activation.restore(result)

    assert not any(sql.startswith("RENAME TABLE") for sql, _params in connection.statements)
    assert not any(sql.startswith("DROP TABLE") for sql, _params in connection.statements)
    assert connection.rollbacks == 1
    assert connection.commits == 0
    assert connection.closed is True


def test_restore_fails_closed_when_backup_identity_does_not_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _shadow_env(monkeypatch)
    connection = Connection(fetchone_values=[1, 1, 1, 0])
    monkeypatch.setattr(activation, "connect", lambda *_args, **_kwargs: connection)
    result = _published_result()

    with pytest.raises(activation.ActivationError, match="rollback backup identity mismatch"):
        activation.restore(result)

    assert not any(sql.startswith("RENAME TABLE") for sql, _params in connection.statements)
    assert connection.rollbacks == 1
    assert connection.commits == 0


def test_restore_uses_one_atomic_rename_for_all_tables_and_cleans_publish_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(activation.ENV_BUILD_PREFIX, "jw_ingest_shadow_category_build")
    monkeypatch.setenv(activation.ENV_TARGET_MI_MASTER_DB, "jw_ingest_shadow_master")
    connection = Connection(fetchone_values=[1, 1, 0, 1, 1, 0])
    monkeypatch.setattr(activation, "connect", lambda *_args, **_kwargs: connection)
    result = _published_result(
        category="mi_master",
        epoch="2026-03",
        tables=("stg_master_market_definition", "stg_master_mapping_table"),
        target_tables=(
            "jw_ingest_shadow_master.stg_master_market_definition",
            "jw_ingest_shadow_master.stg_master_mapping_table",
        ),
    )

    activation.restore(result)

    rename_sql = [
        sql for sql, _params in connection.statements if sql.startswith("RENAME TABLE")
    ]
    assert rename_sql == [
        "RENAME TABLE `jw_ingest_shadow_master`.`stg_master_market_definition` "
        "TO `jw_ingest_shadow_master`.`stg_master_market_definition__failed_run_1`, "
        "`jw_ingest_shadow_master`.`stg_master_market_definition__old_run_1` "
        "TO `jw_ingest_shadow_master`.`stg_master_market_definition`, "
        "`jw_ingest_shadow_master`.`stg_master_mapping_table` "
        "TO `jw_ingest_shadow_master`.`stg_master_mapping_table__failed_run_1`, "
        "`jw_ingest_shadow_master`.`stg_master_mapping_table__old_run_1` "
        "TO `jw_ingest_shadow_master`.`stg_master_mapping_table`"
    ]
    cleanup_sql = [
        sql
        for sql, _params in connection.statements
        if sql.startswith("DROP TABLE") or sql.startswith("DROP SCHEMA")
    ]
    assert cleanup_sql == [
        "DROP TABLE IF EXISTS "
        "`jw_ingest_shadow_master`.`stg_master_market_definition__failed_run_1`",
        "DROP TABLE IF EXISTS `jw_ingest_shadow_master`.`stg_master_mapping_table__failed_run_1`",
        "DROP SCHEMA IF EXISTS `jw_ingest_shadow_category_build_run_1`",
    ]
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed is True
