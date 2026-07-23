from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook import ubist_mart_activation as activation


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


def test_build_shadow_uses_candidate_ubist_root(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    target = activation.MartActivation("jw_mart", "jw_mart", "jw_mart_ingest_run1")
    monkeypatch.setattr(
        activation,
        "run_s4_general",
        lambda **kwargs: calls.append(kwargs),
    )

    activation.build_shadow(target, ubist_dir=Path("/market-output/.ubist-candidate-run1"))

    assert calls == [{
        "build_db": "jw_mart_ingest_run1",
        "source_db": "jw_mart",
        "catalog_root": None,
        "ubist_dir": Path("/market-output/.ubist-candidate-run1"),
        "input_mode": "raw",
    }]


def test_build_shadow_restores_s4_mutated_environment(monkeypatch) -> None:
    target = activation.MartActivation("jw_mart", "jw_mart", "jw_mart_ingest_run1")
    monkeypatch.setenv("MARIADB_DATABASE", "jw_mart")
    monkeypatch.delenv("S4_UBIST_DIR", raising=False)

    def mutate(**_kwargs):
        monkeypatch.setenv("MARIADB_DATABASE", "jw_mart_ingest_run1")
        monkeypatch.setenv("S4_UBIST_DIR", "/candidate")

    monkeypatch.setattr(activation, "run_s4_general", mutate)
    activation.build_shadow(target, ubist_dir=Path("/candidate"))

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
