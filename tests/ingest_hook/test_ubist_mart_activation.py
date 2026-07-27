from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from pipeline.scripts.ingest_hook import ubist_mart_activation as activation
from pipeline.etl.io.catalog.paths import publish_catalog_outputs


@dataclass(frozen=True)
class _CatalogResult:
    name: str
    output_path: Path
    rows: int = 1


def _provision_catalog(root: Path, *names: str) -> None:
    build_root = root.parent / "catalog-build"
    results = []
    for name in names:
        path = build_root / name / f"{name}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({"name": [name]}), path)
        results.append(_CatalogResult(name=name, output_path=path))
    publish_catalog_outputs(results, build_root=build_root, catalog_root=root)


def test_activation_is_fail_closed_without_explicit_pl_gate(monkeypatch) -> None:
    monkeypatch.delenv(activation.ENV_PROMOTION_APPROVED, raising=False)

    with pytest.raises(RuntimeError, match=activation.ENV_PROMOTION_APPROVED):
        activation.from_env(run_id="run-1")


def test_activation_pins_isolated_build_schema(monkeypatch) -> None:
    monkeypatch.setenv(activation.ENV_PROMOTION_APPROVED, "1")
    monkeypatch.setenv(activation.ENV_SOURCE_DB, "jw_mart")
    monkeypatch.setenv(activation.ENV_TARGET_DB, "jw_mart")
    monkeypatch.setenv(activation.ENV_BUILD_PREFIX, "jw_mart_ingest")

    result = activation.from_env(run_id="20260723-abc")

    assert result.source_db == "jw_mart"
    assert result.target_db == "jw_mart"
    assert result.build_db == "jw_mart_ingest_20260723_abc"


def test_writer_lock_is_fail_closed_and_released() -> None:
    statements: list[tuple[str, tuple[object, ...]]] = []

    class Cursor:
        def execute(self, sql, params):
            statements.append((sql, params))

        def fetchone(self):
            if statements[-1][0].startswith("SELECT IS_USED_LOCK"):
                return (42, 42)
            return (1,)

        def close(self):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

    conn = Connection()
    activation.acquire_writer_lock(conn, timeout_seconds=0)
    activation.release_writer_lock(conn)

    assert statements == [
        ("SELECT GET_LOCK(%s, %s)", (activation.WRITER_LOCK_NAME, 0)),
        (
            "SELECT IS_USED_LOCK(%s), CONNECTION_ID()",
            (activation.WRITER_LOCK_NAME,),
        ),
        ("SELECT RELEASE_LOCK(%s)", (activation.WRITER_LOCK_NAME,)),
    ]


def test_writer_lock_release_rejects_non_owner() -> None:
    class Cursor:
        def execute(self, _sql, _params):
            return None

        def fetchone(self):
            return (41, 42)

        def close(self):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

    with pytest.raises(RuntimeError, match="ownership lost"):
        activation.release_writer_lock(Connection())


def test_build_shadow_uses_isolated_catalog_and_candidate_ubist_roots(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    target = activation.MartActivation("jw_mart", "jw_mart", "jw_mart_ingest_run1")
    monkeypatch.setattr(
        activation,
        "run_s4_general",
        lambda **kwargs: calls.append(kwargs),
    )

    activation.build_shadow(
        target,
        catalog_root=Path("/market-output/shadow/catalog"),
        ubist_dir=Path("/market-output/.ubist-candidate-run1"),
    )

    assert calls == [{
        "build_db": "jw_mart_ingest_run1",
        "source_db": "jw_mart",
        "catalog_root": Path("/market-output/shadow/catalog"),
        "ubist_dir": Path("/market-output/.ubist-candidate-run1"),
        "input_mode": "raw",
        "sources": ("ubist",),
    }]


def test_shadow_catalog_path_is_resolved_after_environment_is_set(
    tmp_path: Path, monkeypatch
) -> None:
    from pipeline.etl.io.mart import general_catalog

    catalog_root = tmp_path / "catalog"
    observed: list[Path] = []
    monkeypatch.setenv("S4_CATALOG_DIR", str(catalog_root))
    monkeypatch.setattr(
        general_catalog.pd,
        "read_parquet",
        lambda path: observed.append(Path(path)) or general_catalog.pd.DataFrame(),
    )

    general_catalog.load_catalog_key_map()

    assert observed == [catalog_root / "strategic_brand" / "strategic_brand.parquet"]


def test_shadow_catalog_root_requires_isolated_canonical_catalog(tmp_path, monkeypatch) -> None:
    shadow_root = tmp_path / "shadow"
    catalog_root = shadow_root / "catalog"
    required = catalog_root / "strategic_brand" / "strategic_brand.parquet"
    required.parent.mkdir(parents=True)
    required.write_bytes(b"canonical-catalog")
    monkeypatch.setenv(activation.ENV_SHADOW_CATALOG_ROOT, str(catalog_root))

    assert activation.shadow_catalog_root_from_env(shadow_root) == catalog_root.resolve()


def test_shadow_catalog_root_rejects_external_catalog_but_allows_refresh_target(
    tmp_path, monkeypatch
) -> None:
    shadow_root = tmp_path / "shadow"
    external = tmp_path / "external"
    monkeypatch.setenv(activation.ENV_SHADOW_CATALOG_ROOT, str(external))

    with pytest.raises(RuntimeError, match="inside the shadow root"):
        activation.shadow_catalog_root_from_env(shadow_root)

    internal = shadow_root / "catalog"
    monkeypatch.setenv(activation.ENV_SHADOW_CATALOG_ROOT, str(internal))
    assert activation.shadow_catalog_root_from_env(shadow_root) == internal.resolve()


def test_production_catalog_preflight_validates_before_s4(tmp_path, monkeypatch) -> None:
    catalog_root = tmp_path / "catalog"
    _provision_catalog(catalog_root, "strategic_brand", "strategic_product")
    monkeypatch.setenv(activation.CATALOG_ROOT_ENV, str(catalog_root))

    assert activation.production_catalog_root_from_env() == catalog_root.resolve()


def test_production_catalog_resolver_leaves_validation_to_refresh(tmp_path, monkeypatch) -> None:
    catalog_root = tmp_path / "catalog"
    _provision_catalog(catalog_root, "strategic_brand", "strategic_product")
    (catalog_root / "strategic_product" / "strategic_product.parquet").unlink()
    monkeypatch.setenv(activation.CATALOG_ROOT_ENV, str(catalog_root))

    assert activation.production_catalog_root_from_env() == catalog_root.resolve()


def test_shadow_activation_isolated_without_production_approval(monkeypatch) -> None:
    monkeypatch.delenv(activation.ENV_PROMOTION_APPROVED, raising=False)
    monkeypatch.setenv("INGEST_LOAD_SHADOW_ROOT", "/market-output/shadow")
    monkeypatch.setenv(activation.ENV_SOURCE_DB, "jw_mart_d2_stage_20260630_r2")
    monkeypatch.setenv(activation.ENV_SHADOW_TARGET_DB, "jw_mart_ingest_shadow_demo")
    monkeypatch.setenv(activation.ENV_SHADOW_BUILD_PREFIX, "jw_mart_ingest_shadow_build")

    result = activation.shadow_from_env(run_id="run-1")

    assert result.source_db == "jw_mart_d2_stage_20260630_r2"
    assert result.target_db == "jw_mart_ingest_shadow_demo"
    assert result.build_db.startswith("jw_mart_ingest_shadow_build_")


def test_shadow_activation_uses_configured_mart_database_when_source_is_implicit(
    monkeypatch,
) -> None:
    monkeypatch.delenv(activation.ENV_PROMOTION_APPROVED, raising=False)
    monkeypatch.delenv(activation.ENV_SOURCE_DB, raising=False)
    monkeypatch.setenv("MARIADB_DATABASE", "jw_mart_d2_stage_20260630_r2")
    monkeypatch.setenv("INGEST_LOAD_SHADOW_ROOT", "/market-output/shadow")
    monkeypatch.setenv(activation.ENV_SHADOW_TARGET_DB, "jw_mart_ingest_shadow_demo")
    monkeypatch.setenv(activation.ENV_SHADOW_BUILD_PREFIX, "jw_mart_ingest_shadow_build")

    result = activation.shadow_from_env(run_id="run-1")

    assert result.source_db == "jw_mart_d2_stage_20260630_r2"


def test_shadow_post_gate_row_count_failure_is_shadow_only(monkeypatch) -> None:
    monkeypatch.setenv(
        activation.ENV_SHADOW_FAILURE_AT,
        activation.SHADOW_FAILURE_POST_GATE_ROW_COUNT,
    )
    monkeypatch.delenv("INGEST_LOAD_SHADOW_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="shadow mode only"):
        activation.shadow_post_gate_actual_rows(9)

    monkeypatch.setenv("INGEST_LOAD_SHADOW_ROOT", "/market-output/shadow")

    assert activation.shadow_post_gate_actual_rows(9) == 10


def test_shadow_sigma_failure_injection_mutates_only_isolated_build(monkeypatch) -> None:
    monkeypatch.setenv("INGEST_LOAD_SHADOW_ROOT", "/market-output/shadow")
    monkeypatch.setenv(
        activation.ENV_SHADOW_FAILURE_AT,
        activation.SHADOW_FAILURE_SIGMA_PARTS_WHOLE,
    )
    statements: list[tuple[str, tuple[object, ...] | None]] = []

    class Cursor:
        def execute(self, sql, params=None):
            statements.append((sql, params))

        def fetchone(self):
            sql = statements[-1][0]
            if sql == "SELECT DATABASE()":
                return ("jw_mart_ingest_shadow_build_run1",)
            return (
                "LIVALO",
                "C10C0",
                json.dumps({"2026-05": {"raw_value": 100.0}}),
            )

        def close(self):
            return None

    class Connection:
        committed = False

        def cursor(self):
            return Cursor()

        def commit(self):
            self.committed = True

    conn = Connection()
    evidence = activation.maybe_inject_shadow_sigma_mismatch(
        conn,
        source="ubist",
        periods=("2026-05",),
    )

    assert evidence == {
        "atc4_code": "C10C0",
        "brand_key": "LIVALO",
        "period": "2026-05",
    }
    assert conn.committed is True
    update_sql, update_params = statements[-1]
    assert update_sql.startswith("UPDATE mart_general_brand_metric SET metric_history=%s")
    assert update_params is not None
    mutated = json.loads(str(update_params[0]))
    assert mutated["2026-05"]["raw_value"] > 100.0


def test_shadow_sigma_failure_injection_refuses_serving_database(monkeypatch) -> None:
    monkeypatch.setenv("INGEST_LOAD_SHADOW_ROOT", "/market-output/shadow")
    monkeypatch.setenv(
        activation.ENV_SHADOW_FAILURE_AT,
        activation.SHADOW_FAILURE_SIGMA_PARTS_WHOLE,
    )

    class Cursor:
        def execute(self, _sql, _params=None):
            return None

        def fetchone(self):
            return ("jw_mart",)

        def close(self):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

    with pytest.raises(RuntimeError, match="refused non-isolated database"):
        activation.maybe_inject_shadow_sigma_mismatch(
            Connection(), source="ubist", periods=("2026-05",)
        )


@pytest.mark.parametrize("target", ["jw_mart", "jw_mart_d2_stage_20260630_r2", "not_shadow"])
def test_shadow_activation_rejects_serving_or_unscoped_target(monkeypatch, target) -> None:
    monkeypatch.setenv("INGEST_LOAD_SHADOW_ROOT", "/market-output/shadow")
    monkeypatch.setenv(activation.ENV_SOURCE_DB, "jw_mart_d2_stage_20260630_r2")
    monkeypatch.setenv(activation.ENV_SHADOW_TARGET_DB, target)

    with pytest.raises(RuntimeError, match="isolated shadow"):
        activation.shadow_from_env(run_id="run-1")


def test_shadow_crash_injection_is_explicit_and_shadow_only(monkeypatch) -> None:
    monkeypatch.setenv(activation.ENV_SHADOW_CRASH_AT, "after_mart_publish")
    monkeypatch.delenv("INGEST_LOAD_SHADOW_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="shadow mode only"):
        activation.maybe_inject_shadow_crash("after_mart_publish")
    monkeypatch.setenv("INGEST_LOAD_SHADOW_ROOT", "/market-output/shadow")
    activation.maybe_inject_shadow_crash("after_corpus_publish")
    with pytest.raises(RuntimeError, match="deterministic shadow crash"):
        activation.maybe_inject_shadow_crash("after_mart_publish")


def test_shadow_target_bootstrap_copies_only_general_tables(monkeypatch) -> None:
    statements: list[str] = []
    source_rows = {
        activation.GENERAL_TABLES[0]: 250,
        activation.GENERAL_TABLES[1]: 20,
    }
    copied_rows = {table: 0 for table in activation.GENERAL_TABLES}
    result = None

    class Cursor:
        def execute(self, sql, params=None):
            nonlocal result
            statements.append(sql)
            table = next(
                (name for name in activation.GENERAL_TABLES if name in sql), None
            )
            if sql.startswith("SELECT COUNT(*), COALESCE(MAX(`id`), 0) FROM"):
                result = (source_rows[table], source_rows[table])
                return 1
            if sql.startswith("INSERT INTO"):
                last_id, source_max = params
                inserted = min(
                    activation.SHADOW_BASELINE_COPY_BATCH_SIZE,
                    source_max - last_id,
                )
                copied_rows[table] += inserted
                result = None
                return inserted
            if sql.startswith("SELECT COALESCE(MAX(`id`), 0) FROM"):
                result = (copied_rows[table],)
                return 1
            if sql.startswith("SELECT COUNT(*) FROM"):
                result = (copied_rows[table],)
                return 1
            result = None
            return 0

        def fetchone(self):
            return result

        def close(self):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            statements.append("COMMIT")

    config = activation.MartActivation(
        "jw_mart_d2_stage_20260630_r2",
        "jw_mart_ingest_shadow_demo",
        "jw_mart_ingest_shadow_build_run1",
    )
    monkeypatch.setattr(
        activation,
        "table_exists",
        lambda _conn, db, _table: db == config.source_db,
    )
    activation.ensure_shadow_target_baseline(Connection(), config)

    assert statements[0] == "CREATE DATABASE IF NOT EXISTS `jw_mart_ingest_shadow_demo`"
    assert statements[-1] == "COMMIT"
    for table in activation.GENERAL_TABLES:
        scratch = f"{table}__shadow_seed"
        assert any(
            f"CREATE TABLE `jw_mart_ingest_shadow_demo`.`{scratch}` LIKE "
            f"`jw_mart_d2_stage_20260630_r2`.`{table}`" == sql
            for sql in statements
        )
        assert any(
            f"INSERT INTO `jw_mart_ingest_shadow_demo`.`{scratch}` SELECT * FROM "
            f"`jw_mart_d2_stage_20260630_r2`.`{table}` WHERE `id` > %s "
            f"AND `id` <= %s ORDER BY `id` LIMIT " in sql
            for sql in statements
        )
    assert not any(
        sql.endswith(f"SELECT * FROM `jw_mart_d2_stage_20260630_r2`.`{table}`")
        for table in activation.GENERAL_TABLES
        for sql in statements
    )
    assert statements.count("COMMIT") >= 4
    assert any(sql.startswith("RENAME TABLE ") for sql in statements)


def test_shadow_target_bootstrap_rejects_partial_state(monkeypatch) -> None:
    class Cursor:
        def execute(self, _sql, _params=None):
            return None

        def close(self):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

    config = activation.MartActivation(
        "jw_mart_d2_stage_20260630_r2",
        "jw_mart_ingest_shadow_demo",
        "jw_mart_ingest_shadow_build_run1",
    )
    monkeypatch.setattr(
        activation,
        "table_exists",
        lambda _conn, db, table: db == config.target_db
        and table == activation.GENERAL_TABLES[0],
    )

    with pytest.raises(RuntimeError, match="partially initialized"):
        activation.ensure_shadow_target_baseline(Connection(), config)


@pytest.mark.parametrize("target", ["jw_mart", "jw_mart_d2_stage_20260630_r2"])
def test_shadow_target_bootstrap_refuses_serving_schema(target) -> None:
    config = activation.MartActivation(
        "jw_mart_d2_stage_20260630_r2",
        target,
        "jw_mart_ingest_shadow_build_run1",
    )

    with pytest.raises(RuntimeError, match="isolated shadow"):
        activation.ensure_shadow_target_baseline(object(), config)


def test_build_shadow_restores_s4_mutated_environment(monkeypatch) -> None:
    target = activation.MartActivation("jw_mart", "jw_mart", "jw_mart_ingest_run1")
    monkeypatch.setenv("MARIADB_DATABASE", "jw_mart")
    monkeypatch.delenv("S4_UBIST_DIR", raising=False)

    def mutate(**_kwargs):
        monkeypatch.setenv("MARIADB_DATABASE", "jw_mart_ingest_run1")
        monkeypatch.setenv("S4_UBIST_DIR", "/candidate")

    monkeypatch.setattr(activation, "run_s4_general", mutate)
    activation.build_shadow(
        target,
        catalog_root=Path("/market-output/shadow/catalog"),
        ubist_dir=Path("/candidate"),
    )

    assert activation.os.environ["MARIADB_DATABASE"] == "jw_mart"
    assert "S4_UBIST_DIR" not in activation.os.environ


def test_publish_shadow_checks_post_gate_and_limits_general_tables(monkeypatch) -> None:
    calls: list[object] = []
    target = activation.MartActivation("jw_mart", "jw_mart", "jw_mart_ingest_run1")
    monkeypatch.setattr(activation, "require_completed_post_gate", lambda *_args, **_kwargs: calls.append("gate"))
    monkeypatch.setattr(
        activation,
        "publish_table_group_atomically",
        lambda *_args, **kwargs: calls.append(kwargs) or (),
    )
    monkeypatch.setattr(activation, "record_mysql_component", lambda *_args, **_kwargs: calls.append("record"))

    activation.publish_shadow(
        object(), target, run_id="run1", epoch="2026-07", ingest_run_id="ingest-run1"
    )

    assert calls[0] == "gate"
    assert calls[1]["tables"] == activation.GENERAL_TABLES
    assert calls[2] == "record"


@pytest.mark.parametrize("row", [None, ("failed", "sigma mismatch"), ("running", None)])
def test_post_gate_requirement_rejects_absent_or_incomplete_evidence(row) -> None:
    class Cursor:
        def execute(self, sql, params):
            assert "stage='post_gate'" in sql
            assert params == ("ingest-run1",)

        def fetchone(self):
            return row

        def close(self):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

    with pytest.raises(RuntimeError, match="promotion blocked"):
        activation.require_completed_post_gate(Connection(), ingest_run_id="ingest-run1")


def test_post_gate_requirement_accepts_only_complete_evidence() -> None:
    class Cursor:
        def execute(self, _sql, _params):
            return None

        def fetchone(self):
            return {"status": "complete", "reason": None}

        def close(self):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

    activation.require_completed_post_gate(Connection(), ingest_run_id="ingest-run1")


def test_candidate_corpus_promotes_by_rename_and_can_rollback(tmp_path) -> None:
    live = tmp_path / "ubist"
    live.mkdir()
    (live / "old.txt").write_text("old", encoding="utf-8")

    corpus = activation.prepare_candidate_corpus(live, run_id="run1")
    (corpus.candidate_root / "new.txt").write_text("new", encoding="utf-8")
    activation.promote_candidate_corpus(corpus)

    assert (live / "new.txt").is_file()
    assert (corpus.backup_root / "old.txt").is_file()

    activation.rollback_candidate_corpus(corpus)

    assert (live / "old.txt").is_file()
    assert not (live / "new.txt").exists()


def test_publish_shadow_rolls_back_when_ledger_record_fails(monkeypatch) -> None:
    action = type(
        "Action",
        (),
        {
            "table": "mart_general_brand_metric",
            "backup_table": "mart_general_brand_metric__old_run1",
        },
    )()
    target = activation.MartActivation("jw_mart", "jw_mart", "jw_mart_ingest_run1")
    restored: list[tuple[object, ...]] = []
    monkeypatch.setattr(activation, "require_completed_post_gate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        activation, "publish_table_group_atomically", lambda *_args, **_kwargs: (action,)
    )
    monkeypatch.setattr(
        activation,
        "record_mysql_component",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger failed")),
    )
    monkeypatch.setattr(
        activation,
        "restore_table_group_atomically",
        lambda *_args, **kwargs: restored.append(kwargs["actions"]),
    )

    with pytest.raises(RuntimeError, match="ledger failed"):
        activation.publish_shadow(
            object(), target, run_id="run1", epoch="2026-07", ingest_run_id="ingest-run1"
        )

    assert restored == [(action,)]


def test_activation_journal_recovers_corpus_and_atomic_mart_group(tmp_path, monkeypatch) -> None:
    live = tmp_path / "ubist"
    live.mkdir()
    (live / "old.txt").write_text("old", encoding="utf-8")
    corpus = activation.prepare_candidate_corpus(live, run_id="run1")
    (corpus.candidate_root / "new.txt").write_text("new", encoding="utf-8")
    target = activation.MartActivation("jw_mart", "jw_mart", "jw_mart_ingest_run1")
    journal = activation.write_activation_journal(
        corpus,
        target,
        run_id="run1",
        phase="prepared",
        identity=("2026-07", "ubist", "a" * 64),
    )
    activation.promote_candidate_corpus(corpus)
    activation.update_activation_journal(journal, "corpus_promoted")
    restored: list[tuple[str, ...]] = []
    monkeypatch.setattr(activation, "table_exists", lambda *_args: True)
    monkeypatch.setattr(
        activation,
        "restore_table_group_atomically",
        lambda _conn, *, target_db, actions, run_id: restored.append(
            (target_db, run_id, *(action.table for action in actions))
        ),
    )

    recovered = activation.recover_incomplete_activations(object(), output_root=tmp_path)

    assert recovered == (journal,)
    assert (live / "old.txt").read_text(encoding="utf-8") == "old"
    assert restored == [
        (
            "jw_mart",
            "recovery_run1",
            "mart_general_brand_metric",
            "mart_general_market_metric",
        )
    ]
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "rollback_needs_refresh"


def test_activation_journal_rejects_partial_mart_backup(tmp_path, monkeypatch) -> None:
    live = tmp_path / "ubist"
    live.mkdir()
    (live / "_manifest.json").write_text("{}", encoding="utf-8")
    corpus = activation.prepare_candidate_corpus(live, run_id="run1")
    target = activation.MartActivation("jw_mart", "jw_mart", "jw_mart_ingest_run1")
    activation.write_activation_journal(
        corpus,
        target,
        run_id="run1",
        phase="prepared",
        identity=("2026-07", "ubist", "a" * 64),
    )
    exists = iter((True, False))
    monkeypatch.setattr(activation, "table_exists", lambda *_args: next(exists))

    with pytest.raises(RuntimeError, match="ambiguous partial mart backup"):
        activation.recover_incomplete_activations(object(), output_root=tmp_path)


def test_shadow_recovery_rejects_journal_targeting_serving_db(tmp_path) -> None:
    live = tmp_path / "ubist"
    candidate = tmp_path / ".ubist_candidate_run1"
    backup = tmp_path / ".ubist_backup_run1"
    for path in (live, candidate, backup):
        path.mkdir()
    journal = tmp_path / ".ubist_activation_run1.json"
    journal.write_text(
        json.dumps({
            "version": 2,
            "run_id": "run1",
            "phase": "prepared",
            "epoch": "2026-05",
            "category": "ubist",
            "manifest_sha": "f" * 64,
            "source_db": "jw_mart_d2_stage_20260630_r2",
            "target_db": "jw_mart_d2_stage_20260630_r2",
            "build_db": "jw_mart_ingest_shadow_build_run1",
            "live_root": str(live),
            "candidate_root": str(candidate),
            "backup_root": str(backup),
            "tables": list(activation.GENERAL_TABLES),
        }),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="escapes required prefix"):
        activation.recover_incomplete_activations(
            object(),
            output_root=tmp_path,
            required_target_prefix=activation.SHADOW_DB_PREFIX,
        )


def test_recovery_keeps_promoted_state_when_ledger_is_already_complete(
    tmp_path, monkeypatch
) -> None:
    live = tmp_path / "ubist"
    live.mkdir()
    (live / "old.txt").write_text("old", encoding="utf-8")
    corpus = activation.prepare_candidate_corpus(live, run_id="run1")
    (corpus.candidate_root / "new.txt").write_text("new", encoding="utf-8")
    target = activation.MartActivation("jw_mart", "jw_mart", "jw_mart_ingest_run1")
    journal = activation.write_activation_journal(
        corpus,
        target,
        run_id="run1",
        phase="refresh_succeeded",
        identity=("2026-07", "ubist", "a" * 64),
    )
    activation.promote_candidate_corpus(corpus)
    restored: list[str] = []
    monkeypatch.setattr(
        activation,
        "restore_table_group_atomically",
        lambda *_args, **_kwargs: restored.append("mart"),
    )

    recovered = activation.recover_incomplete_activations(
        object(),
        output_root=tmp_path,
        ledger_status=lambda *_identity: "complete",
    )

    assert recovered == ()
    assert restored == []
    assert (live / "new.txt").read_text(encoding="utf-8") == "new"
    assert corpus.backup_root.exists()
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "complete"


def test_recovery_resumes_after_crash_following_atomic_mart_restore(
    tmp_path, monkeypatch
) -> None:
    live = tmp_path / "ubist"
    live.mkdir()
    (live / "old.txt").write_text("old", encoding="utf-8")
    corpus = activation.prepare_candidate_corpus(live, run_id="run1")
    (corpus.candidate_root / "new.txt").write_text("new", encoding="utf-8")
    target = activation.MartActivation("jw_mart", "jw_mart", "jw_mart_ingest_run1")
    journal = activation.write_activation_journal(
        corpus,
        target,
        run_id="run1",
        phase="refresh_started",
        identity=("2026-07", "ubist", "a" * 64),
    )
    activation.promote_candidate_corpus(corpus)
    existing = {
        *(f"{table}__old_run1" for table in activation.GENERAL_TABLES),
        *activation.GENERAL_TABLES,
    }
    restore_calls: list[str] = []

    def table_exists(_conn, _db, table):
        return table in existing

    def restore(_conn, *, target_db, actions, run_id):
        restore_calls.append(run_id)
        for action in actions:
            existing.remove(str(action.backup_table))
            existing.add(f"{action.table}__failed_{run_id}")

    original_update = activation.update_activation_journal
    crash_once = True

    def update(path, phase):
        nonlocal crash_once
        if phase == "recovery_mart_complete" and crash_once:
            crash_once = False
            raise RuntimeError("injected crash after mart restore")
        original_update(path, phase)

    monkeypatch.setattr(activation, "table_exists", table_exists)
    monkeypatch.setattr(activation, "restore_table_group_atomically", restore)
    monkeypatch.setattr(activation, "update_activation_journal", update)

    with pytest.raises(RuntimeError, match="injected crash"):
        activation.recover_incomplete_activations(object(), output_root=tmp_path)
    recovered = activation.recover_incomplete_activations(object(), output_root=tmp_path)

    assert recovered == (journal,)
    assert restore_calls == ["recovery_run1"]
    assert (live / "old.txt").read_text(encoding="utf-8") == "old"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "rollback_needs_refresh"


def test_recovery_resumes_after_crash_following_corpus_rollback(
    tmp_path, monkeypatch
) -> None:
    live = tmp_path / "ubist"
    live.mkdir()
    (live / "old.txt").write_text("old", encoding="utf-8")
    corpus = activation.prepare_candidate_corpus(live, run_id="run1")
    (corpus.candidate_root / "new.txt").write_text("new", encoding="utf-8")
    target = activation.MartActivation("jw_mart", "jw_mart", "jw_mart_ingest_run1")
    journal = activation.write_activation_journal(
        corpus,
        target,
        run_id="run1",
        phase="corpus_promoted",
        identity=("2026-07", "ubist", "a" * 64),
    )
    activation.promote_candidate_corpus(corpus)
    monkeypatch.setattr(activation, "table_exists", lambda *_args: False)
    original_update = activation.update_activation_journal
    crash_once = True

    def update(path, phase):
        nonlocal crash_once
        if phase == "rollback_needs_refresh" and crash_once:
            crash_once = False
            raise RuntimeError("injected crash after corpus rollback")
        original_update(path, phase)

    monkeypatch.setattr(activation, "update_activation_journal", update)

    with pytest.raises(RuntimeError, match="injected crash"):
        activation.recover_incomplete_activations(object(), output_root=tmp_path)
    recovered = activation.recover_incomplete_activations(object(), output_root=tmp_path)

    assert recovered == (journal,)
    assert (live / "old.txt").read_text(encoding="utf-8") == "old"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "rollback_needs_refresh"
