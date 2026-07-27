from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook.test_run_runner import (
    IsolationContractError,
    _prepare_shadow_catalog,
    _writable_column_names,
    read_pipeline_observation,
    run_test_load,
    validate_isolation_env,
)
from pipeline.scripts.ingest_hook.ledger import Ledger
from pipeline.scripts.ingest_hook.test_runs import TestRunStore


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "INGEST_TEST_RUN_ID": "11111111-1111-4111-8111-111111111111",
        "INGEST_TEST_RUN_ROOT": str(tmp_path / "results"),
        "INGEST_TEST_SOURCE_DB_HOST": "reader.internal",
        "INGEST_TEST_SOURCE_DB_PORT": "3306",
        "INGEST_TEST_SOURCE_DB_NAME": "serving",
        "INGEST_TEST_SOURCE_DB_USER": "reader",
        "INGEST_TEST_SOURCE_DB_PASSWORD": "redacted",
        "MARIADB_HOST": "127.0.0.1",
        "MARIADB_PORT": "3306",
        "MARIADB_DATABASE": "jw_mart_test_11111111111141118111",
        "MARIADB_SOURCE_DATABASE": "jw_mart_test_11111111111141118111",
        "MARIADB_PASSWORD": "test-11111111111141118111",
        "MARIADB_ROOT_PASSWORD": "test-11111111111141118111",
        "INGEST_SHADOW_TARGET_DB": "jw_mart_ingest_shadow_11111111111141118111",
        "INGEST_SHADOW_BUILD_PREFIX": "jw_mart_ingest_shadow_build_11111111111141118111_",
        "INGEST_LOAD_SHADOW_ROOT": str(tmp_path / "work" / "market-output"),
        "INGEST_SHADOW_CATALOG_ROOT": str(
            tmp_path / "work" / "market-output" / "catalog"
        ),
        "INGEST_TEST_SOURCE_CATALOG_ROOT": str(tmp_path / "source" / "catalog"),
        "INGEST_LEDGER_SQLITE": str(tmp_path / "work" / "ledger.sqlite"),
    }


def _seed(store: TestRunStore) -> str:
    record = store.create(
        category="ubist",
        epoch="2026-07",
        manifest_sha="a" * 64,
        manifest_path="/input/manifest.json",
        requested_by="pl@example.test",
    )
    return record.run_id


def test_isolation_contract_rejects_operating_write_paths(tmp_path):
    env = _env(tmp_path)
    env["INGEST_LOAD_TARGET_ROOT"] = "/market-output/ubist"

    with pytest.raises(IsolationContractError, match="INGEST_LOAD_TARGET_ROOT"):
        validate_isolation_env(env)


def test_snapshot_column_projection_excludes_generated_columns():
    rows = [
        ("id", ""),
        ("amount", "DEFAULT_GENERATED"),
        ("amount_total", "VIRTUAL GENERATED"),
        ("loaded_at", "on update CURRENT_TIMESTAMP"),
    ]

    assert _writable_column_names(rows) == ("id", "loaded_at")


def test_shadow_catalog_is_copied_into_disposable_root(tmp_path):
    env = _env(tmp_path)
    source = Path(env["INGEST_TEST_SOURCE_CATALOG_ROOT"])
    required = source / "strategic_brand" / "strategic_brand.parquet"
    required.parent.mkdir(parents=True)
    required.write_bytes(b"catalog")

    copied = _prepare_shadow_catalog(env)

    target = Path(env["INGEST_SHADOW_CATALOG_ROOT"])
    assert (target / "strategic_brand" / "strategic_brand.parquet").read_bytes() == (
        b"catalog"
    )
    assert copied == {"files": 1}


def test_runner_records_result_and_always_signals_sidecar_shutdown(tmp_path):
    env = _env(tmp_path)
    store = TestRunStore(Path(env["INGEST_TEST_RUN_ROOT"]))
    run_id = _seed(store)
    env["INGEST_TEST_RUN_ID"] = run_id
    lifecycle = tmp_path / "lifecycle"

    result = run_test_load(
        manifest=Path("/input/manifest.json"),
        run_id=run_id,
        job_name="jw-ingest-test",
        environ=env,
        lifecycle_root=lifecycle,
        clone_snapshot=lambda _env: {"tables": 12, "rows": 345},
        prepare_catalog=lambda _env: {"files": 7},
        execute_pipeline=lambda **_kwargs: 0,
        build_census=lambda _env: {
            "changed_markets": 2,
            "member_changes": {"added_only": 1, "removed_only": 0, "mixed": 0},
        },
        read_observation=lambda _path, _run_id: {
            "stages": [{"stage": "load", "duration_ms": 1000}],
            "row_counts": {"ubist.csv": 345},
        },
    )

    assert result == 0
    record = store.get(run_id)
    assert record is not None
    assert record.status == "completed"
    assert record.result["snapshot"]["rows"] == 345
    assert record.result["snapshot"]["catalog_files"] == 7
    assert record.result["row_counts"] == {"ubist.csv": 345}
    assert record.result["census"]["changed_markets"] == 2
    assert (lifecycle / "done").read_text(encoding="utf-8") == "completed\n"


def test_pipeline_observation_attaches_manifest_rows_to_load_stage(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(sqlite3.connect(ledger_path), dialect="sqlite")
    ledger.ensure_table()
    ledger.receive(
        "2026-07",
        "ubist",
        "a" * 64,
        manifest_path="/input/manifest.json",
        uploaded_by="pl@example.test",
    )
    ledger.mark_running(
        "2026-07",
        "ubist",
        "a" * 64,
        job_name="job",
        run_id="run-1",
    )
    ledger.record_stage(
        "2026-07",
        "ubist",
        "a" * 64,
        run_id="run-1",
        seq=1,
        stage="load",
        status="complete",
        started_at="2026-07-27T00:00:00+00:00",
        finished_at="2026-07-27T00:00:01+00:00",
        duration_ms=1000,
    )
    ledger.mark_complete(
        "2026-07",
        "ubist",
        "a" * 64,
        row_counts={"a.csv": 10, "b.csv": 5},
    )

    observation = read_pipeline_observation(ledger_path, "run-1")

    assert observation["row_counts"] == {"a.csv": 10, "b.csv": 5}
    assert observation["stages"][0]["rows"] == 15


def test_runner_failure_is_durable_and_still_cleans_sidecar(tmp_path):
    env = _env(tmp_path)
    store = TestRunStore(Path(env["INGEST_TEST_RUN_ROOT"]))
    run_id = _seed(store)
    env["INGEST_TEST_RUN_ID"] = run_id
    lifecycle = tmp_path / "lifecycle"

    result = run_test_load(
        manifest=Path("/input/manifest.json"),
        run_id=run_id,
        job_name="jw-ingest-test",
        environ=env,
        lifecycle_root=lifecycle,
        clone_snapshot=lambda _env: (_ for _ in ()).throw(RuntimeError("boom")),
        prepare_catalog=lambda _env: {"files": 7},
        execute_pipeline=lambda **_kwargs: 0,
        build_census=lambda _env: {},
        read_observation=lambda _path, _run_id: {},
    )

    assert result == 1
    record = store.get(run_id)
    assert record is not None
    assert record.status == "failed"
    assert "boom" in record.reason
    assert (lifecycle / "done").read_text(encoding="utf-8") == "failed\n"


def test_runner_records_validation_failure_from_real_pipeline_boundary(tmp_path):
    env = _env(tmp_path)
    store = TestRunStore(Path(env["INGEST_TEST_RUN_ROOT"]))
    run_id = _seed(store)
    lifecycle = tmp_path / "lifecycle"

    result = run_test_load(
        manifest=Path("/input/invalid-manifest.json"),
        run_id=run_id,
        job_name="jw-ingest-test",
        environ=env,
        lifecycle_root=lifecycle,
        clone_snapshot=lambda _env: {"tables": 12, "rows": 345},
        prepare_catalog=lambda _env: {"files": 7},
        execute_pipeline=lambda **_kwargs: 2,
        build_census=lambda _env: {},
        read_observation=lambda _path, _run_id: {},
    )

    record = store.get(run_id)
    assert result == 1
    assert record is not None
    assert record.status == "failed"
    assert record.reason == "RuntimeError: full ingest pipeline exited with 2"
    assert (lifecycle / "done").read_text(encoding="utf-8") == "failed\n"


def test_pipeline_observation_reports_stage_timings_and_loaded_rows(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    connection = sqlite3.connect(ledger_path)
    ledger = Ledger(connection)
    ledger.ensure_table()
    identity = ("2026-07", "ubist", "a" * 64)
    ledger.receive(
        *identity,
        manifest_path="/input/manifest.json",
        uploaded_by="pl@example.test",
    )
    ledger.mark_running(*identity, job_name="jw-ingest-test", run_id="run-1")
    ledger.record_stage(
        *identity,
        run_id="run-1",
        seq=1,
        stage="g3",
        status="complete",
        started_at="2026-07-27 01:00:00",
        finished_at="2026-07-27 01:00:02",
        duration_ms=2000,
    )
    ledger.record_stage(
        *identity,
        run_id="run-1",
        seq=2,
        stage="load",
        status="complete",
        started_at="2026-07-27 01:00:02",
        finished_at="2026-07-27 01:00:07",
        duration_ms=5000,
    )
    ledger.mark_complete(*identity, row_counts={"ubist.csv": 321})
    connection.close()

    observation = read_pipeline_observation(ledger_path, "run-1")

    assert observation["row_counts"] == {"ubist.csv": 321}
    assert observation["stages"] == [
        {
            "seq": 1,
            "stage": "g3",
            "status": "complete",
            "reason": None,
            "started_at": "2026-07-27 01:00:00",
            "finished_at": "2026-07-27 01:00:02",
            "duration_ms": 2000,
        },
        {
            "seq": 2,
            "stage": "load",
            "status": "complete",
            "reason": None,
            "started_at": "2026-07-27 01:00:02",
            "finished_at": "2026-07-27 01:00:07",
            "duration_ms": 5000,
            "rows": 321,
        },
    ]
