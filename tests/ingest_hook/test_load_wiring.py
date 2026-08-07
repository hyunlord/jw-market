"""J5 — upload -> loader wiring and the M-2 silent-failure gate.

The loader subprocess (pipeline.etl.run) needs pyarrow/openpyxl and is exercised
for real in the cluster gate; here _run_commands is stubbed so the wiring
(argv building, target routing, fail-closed) and the M-2 gate (verify_epoch_loaded)
are tested deterministically with zero external deps.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pymysql

from pipeline.scripts.ingest_hook import config, job_runner
from pipeline.scripts.ingest_hook import ubist_mart_activation
from pipeline.scripts.ingest_hook.source_inventory import (
    FileObservation,
    PeriodGate,
    PeriodGateResult,
    ScanOutcome,
    ScanSnapshot,
)
from pipeline.scripts.ingest_hook.category_map import resolve_category
from pipeline.scripts.ingest_hook.contract import load_manifest
from pipeline.scripts.ingest_hook.load_verify import (
    LoadVerifyError,
    verify_epoch_loaded,
    verify_table_load,
)
from ingest_fixtures import write_submission

UBIST = resolve_category("ubist")


def test_open_mart_connection_uses_mapping_cursor(monkeypatch):
    captured: dict[str, object] = {}

    def connect(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(pymysql, "connect", connect)
    monkeypatch.setenv("MARIADB_HOST", "db")
    monkeypatch.setenv("MARIADB_PORT", "3306")
    monkeypatch.setenv("MARIADB_USER", "writer")
    monkeypatch.setenv("MARIADB_PASSWORD", "secret")
    monkeypatch.setenv("MARIADB_DATABASE", "jw_mart_source")

    config.open_mart_connection("jw_mart_shadow")

    assert captured["database"] == "jw_mart_shadow"
    assert captured["cursorclass"] is pymysql.cursors.DictCursor


def _write_load_manifest(target_dir: Path, epoch: str, rows: int) -> None:
    """Simulate what the real UBIST loader writes to its target dir."""
    year, month = epoch.split("-")
    part = target_dir / f"year={year}" / f"month={month}" / "data.parquet"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"PAR1")  # placeholder parquet bytes; M-2 only checks existence
    (target_dir / "_manifest.json").write_text(
        json.dumps({"schema_version": "1.0", "partitions": [
            {"period_yyyymm": epoch, "path": f"year={year}/month={month}/data.parquet", "row_count": rows}
        ]}),
        encoding="utf-8",
    )


def _write_table_manifest(
    target_dir: Path, epoch: str, *, before: int, after: int, loaded: int
) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "_manifest.json").write_text(
        json.dumps({
            "schema_version": "ingest-table-load-v1",
            "epoch": epoch,
            "primary": {
                "schema": "jw_ingest_stage_test",
                "table": "iqvia_nsa_quarterly_raw",
                "kind": "append",
                "rows_before": before,
                "rows_after": after,
                "rows_loaded": loaded,
                "source_rows": loaded,
                "difference_reasons": [],
            },
        }),
        encoding="utf-8",
    )


# ─── M-2 gate unit tests ─────────────────────────────────────────────
def test_verify_passes_when_epoch_present(tmp_path):
    _write_load_manifest(tmp_path, "2026-03", 42)
    assert verify_epoch_loaded("ubist_parquet_manifest", tmp_path, "2026-03") == 42


def test_verify_fails_when_no_manifest(tmp_path):
    with pytest.raises(LoadVerifyError, match="no manifest"):
        verify_epoch_loaded("ubist_parquet_manifest", tmp_path, "2026-03")


def test_verify_fails_when_epoch_absent(tmp_path):
    _write_load_manifest(tmp_path, "2026-02", 10)
    with pytest.raises(LoadVerifyError, match="absent from load output"):
        verify_epoch_loaded("ubist_parquet_manifest", tmp_path, "2026-03")


def test_verify_fails_on_zero_rows(tmp_path):
    (tmp_path).mkdir(exist_ok=True)
    (tmp_path / "_manifest.json").write_text(
        json.dumps({"partitions": [{"period_yyyymm": "2026-03", "row_count": 0}]}), encoding="utf-8"
    )
    with pytest.raises(LoadVerifyError, match="<= 0"):
        verify_epoch_loaded("ubist_parquet_manifest", tmp_path, "2026-03")


def test_verify_fails_when_parquet_missing(tmp_path):
    (tmp_path / "_manifest.json").write_text(
        json.dumps({"partitions": [{"period_yyyymm": "2026-03", "path": "year=2026/month=03/data.parquet", "row_count": 5}]}),
        encoding="utf-8",
    )
    with pytest.raises(LoadVerifyError, match="parquet is missing"):
        verify_epoch_loaded("ubist_parquet_manifest", tmp_path, "2026-03")


def test_table_manifest_returns_verified_loader_counts(tmp_path):
    (tmp_path / "_manifest.json").write_text(
        json.dumps({
            "schema_version": "ingest-table-load-v1",
            "epoch": "2026-03",
            "primary": {
                "schema": "jw_ingest_stage_test",
                "table": "raw_events",
                "kind": "append",
                "rows_before": 10,
                "rows_after": 13,
                "rows_loaded": 3,
                "source_rows": 3,
                "difference_reasons": [],
            },
        }),
        encoding="utf-8",
    )

    evidence = verify_table_load(tmp_path, "2026-03")

    assert evidence.rows_before == 10
    assert evidence.rows_after == 13
    assert evidence.rows_loaded == 3


def test_table_manifest_rejects_claimed_load_without_growth(tmp_path):
    (tmp_path / "_manifest.json").write_text(
        json.dumps({
            "schema_version": "ingest-table-load-v1",
            "epoch": "2026-03",
            "primary": {
                "schema": "jw_ingest_stage_test",
                "table": "raw_events",
                "kind": "append",
                "rows_before": 10,
                "rows_after": 10,
                "rows_loaded": 3,
                "source_rows": 3,
                "difference_reasons": [],
            },
        }),
        encoding="utf-8",
    )

    with pytest.raises(LoadVerifyError, match="did not grow"):
        verify_table_load(tmp_path, "2026-03")


# ─── _real_load wiring tests (loader stubbed) ────────────────────────
@pytest.fixture
def staging_env(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_LOAD_STAGING_ROOT, str(tmp_path / "staging"))
    monkeypatch.setenv(config.ENV_LOAD_STAGING_DB, "jw_ingest_stage_test")
    monkeypatch.delenv(config.ENV_LOAD_TARGET_ROOT, raising=False)
    return tmp_path


def _manifest(bucket, **kw):
    return load_manifest(write_submission(bucket, **kw))


def test_real_load_injects_file_and_target(staging_env, bucket, monkeypatch):
    manifest = _manifest(bucket, epoch="2026-03")
    seen = {}

    def fake_run(label, argv):
        seen["argv"] = argv
        # simulate the loader honoring --target-dir
        target = Path(argv[argv.index("--target-dir") + 1])
        _write_load_manifest(target, "2026-03", 7)

    monkeypatch.setattr(job_runner, "_run_commands", fake_run)
    result = job_runner._real_load(manifest, UBIST, bucket)

    assert "--file" in seen["argv"] and "--target-dir" in seen["argv"]
    assert seen["argv"][seen["argv"].index("--file") + 1].endswith("data.csv")
    assert result["epoch_rows"] == 7
    assert result["staging_verify"] is True


def test_real_load_uses_only_content_classified_full_scan_inputs(
    staging_env,
    bucket,
    monkeypatch,
    tmp_path,
):
    manifest = _manifest(bucket, epoch="2026-03")
    classified = (tmp_path / "all-a.xlsx", tmp_path / "all-b.xlsx")
    for path in classified:
        path.write_bytes(b"xlsx")
    observed: list[str] = []

    def fake_run(_label, argv):
        observed.append(argv[argv.index("--file") + 1])
        target = Path(argv[argv.index("--target-dir") + 1])
        _write_load_manifest(target, "2026-03", 7)

    monkeypatch.setattr(job_runner, "_run_commands", fake_run)

    job_runner._real_load(manifest, UBIST, bucket, source_files=classified)

    assert observed == [str(classified[0]), str(classified[1])]


def test_full_scan_load_publishes_snapshot_only_after_loader_succeeds(
    staging_env,
    bucket,
    monkeypatch,
    tmp_path,
):
    manifest = _manifest(bucket, epoch="2026-03")
    classified = tmp_path / "operating.xlsx"
    classified.write_bytes(b"xlsx")
    calls: list[str] = []

    class Policy:
        root = tmp_path

    def fake_run_full_scan(policy, **kwargs):
        calls.append("scan")
        result = kwargs["rebuild"]((classified,))
        calls.append("snapshot")
        return type("Outcome", (), {"rebuild_result": result})()

    monkeypatch.setattr(job_runner, "load_scan_policy", lambda category, required: Policy())
    monkeypatch.setattr(job_runner, "latest_successful_snapshot", lambda root, category: None)
    monkeypatch.setattr(job_runner, "run_full_scan", fake_run_full_scan)
    monkeypatch.setattr(
        job_runner,
        "_real_load",
        lambda *args, source_files=None, **kwargs: (
            calls.append(f"load:{source_files[0].name}")
            or {"epoch_rows": 1, "rows_before": 0, "rows_loaded": 1, "staging_verify": True}
        ),
    )

    result, outcome = job_runner._load_with_source_inventory(
        manifest,
        UBIST,
        bucket,
        run_id="scan-run",
        target_dir_override=None,
        required=True,
    )

    assert result["rows_loaded"] == 1
    assert outcome is not None
    assert calls == ["scan", "load:operating.xlsx", "snapshot"]


def test_automatic_publish_contract_carries_pg4_pg5_and_warnings(tmp_path):
    snapshot = ScanSnapshot(
        schema_version="1",
        category="ubist",
        epoch="2026-06",
        manifest_sha="a" * 64,
        run_id="scan-run",
        observed_at="2026-08-07T00:00:00+00:00",
        files=(FileObservation("source.xlsx", "b" * 64, 10, "classified", category="ubist"),),
    )
    gates = PeriodGateResult(
        pg4=PeriodGate("PG-4", "pass", (), "continuous"),
        pg5=PeriodGate("PG-5", "pass", (), "explained"),
        pg6=PeriodGate("PG-6", "warning", (), "value drift"),
        pg7=PeriodGate("PG-7", "warning", (), "newest drift"),
    )
    outcome = ScanOutcome(snapshot, tmp_path / "snapshot.json", None, gates, {})

    contract = job_runner._automatic_publish_contract(outcome)

    assert contract["hard_gates"] == {
        "PG-1": "pass",
        "PG-2": "pass",
        "PG-3": "pass",
        "PG-4": "pass",
        "PG-5": "pass",
    }
    assert contract["warnings"] == {"PG-6": "warning", "PG-7": "warning"}


def test_automatic_publish_request_uses_exact_identity_and_timeout(monkeypatch):
    observed: dict[str, object] = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def opener(request, data=None, timeout=0):
        observed["payload"] = json.loads(request.data)
        observed["data"] = data
        observed["timeout"] = timeout
        return Response()

    result = job_runner._request_automatic_publish(
        ("2026-06", "ubist", "c" * 64),
        run_id="build-run",
        endpoint="http://hook/ingest/publish/automatic",
        opener=opener,
    )

    assert result == 200
    assert observed == {
        "payload": {
            "epoch": "2026-06",
            "category": "ubist",
            "manifest_sha": "c" * 64,
            "run_id": "build-run",
        },
        "data": None,
        "timeout": 15,
    }


def test_real_load_keeps_manifest_isolation_in_staging(staging_env, bucket, monkeypatch):
    manifest = _manifest(bucket, epoch="2026-03")
    seen = {}

    def fake_run(_label, argv):
        target = Path(argv[argv.index("--target-dir") + 1])
        seen["target"] = target
        _write_load_manifest(target, "2026-03", 7)

    monkeypatch.setattr(job_runner, "_run_commands", fake_run)
    job_runner._real_load(manifest, UBIST, bucket)

    assert seen["target"] == staging_env / "staging" / "ubist" / "2026-03" / manifest.manifest_sha


def test_real_load_flattens_production_ubist_to_reader_root(tmp_path, bucket, monkeypatch):
    manifest = _manifest(bucket, epoch="2026-03")
    target_root = tmp_path / "market-output"
    monkeypatch.delenv(config.ENV_LOAD_STAGING_ROOT, raising=False)
    monkeypatch.setenv(config.ENV_LOAD_TARGET_ROOT, str(target_root))
    seen = {}

    def fake_run(_label, argv):
        seen["argv"] = argv
        target = Path(argv[argv.index("--target-dir") + 1])
        seen["target"] = target
        _write_load_manifest(target, "2026-03", 7)

    monkeypatch.setattr(job_runner, "_run_commands", fake_run)
    result = job_runner._real_load(manifest, UBIST, bucket)

    assert seen["target"] == target_root / "ubist"
    assert "--allow-overlap-dedup" not in seen["argv"]
    assert result["target_dir"] == target_root / "ubist"
    assert result["staging_verify"] is False


def test_real_load_uses_same_row_merge_contract_for_shadow(tmp_path, bucket, monkeypatch):
    manifest = _manifest(bucket, epoch="2026-03")
    monkeypatch.delenv(config.ENV_LOAD_STAGING_ROOT, raising=False)
    monkeypatch.delenv(config.ENV_LOAD_TARGET_ROOT, raising=False)
    monkeypatch.setenv(config.ENV_LOAD_SHADOW_ROOT, str(tmp_path / "shadow"))
    seen: dict[str, tuple[str, ...]] = {}

    def fake_run(_label, argv):
        seen["argv"] = argv
        target = Path(argv[argv.index("--target-dir") + 1])
        _write_load_manifest(target, "2026-03", 7)

    monkeypatch.setattr(job_runner, "_run_commands", fake_run)

    job_runner._real_load(
        manifest,
        UBIST,
        bucket,
        target_dir_override=tmp_path / "shadow-candidate",
    )

    assert "--allow-overlap-dedup" not in seen["argv"]


def test_real_load_silent_failure_is_caught(staging_env, bucket, monkeypatch):
    manifest = _manifest(bucket, epoch="2026-03")
    # loader runs but writes nothing to the target (the exact silent-failure shape)
    monkeypatch.setattr(job_runner, "_run_commands", lambda label, argv: None)
    with pytest.raises(LoadVerifyError, match="no manifest|absent"):
        job_runner._real_load(manifest, UBIST, bucket)


def test_real_load_iqvia_nsa_injects_file_target_and_epoch(staging_env, bucket, monkeypatch):
    manifest = _manifest(bucket, category="iqvia_nsa", epoch="2026-Q1",
                         rows=[("2026-Q1", "Class", "x", 1.0), ("2026-Q1", "전체", "-", 1.0)])
    seen = {}

    def fake_run(_label, argv):
        seen["argv"] = argv
        target = Path(argv[argv.index("--target-dir") + 1])
        _write_table_manifest(target, "2026-Q1", before=10, after=12, loaded=2)

    monkeypatch.setattr(job_runner, "_run_commands", fake_run)
    result = job_runner._real_load(manifest, resolve_category("iqvia_nsa"), bucket)
    assert seen["argv"][seen["argv"].index("--epoch") + 1] == "2026-Q1"
    assert seen["argv"][seen["argv"].index("--file") + 1].endswith("data.csv")
    assert result["epoch_rows"] == 12
    assert result["rows_before"] == 10
    assert result["rows_loaded"] == 2


def test_real_load_skeleton_no_op(staging_env, bucket):
    manifest = _manifest(bucket, category="skeleton", epoch="2026-03")
    result = job_runner._real_load(manifest, resolve_category("skeleton"), bucket)
    assert result["target_dir"] is None


def test_load_output_root_fail_closed_without_env(monkeypatch):
    monkeypatch.delenv(config.ENV_LOAD_STAGING_ROOT, raising=False)
    monkeypatch.delenv(config.ENV_LOAD_TARGET_ROOT, raising=False)
    with pytest.raises(RuntimeError, match="no output root"):
        config.load_output_root()


def test_load_output_root_rejects_staging_and_target_together(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_LOAD_STAGING_ROOT, str(tmp_path / "staging"))
    monkeypatch.setenv(config.ENV_LOAD_TARGET_ROOT, str(tmp_path / "target"))
    with pytest.raises(RuntimeError, match="mutually exclusive"):
        config.load_output_root()
    with pytest.raises(RuntimeError, match="mutually exclusive"):
        config.load_target_mount_root()


def test_load_output_root_selects_shadow_as_first_class_mode(monkeypatch, tmp_path):
    monkeypatch.delenv(config.ENV_LOAD_STAGING_ROOT, raising=False)
    monkeypatch.delenv(config.ENV_LOAD_TARGET_ROOT, raising=False)
    monkeypatch.setenv(config.ENV_LOAD_SHADOW_ROOT, str(tmp_path / "shadow"))

    root, staging_verify = config.load_output_root()

    assert root == tmp_path / "shadow"
    assert staging_verify is False
    assert config.load_mode() == "shadow"
    monkeypatch.setenv(config.ENV_LOAD_SHADOW_ROOT, "/market-output/shadow")
    assert config.load_target_mount_root() == Path("/market-output")


def test_shadow_root_must_be_below_dedicated_output_mount(monkeypatch):
    monkeypatch.delenv(config.ENV_LOAD_STAGING_ROOT, raising=False)
    monkeypatch.delenv(config.ENV_LOAD_TARGET_ROOT, raising=False)
    monkeypatch.setenv(config.ENV_LOAD_SHADOW_ROOT, "/tmp/shadow")

    with pytest.raises(RuntimeError, match="must be below"):
        config.load_target_mount_root()


@pytest.mark.parametrize(
    "enabled",
    [
        (config.ENV_LOAD_STAGING_ROOT, config.ENV_LOAD_SHADOW_ROOT),
        (config.ENV_LOAD_SHADOW_ROOT, config.ENV_LOAD_TARGET_ROOT),
        (config.ENV_LOAD_STAGING_ROOT, config.ENV_LOAD_TARGET_ROOT),
    ],
)
def test_load_modes_are_pairwise_mutually_exclusive(monkeypatch, tmp_path, enabled):
    for name in (
        config.ENV_LOAD_STAGING_ROOT,
        config.ENV_LOAD_SHADOW_ROOT,
        config.ENV_LOAD_TARGET_ROOT,
    ):
        monkeypatch.delenv(name, raising=False)
    for index, name in enumerate(enabled):
        monkeypatch.setenv(name, str(tmp_path / f"root-{index}"))

    with pytest.raises(RuntimeError, match="mutually exclusive"):
        config.load_mode()


# ─── full run() in staging-verify mode (loader stubbed) ──────────────
def test_run_real_staging_verify_completes(staging_env, bucket, sqlite_ledger, monkeypatch):
    manifest_path = write_submission(bucket)  # default epoch 2026-07 matches GOOD_ROWS periods
    manifest = load_manifest(manifest_path)

    def fake_run(label, argv):
        if label == "load":
            target = Path(argv[argv.index("--target-dir") + 1])
            _write_load_manifest(target, "2026-07", 9)
        # refresh must NOT be called in staging-verify
        elif label == "refresh":
            raise AssertionError("refresh ran in staging-verify mode")

    monkeypatch.setattr(job_runner, "_run_commands", fake_run)
    rc = job_runner.run(manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=None)
    assert rc == 0
    entry = sqlite_ledger.status(manifest.epoch, "ubist", manifest.manifest_sha)
    assert entry.status == "complete"
    assert entry.row_counts.get("epoch:2026-07") == 9


def test_run_real_silent_failure_marks_failed(staging_env, bucket, sqlite_ledger, monkeypatch):
    manifest_path = write_submission(bucket)
    manifest = load_manifest(manifest_path)
    monkeypatch.setattr(job_runner, "_run_commands", lambda label, argv: None)  # loads nothing
    rc = job_runner.run(manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=None)
    assert rc == 1
    entry = sqlite_ledger.status(manifest.epoch, "ubist", manifest.manifest_sha)
    assert entry.status == "failed"
    assert "LoadVerifyError" in entry.reason


def test_production_ubist_orders_shadow_gate_publish_then_refresh(
    tmp_path, bucket, sqlite_ledger, monkeypatch
):
    manifest_path = write_submission(bucket)
    manifest = load_manifest(manifest_path)
    target_root = tmp_path / "market-output"
    live_root = target_root / "ubist"
    live_root.mkdir(parents=True)
    (live_root / "_manifest.json").write_text(
        json.dumps({"schema_version": "1.0", "partitions": []}), encoding="utf-8"
    )
    monkeypatch.delenv(config.ENV_LOAD_STAGING_ROOT, raising=False)
    monkeypatch.setenv(config.ENV_LOAD_TARGET_ROOT, str(target_root))
    monkeypatch.setenv(ubist_mart_activation.ENV_PROMOTION_APPROVED, "1")
    order: list[str] = []

    class Connection:
        def close(self):
            order.append("close")

    connection = Connection()
    monkeypatch.setattr(config, "open_mart_connection", lambda *_args: connection)
    stable_snapshot = object()
    monkeypatch.setattr(
        job_runner, "fingerprint_untouched_sources", lambda *_args, **_kwargs: stable_snapshot
    )
    monkeypatch.setattr(job_runner, "sample_existing_periods", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        job_runner,
        "run_post_gates",
        lambda **_kwargs: order.append("post_gate") or type("Post", (), {"status": "pass", "duration_ms": 1})(),
    )
    from pipeline.scripts.ingest_hook import sigma_market

    monkeypatch.setattr(
        sigma_market,
        "check_market_sigma",
        lambda *_args, **_kwargs: type(
            "Sigma", (), {"cells_checked": 1, "markets_checked": 1, "worst_rel": 0.0}
        )(),
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "acquire_writer_lock",
        lambda *_args, **_kwargs: order.append("lock"),
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "release_writer_lock",
        lambda *_args, **_kwargs: order.append("unlock"),
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "require_writer_lock_owner",
        lambda *_args, **_kwargs: order.append("lock_owner"),
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "recover_incomplete_activations",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "build_shadow",
        lambda *_args, **_kwargs: order.append("mart_build"),
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "fingerprint_build_tables",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "affected_atc4_codes",
        lambda *_args, **_kwargs: ("C10A1",),
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "production_catalog_root_from_env",
        lambda: tmp_path / "provisioned-catalog",
    )
    catalog_preflight_args = []
    monkeypatch.setattr(
        ubist_mart_activation,
        "prepare_catalog_for_mart",
        lambda **kwargs: catalog_preflight_args.append(kwargs)
        or order.append("catalog_preflight")
        or type(
            "CatalogPreparation",
            (),
            {"action": "reused", "mi_master_sha256": "a" * 64, "parity": ()},
        )(),
    )
    real_promote = ubist_mart_activation.promote_candidate_corpus
    monkeypatch.setattr(
        ubist_mart_activation,
        "promote_candidate_corpus",
        lambda corpus: order.append("corpus_promote") or real_promote(corpus),
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "publish_shadow",
        lambda *_args, **_kwargs: order.append("mart_publish") or (),
    )
    original_update_journal = ubist_mart_activation.update_activation_journal

    def update_journal(path, phase):
        order.append(f"journal:{phase}")
        original_update_journal(path, phase)

    monkeypatch.setattr(
        ubist_mart_activation,
        "update_activation_journal",
        update_journal,
    )
    original_mark_complete = sqlite_ledger.mark_complete

    def mark_complete(*args, **kwargs):
        order.append("ledger:complete")
        original_mark_complete(*args, **kwargs)

    monkeypatch.setattr(sqlite_ledger, "mark_complete", mark_complete)

    def fake_run(label, argv):
        if label == "load":
            order.append("load")
            target = Path(argv[argv.index("--target-dir") + 1])
            _write_load_manifest(target, "2026-07", 9)
        elif label == "refresh":
            order.append("refresh")

    monkeypatch.setattr(job_runner, "_run_commands", fake_run)
    monkeypatch.setattr(
        job_runner,
        "_run_commands_with_writer_lock",
        lambda label, argv, **_kwargs: fake_run(label, argv),
    )
    original_inventory_load = job_runner._load_with_source_inventory

    def load_with_inventory(*args, **kwargs):
        result, _outcome = original_inventory_load(*args, **kwargs)
        snapshot = ScanSnapshot(
            "1",
            "ubist",
            "2026-07",
            manifest.manifest_sha,
            "inventory-run",
            "2026-08-07T00:00:00Z",
            (),
        )
        gates = PeriodGateResult(
            PeriodGate("PG-4", "pass", (), "continuous"),
            PeriodGate("PG-5", "pass", (), "no unexplained loss"),
            PeriodGate("PG-6", "warning", (), "row drift"),
            PeriodGate("PG-7", "warning", ("2026-07",), "newest period changed"),
        )
        return result, ScanOutcome(
            snapshot,
            tmp_path / "inventory.json",
            None,
            gates,
            result,
        )

    monkeypatch.setattr(job_runner, "_load_with_source_inventory", load_with_inventory)

    def request_automatic(identity, *, run_id, endpoint):
        candidate = sqlite_ledger.prepared_candidate(*identity)
        assert candidate is not None
        assert candidate.build_run_id == run_id
        assert candidate.payload["automatic_publish"]["hard_gates"]["PG-5"] == "pass"
        order.append("automatic_publish")
        return 200

    monkeypatch.setattr(job_runner, "_request_automatic_publish", request_automatic)

    assert job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=None
    ) == 0
    assert order.index("load") < order.index("mart_build")
    assert catalog_preflight_args[0]["ubist_dir"] == live_root
    assert order.index("mart_build") < order.index("post_gate")
    assert "corpus_promote" not in order
    assert "mart_publish" not in order
    assert "refresh" not in order
    assert "ledger:complete" not in order
    assert "automatic_publish" in order
    entry = sqlite_ledger.status(manifest.epoch, "ubist", manifest.manifest_sha)
    assert entry.status == "awaiting_approval"
    candidate = sqlite_ledger.prepared_candidate(
        manifest.epoch, "ubist", manifest.manifest_sha
    )
    assert candidate.payload["row_counts"]["epoch:2026-07"] == 9
    assert sqlite_ledger.signal_events(
        manifest.epoch, manifest.category, manifest.manifest_sha
    )[0].event == "prepared"


def test_shadow_ubist_publishes_only_to_isolated_db_and_skips_live_refresh(
    tmp_path, bucket, sqlite_ledger, monkeypatch
):
    manifest_path = write_submission(bucket)
    manifest = load_manifest(manifest_path)
    shadow_root = tmp_path / "market-output" / "shadow"
    live_root = shadow_root / "ubist"
    live_root.mkdir(parents=True)
    (live_root / "_manifest.json").write_text(
        json.dumps({"schema_version": "1.0", "partitions": []}), encoding="utf-8"
    )
    monkeypatch.delenv(config.ENV_LOAD_STAGING_ROOT, raising=False)
    monkeypatch.delenv(config.ENV_LOAD_TARGET_ROOT, raising=False)
    monkeypatch.delenv(ubist_mart_activation.ENV_PROMOTION_APPROVED, raising=False)
    monkeypatch.setenv(config.ENV_LOAD_SHADOW_ROOT, str(shadow_root))
    catalog_root = shadow_root / "catalog"
    catalog_file = catalog_root / "strategic_brand" / "strategic_brand.parquet"
    catalog_file.parent.mkdir(parents=True)
    catalog_file.write_bytes(b"isolated-catalog")
    monkeypatch.setenv(
        ubist_mart_activation.ENV_SHADOW_CATALOG_ROOT, str(catalog_root)
    )
    monkeypatch.setenv(
        ubist_mart_activation.ENV_SOURCE_DB, "jw_mart_d2_stage_20260630_r2"
    )
    monkeypatch.setenv(
        ubist_mart_activation.ENV_SHADOW_TARGET_DB, "jw_mart_ingest_shadow_test"
    )

    opened: list[str | None] = []
    order: list[str] = []

    class Connection:
        def close(self):
            order.append("close")

    def open_connection(database=None):
        opened.append(database)
        return Connection()

    monkeypatch.setattr(config, "open_mart_connection", open_connection)
    snapshot = object()
    monkeypatch.setattr(
        job_runner, "fingerprint_untouched_sources", lambda *_args, **_kwargs: snapshot
    )
    monkeypatch.setattr(job_runner, "sample_existing_periods", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        job_runner,
        "run_post_gates",
        lambda **_kwargs: order.append("post_gate")
        or type("Post", (), {"status": "pass", "duration_ms": 1})(),
    )
    from pipeline.scripts.ingest_hook import sigma_market

    monkeypatch.setattr(
        sigma_market,
        "check_market_sigma",
        lambda *_args, **_kwargs: type(
            "Sigma", (), {"cells_checked": 1, "markets_checked": 1, "worst_rel": 0.0}
        )(),
    )
    monkeypatch.setattr(
        ubist_mart_activation, "acquire_writer_lock", lambda *_args, **_kwargs: order.append("lock")
    )
    monkeypatch.setattr(
        ubist_mart_activation, "release_writer_lock", lambda *_args, **_kwargs: order.append("unlock")
    )
    monkeypatch.setattr(
        ubist_mart_activation, "require_writer_lock_owner", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        ubist_mart_activation, "recover_incomplete_activations", lambda *_args, **_kwargs: ()
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "ensure_shadow_target_baseline",
        lambda *_args, **_kwargs: order.append("shadow_bootstrap"),
    )
    monkeypatch.setattr(
        ubist_mart_activation, "build_shadow", lambda *_args, **_kwargs: order.append("mart_build")
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "fingerprint_build_tables",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "affected_atc4_codes",
        lambda *_args, **_kwargs: ("C10A1",),
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "prepare_catalog_for_mart",
        lambda **_kwargs: order.append("catalog_preflight")
        or type(
            "CatalogPreparation",
            (),
            {"action": "reused", "mi_master_sha256": "a" * 64, "parity": ()},
        )(),
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "publish_shadow",
        lambda *_args, **kwargs: order.append(("publish", kwargs["require_ledger_gate"])) or (),
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "validate_shadow_publish",
        lambda *_args, **_kwargs: order.append("shadow_refresh"),
    )
    monkeypatch.setattr(
        ubist_mart_activation, "maybe_inject_shadow_crash", lambda *_args: None
    )

    def fake_run(label, argv):
        if label == "load":
            target = Path(argv[argv.index("--target-dir") + 1])
            _write_load_manifest(target, "2026-07", 9)
            order.append("load")
        elif label == "refresh":
            raise AssertionError("production refresh ran in shadow mode")

    monkeypatch.setattr(job_runner, "_run_commands", fake_run)

    assert job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=None
    ) == 0
    assert "jw_mart_ingest_shadow_test" not in opened
    assert "shadow_bootstrap" in order
    assert order.index("shadow_bootstrap") < order.index("mart_build")
    assert not any(isinstance(item, tuple) and item[0] == "publish" for item in order)
    assert "shadow_refresh" not in order
    assert sqlite_ledger.status(
        manifest.epoch, manifest.category, manifest.manifest_sha
    ).status == "awaiting_approval"
    assert sqlite_ledger.signal_events(
        manifest.epoch, manifest.category, manifest.manifest_sha
    )[0].event == "prepared"


def test_production_ubist_does_not_release_unacquired_writer_lock(
    tmp_path, bucket, sqlite_ledger, monkeypatch
):
    manifest_path = write_submission(bucket)
    target_root = tmp_path / "market-output"
    live_root = target_root / "ubist"
    live_root.mkdir(parents=True)
    (live_root / "_manifest.json").write_text(
        json.dumps({"schema_version": "1.0", "partitions": []}), encoding="utf-8"
    )
    monkeypatch.delenv(config.ENV_LOAD_STAGING_ROOT, raising=False)
    monkeypatch.setenv(config.ENV_LOAD_TARGET_ROOT, str(target_root))
    monkeypatch.setenv(ubist_mart_activation.ENV_PROMOTION_APPROVED, "1")

    class Connection:
        def close(self):
            return None

    released: list[object] = []
    monkeypatch.setattr(config, "open_mart_connection", lambda *_args: Connection())
    monkeypatch.setattr(job_runner, "fingerprint_untouched_sources", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        ubist_mart_activation,
        "acquire_writer_lock",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("lock busy")),
    )
    monkeypatch.setattr(
        ubist_mart_activation,
        "release_writer_lock",
        lambda conn: released.append(conn),
    )

    assert job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=None
    ) == 1
    assert released == []
