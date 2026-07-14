from __future__ import annotations

from pathlib import Path
import subprocess

from pipeline.scripts.deploy import strategic_reload_publish as publish
from pipeline.scripts.deploy.mart_load_ops import PublishAction
from pipeline.scripts.deploy.mart_load_verify import CanonicalDigest


class RecordingCursor:
    def __init__(self, connection: "RecordingConnection") -> None:
        self.connection = connection

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.connection.executed.append((" ".join(sql.split()), params))

    def fetchall(self) -> list[dict[str, object]]:
        if not self.connection.results:
            return []
        return self.connection.results.pop(0)


class RecordingConnection:
    def __init__(self, results: list[list[dict[str, object]]] | None = None) -> None:
        self.executed: list[tuple[str, object]] = []
        self.results = list(results or [[], []])

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self)


def test_strategic_reload_tables_are_exact_eight_body_tables() -> None:
    assert publish.STRATEGIC_RELOAD_TABLES == (
        "mart_strategic_ml_brand_metric",
        "mart_strategic_cd_brand_metric",
        "mart_strategic_ml_market_metric",
        "mart_strategic_cd_market_metric",
        "cache_brands",
        "cache_market_status",
        "cache_cause",
        "cache_deep_analysis",
    )


def test_legacy_publish_allowlist_rejects_analysis_level_blocks() -> None:
    try:
        publish.validate_publish_tables(("mart_analysis_level_block",))
    except ValueError as exc:
        assert "mart_analysis_level_block" in str(exc)
    else:
        raise AssertionError("MALB must use the paired blue-green publisher")


def test_validate_publish_tables_rejects_general_mart() -> None:
    try:
        publish.validate_publish_tables(("mart_strategic_ml_brand_metric", "mart_general_brand_metric"))
    except ValueError as exc:
        assert "mart_general_brand_metric" in str(exc)
    else:
        raise AssertionError("expected general mart table to be rejected")


def test_guard_publish_requires_explicit_operating_target_flag() -> None:
    try:
        publish.guard_publish_run(build_db="jw_mart_pubtest_build", target_db="jw_mart", allow_operating_target=False)
    except RuntimeError as exc:
        assert "--allow-operating-target" in str(exc)
    else:
        raise AssertionError("expected protected target guard")


def test_resolve_catalog_root_requires_output_catalog(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "repo"
    output_catalog = project_root / "output" / "catalog"
    parquet = project_root / "parquet"
    output_catalog.mkdir(parents=True)
    parquet.mkdir()
    monkeypatch.setattr(publish, "PROJECT_ROOT", project_root)

    assert publish.resolve_publish_catalog_root(output_catalog) == output_catalog.resolve()

    try:
        publish.resolve_publish_catalog_root(parquet)
    except ValueError as exc:
        assert "output/catalog" in str(exc)
    else:
        raise AssertionError("expected non-output catalog root to be rejected")


def test_publish_calls_atomic_rename_for_each_reload_table(monkeypatch) -> None:
    calls: list[tuple[str, str, str, str]] = []

    def fake_publish_one(conn: object, build_db: str, target_db: str, table_name: str, run_id: str) -> PublishAction:
        calls.append((build_db, target_db, table_name, run_id))
        return PublishAction(table_name, "atomic_rename", table_name, f"{table_name}__old_{run_id}", 3)

    monkeypatch.setattr(publish, "_publish_one", fake_publish_one)

    summary = publish.publish_strategic_reload_tables(
        object(),
        build_db="jw_mart_pubtest_build",
        target_db="jw_mart_pubtest_target",
        run_id="run123",
    )

    assert [call[2] for call in calls] == list(publish.STRATEGIC_RELOAD_TABLES)
    assert summary.rolled_back is False
    assert len(summary.actions) == 8


def test_dry_run_checks_rows_without_swapping(monkeypatch) -> None:
    published: list[str] = []

    def fake_publish_one(*args: object, **kwargs: object) -> PublishAction:
        published.append("called")
        return PublishAction("unexpected", "atomic_rename", "unexpected", None, 0)

    def fake_table_digest(conn: object, db_name: str, table_name: str) -> CanonicalDigest:
        return CanonicalDigest(row_count=len(table_name), sha256=f"sha-{table_name}")

    monkeypatch.setattr(publish, "_publish_one", fake_publish_one)
    monkeypatch.setattr(publish, "table_digest", fake_table_digest)

    summary = publish.publish_strategic_reload_tables(
        object(),
        build_db="jw_mart_pubtest_build",
        target_db="jw_mart_pubtest_target",
        run_id="run123",
        dry_run=True,
    )

    assert published == []
    assert summary.dry_run is True
    assert summary.actions[0].mode == "dry_run"
    assert summary.actions[0].row_count == len("mart_strategic_ml_brand_metric")


def test_publish_restores_successful_backups_after_later_failure(monkeypatch) -> None:
    published: list[str] = []
    restored: list[str] = []

    def fake_publish_one(conn: object, build_db: str, target_db: str, table_name: str, run_id: str) -> PublishAction:
        published.append(table_name)
        if table_name == "mart_strategic_cd_brand_metric":
            raise RuntimeError("boom")
        return PublishAction(table_name, "atomic_rename", table_name, f"{table_name}__old_{run_id}", 3)

    def fake_restore(conn: object, target_db: str, action: PublishAction, run_id: str) -> None:
        restored.append(action.table)

    monkeypatch.setattr(publish, "_publish_one", fake_publish_one)
    monkeypatch.setattr(publish, "restore_published_table", fake_restore)

    try:
        publish.publish_strategic_reload_tables(
            object(),
            build_db="jw_mart_pubtest_build",
            target_db="jw_mart_pubtest_target",
            run_id="run123",
        )
    except publish.PublishFailedError as exc:
        assert "boom" in str(exc.__cause__)
    else:
        raise AssertionError("expected publish failure")

    assert published == ["mart_strategic_ml_brand_metric", "mart_strategic_cd_brand_metric"]
    assert restored == ["mart_strategic_ml_brand_metric"]


def test_f124a_publish_uses_one_two_move_rename_after_preflight(monkeypatch) -> None:
    conn = RecordingConnection()

    def fake_exists(_conn: object, _db: str, table: str) -> bool:
        return table in {
            publish.F124A_LIVE_TABLE,
            publish.F124A_STAGING_TABLE,
        }

    monkeypatch.setattr(publish, "table_exists", fake_exists)
    monkeypatch.setattr(
        publish,
        "table_digest",
        lambda *_args: CanonicalDigest(row_count=916_076, sha256="stage-sha"),
    )

    action = publish.publish_f124a_general_dimension(
        conn,
        target_db=publish.F124A_TARGET_DB,
        run_id="run123",
        lock_wait_timeout_seconds=7,
    )

    rename_sql = [sql for sql, _params in conn.executed if sql.startswith("RENAME TABLE")]
    assert len(rename_sql) == 1
    assert rename_sql[0].count(" TO ") == 2
    assert publish.F124A_STAGING_TABLE in rename_sql[0]
    assert action.backup_table == f"{publish.F124A_LIVE_TABLE}__old_run123"
    assert any(sql == "SET SESSION lock_wait_timeout=%s" and params == (7,) for sql, params in conn.executed)
    transaction_queries = [
        (sql, params) for sql, params in conn.executed if "information_schema.innodb_trx" in sql
    ]
    assert len(transaction_queries) == 1
    assert "information_schema.processlist" in transaction_queries[0][0]
    assert transaction_queries[0][1] == (publish.F124A_TARGET_DB,)
    metadata_queries = [
        (sql, params) for sql, params in conn.executed if "performance_schema.metadata_locks" in sql
    ]
    assert len(metadata_queries) == 1
    assert "performance_schema.threads" in metadata_queries[0][0]


def test_f124a_publish_rejects_missing_staging_table(monkeypatch) -> None:
    conn = RecordingConnection()
    monkeypatch.setattr(
        publish,
        "table_exists",
        lambda _conn, _db, table: table == publish.F124A_LIVE_TABLE,
    )

    try:
        publish.publish_f124a_general_dimension(
            conn,
            target_db=publish.F124A_TARGET_DB,
            run_id="run123",
        )
    except RuntimeError as exc:
        assert "staging table missing" in str(exc)
    else:
        raise AssertionError("missing staging table must fail")

    assert not any(sql.startswith("RENAME TABLE") for sql, _params in conn.executed)


def test_f124a_publish_rejects_active_transaction_or_metadata_lock(monkeypatch) -> None:
    conn = RecordingConnection(
        results=[
            [{"trx_mysql_thread_id": 42}],
            [{"owner_thread_id": 84}],
        ]
    )
    monkeypatch.setattr(
        publish,
        "table_exists",
        lambda _conn, _db, table: table in {publish.F124A_LIVE_TABLE, publish.F124A_STAGING_TABLE},
    )

    try:
        publish.publish_f124a_general_dimension(
            conn,
            target_db=publish.F124A_TARGET_DB,
            run_id="run123",
        )
    except RuntimeError as exc:
        assert "active transaction" in str(exc)
    else:
        raise AssertionError("active transaction must fail")

    assert not any(sql.startswith("RENAME TABLE") for sql, _params in conn.executed)


def test_candidate_image_probe_rejects_unpullable_digest(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:2] == ["kubectl", "run"]:
            return subprocess.CompletedProcess(command, 0, stdout="pod/f124a-image-run123 created\n", stderr="")
        if command[:2] == ["kubectl", "wait"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="ImagePullBackOff")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(publish.subprocess, "run", fake_run)

    try:
        publish.probe_candidate_image_pullable(
            "registry.example/app@sha256:" + "c" * 64,
            namespace="llmops",
            run_id="run123",
            timeout_seconds=1,
        )
    except publish.ImagePullPreflightError as exc:
        assert "ImagePullBackOff" in str(exc)
    else:
        raise AssertionError("unpullable image must fail")

    assert any(command[:2] == ["kubectl", "delete"] for command in calls)


def test_f124a_main_returns_one_when_staging_is_missing(monkeypatch, capsys) -> None:
    class Connection:
        def close(self) -> None:
            pass

    args = type(
        "Args",
        (),
        {
            "f124a_general_dimension": True,
            "build_db": None,
            "target_db": publish.F124A_TARGET_DB,
            "run_id": "run123",
            "catalog_root": None,
            "allow_operating_target": False,
            "dry_run": False,
            "candidate_image": "registry.example/app@sha256:" + "a" * 64,
            "pull_probe_namespace": "llmops",
            "lock_wait_timeout": 10,
        },
    )()
    monkeypatch.setattr(publish, "parse_args", lambda _argv=None: args)
    monkeypatch.setattr(publish, "probe_candidate_image_pullable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publish, "connect_admin", Connection)
    monkeypatch.setattr(
        publish,
        "publish_f124a_general_dimension",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("staging table missing")),
    )

    assert publish.main([]) == 1
    assert "staging table missing" in capsys.readouterr().err


def test_f124a_main_returns_one_when_digest_is_unpullable(monkeypatch, capsys) -> None:
    args = type(
        "Args",
        (),
        {
            "f124a_general_dimension": True,
            "build_db": None,
            "target_db": publish.F124A_TARGET_DB,
            "run_id": "run123",
            "catalog_root": None,
            "allow_operating_target": False,
            "dry_run": False,
            "candidate_image": "registry.example/app@sha256:" + "b" * 64,
            "pull_probe_namespace": "llmops",
            "lock_wait_timeout": 10,
        },
    )()
    monkeypatch.setattr(publish, "parse_args", lambda _argv=None: args)
    monkeypatch.setattr(
        publish,
        "probe_candidate_image_pullable",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(publish.ImagePullPreflightError("ImagePullBackOff")),
    )

    assert publish.main([]) == 1
    assert "ImagePullBackOff" in capsys.readouterr().err
