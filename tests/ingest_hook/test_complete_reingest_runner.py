from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from pipeline.scripts.ingest_hook import complete_reingest_runner as runner
from pipeline.scripts.ingest_hook import ubist_mart_activation


REQUEST_ID = "123e4567-e89b-12d3-a456-426614174000"
RUN_ID = "20260809090101000000"
PARENT_RUN_ID = "20260806155944833982"


class _Ledger:
    def __init__(
        self, *, category: str, affected_scope: dict[str, object] | None
    ) -> None:
        self.category = category
        self.affected_scope = affected_scope
        self.stage_records: list[dict[str, object]] = []
        self.terminals: list[dict[str, object]] = []
        self.last_identity: tuple[str, str, str] | None = None

    def status(self, epoch: str, category: str, manifest_sha: str):
        assert epoch == "2026-Q1"
        assert category == self.category
        assert len(manifest_sha) == 64
        self.last_identity = (epoch, category, manifest_sha)
        return SimpleNamespace(status="complete", run_id=PARENT_RUN_ID)

    def complete_reingest_request(
        self, epoch: str, category: str, manifest_sha: str, *, request_id: str
    ):
        assert epoch == "2026-Q1"
        assert category == self.category
        assert len(manifest_sha) == 64
        assert request_id == REQUEST_ID
        evidence = {
            "request_id": REQUEST_ID,
            "run_id": RUN_ID,
            "mode": "mart_from_existing_raw",
            "affected_scope": self.affected_scope,
        }
        return SimpleNamespace(
            event_id=REQUEST_ID,
            previous_status="complete",
            status="complete",
            source="complete_reingest_request",
            evidence=evidence,
        )

    def record_stage(self, *_identity: str, **kwargs: object) -> None:
        self.stage_records.append(kwargs)

    def record_complete_reingest_terminal(
        self, *_identity: str, **kwargs: object
    ) -> bool:
        self.terminals.append(kwargs)
        return True

    def mark_running(self, *_args: object, **_kwargs: object) -> None:
        pytest.fail("runner must not mutate the parent ledger row")

    def mark_complete(self, *_args: object, **_kwargs: object) -> None:
        pytest.fail("runner must not mutate the parent ledger row")

    def mark_failed(self, *_args: object, **_kwargs: object) -> None:
        pytest.fail("runner must not mutate the parent ledger row")


def _manifest(tmp_path: Path, category: str) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "contract_version": "v2",
                "epoch": "2026-Q1",
                "category": category,
                "complete": True,
                "files": [{"path": "source.xlsx", "sha256": "b" * 64}],
            }
        ),
        encoding="utf-8",
    )
    return path


def _stub_iqvia_success(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, object]]) -> None:
    monkeypatch.setattr(
        runner.config,
        "open_mart_connection",
        lambda _schema=None: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(
        runner.iqvia_activation,
        "from_env",
        lambda *, run_id: SimpleNamespace(
            source_db="src", target_db="dst", build_db=f"build_{run_id}"
        ),
    )
    monkeypatch.setattr(
        runner,
        "_require_existing_build_tables",
        lambda *_args, **kwargs: calls.append(("reuse", kwargs)),
    )
    monkeypatch.setattr(
        runner,
        "_publish_table_group",
        lambda *_args, **kwargs: calls.append(("publish", kwargs)) or (),
    )
    monkeypatch.setattr(
        runner,
        "_verify_existing_forecast",
        lambda *_args: calls.append(("verify_forecast", {}))
        or {"blocks": 43_790, "horizons": 3_002, "bad_simulation": 0},
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "acquire_writer_lock",
        lambda *_args, **kwargs: calls.append(("lock", kwargs)),
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "release_writer_lock",
        lambda *_args, **kwargs: calls.append(("unlock", kwargs)),
    )


def test_ubist_reingest_reuses_parent_build_without_recomputation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: a persisted complete-reingest request with an exact ATC4 scope.
    affected_scope = {
        "dimension": "atc4",
        "count": 2,
        "values": ["A10A", "B20B"],
        "periods": ["2026-01", "2026-02"],
    }
    ledger = _Ledger(category="ubist", affected_scope=affected_scope)
    calls: list[tuple[str, object]] = []
    manifest_path = _manifest(tmp_path, "ubist")
    monkeypatch.setattr(
        runner.config,
        "open_mart_connection",
        lambda _schema=None: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "from_env",
        lambda *, run_id: SimpleNamespace(
            source_db="src", target_db="dst", build_db=f"build_{run_id}"
        ),
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "fingerprint_build_tables",
        lambda *_args, **_kwargs: calls.append(("reuse", _args[1])) or tuple(
            SimpleNamespace(table=table, row_count=1, crc_sum=2, crc_xor=3)
            for table in ubist_mart_activation.NUMERIC_TABLES
        ),
    )
    monkeypatch.setattr(
        runner,
        "_publish_table_group",
        lambda *_args, **kwargs: calls.append(("publish", kwargs)) or (),
    )
    monkeypatch.setattr(
        runner,
        "_verify_existing_forecast",
        lambda *_args: calls.append(("verify_forecast", {}))
        or {"blocks": 43_790, "horizons": 3_002, "bad_simulation": 0},
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "acquire_writer_lock",
        lambda *_args, **kwargs: calls.append(("lock", kwargs)),
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "release_writer_lock",
        lambda *_args, **kwargs: calls.append(("unlock", kwargs)),
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "prepare_candidate_corpus",
        lambda *_args, **_kwargs: pytest.fail(
            "corpus candidate promoter must not run"
        ),
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "promote_candidate_corpus",
        lambda *_args, **_kwargs: pytest.fail("corpus promoter must not run"),
    )

    # When: the mart-only runner handles the attempt.
    runner.run(
        manifest_path,
        request_id=REQUEST_ID,
        run_id=RUN_ID,
        ledger=ledger,
    )

    # Then: the parent build is reused; no raw or mart computation runs.
    assert calls == [
        ("reuse", f"build_{PARENT_RUN_ID}"),
        ("lock", {"timeout_seconds": 0, "lock_name": runner.ubist_mart_activation.WRITER_LOCK_NAME}),
        (
            "publish",
            {
                "build_db": f"build_{PARENT_RUN_ID}",
                "target_db": "dst",
                "run_id": RUN_ID,
                "tables": ubist_mart_activation.NUMERIC_TABLES,
            },
        ),
        ("verify_forecast", {}),
        ("unlock", {"lock_name": runner.ubist_mart_activation.WRITER_LOCK_NAME}),
    ]
    assert {record["run_id"] for record in ledger.stage_records} == {RUN_ID}
    assert ledger.terminals[-1]["status"] == "complete"
    assert ledger.terminals[-1]["affected_scope"] == affected_scope


def test_ubist_source_scope_still_reuses_parent_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: control-plane fallback source scope for UBIST.
    affected_scope = {"dimension": "source", "count": 1, "values": ["ubist"]}
    ledger = _Ledger(category="ubist", affected_scope=affected_scope)
    calls: list[tuple[str, object]] = []
    manifest_path = _manifest(tmp_path, "ubist")
    monkeypatch.setattr(
        runner.config,
        "open_mart_connection",
        lambda _schema=None: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "from_env",
        lambda *, run_id: SimpleNamespace(
            source_db="src", target_db="dst", build_db=f"build_{run_id}"
        ),
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "fingerprint_build_tables",
        lambda *_args, **_kwargs: calls.append(("reuse", _args[1])) or (),
    )
    monkeypatch.setattr(
        runner,
        "_publish_table_group",
        lambda *_args, **kwargs: calls.append(("publish", kwargs)) or (),
    )
    monkeypatch.setattr(
        runner,
        "_verify_existing_forecast",
        lambda *_args: calls.append(("verify_forecast", {}))
        or {"blocks": 43_790, "horizons": 3_002, "bad_simulation": 0},
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "acquire_writer_lock",
        lambda *_args, **kwargs: calls.append(("lock", kwargs)),
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "release_writer_lock",
        lambda *_args, **kwargs: calls.append(("unlock", kwargs)),
    )

    # When: the runner handles the fallback scope.
    runner.run(manifest_path, request_id=REQUEST_ID, run_id=RUN_ID, ledger=ledger)

    # Then: scope does not trigger a rebuild.
    assert calls[0] == ("reuse", f"build_{PARENT_RUN_ID}")


def test_iqvia_reingest_reuses_parent_build_and_publishes_full_nsa_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: a persisted complete-reingest request scoped to IQVIA NSA.
    affected_scope = {"dimension": "source", "count": 1, "values": ["iqvia_nsa"]}
    ledger = _Ledger(category="iqvia_nsa", affected_scope=affected_scope)
    calls: list[tuple[str, object]] = []
    manifest_path = _manifest(tmp_path, "iqvia_nsa")
    _stub_iqvia_success(monkeypatch, calls)
    monkeypatch.setattr(
        runner.iqvia_activation,
        "initialize_build_schema",
        lambda *_args, **_kwargs: pytest.fail(
            "empty raw loader initializer must not run"
        ),
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "promote_candidate_corpus",
        lambda *_args, **_kwargs: pytest.fail("corpus promoter must not run"),
    )

    # When: the mart-only runner handles the attempt.
    runner.run(
        manifest_path,
        request_id=REQUEST_ID,
        run_id=RUN_ID,
        ledger=ledger,
    )

    # Then: the parent build is reused and the full NSA serving contract publishes.
    assert calls == [
        (
            "reuse",
            {
                "build_db": f"build_{PARENT_RUN_ID}",
                "tables": runner.iqvia_activation.NSA_PUBLISH_TABLES,
            },
        ),
        ("lock", {"timeout_seconds": 0, "lock_name": runner.ubist_mart_activation.WRITER_LOCK_NAME}),
        (
            "publish",
            {
                "build_db": f"build_{PARENT_RUN_ID}",
                "target_db": "dst",
                "run_id": RUN_ID,
                "tables": runner.iqvia_activation.NSA_PUBLISH_TABLES,
            },
        ),
        ("verify_forecast", {}),
        ("unlock", {"lock_name": runner.ubist_mart_activation.WRITER_LOCK_NAME}),
    ]
    assert ledger.terminals[-1]["status"] == "complete"
    assert [
        (record["seq"], record["stage"], record["status"])
        for record in ledger.stage_records
    ] == [
        (1, "request_validate", "running"),
        (1, "request_validate", "complete"),
        (2, "mart_build", "running"),
        (2, "mart_build", "complete"),
        (3, "mart_publish", "running"),
        (3, "mart_publish", "complete"),
        (4, "refresh", "running"),
        (4, "refresh", "complete"),
        (5, "agent_refresh", "complete"),
        (6, "agent3", "complete"),
        (7, "agent2", "complete"),
        (8, "dashboard", "complete"),
    ]


def test_s3_input_source_reads_only_manifest_without_materializing_raw_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: a configured input source with a manifest key and raw file entries.
    local_manifest = _manifest(tmp_path, "iqvia_nsa")
    manifest_bytes = local_manifest.read_bytes()
    affected_scope = {"dimension": "source", "count": 1, "values": ["iqvia_nsa"]}
    ledger = _Ledger(category="iqvia_nsa", affected_scope=affected_scope)
    calls: list[tuple[str, object]] = []
    reads: list[str] = []

    class Source:
        def read(self, key: str) -> bytes:
            reads.append(key)
            return manifest_bytes

        def materialize(self, *_args: object, **_kwargs: object) -> None:
            pytest.fail("complete runner must not materialize raw source files")

    _stub_iqvia_success(monkeypatch, calls)

    # When: the runner receives an immutable remote manifest path.
    runner.run(
        Path("_manifests/iqvia_nsa/2026-Q1/manifest.json"),
        request_id=REQUEST_ID,
        run_id=RUN_ID,
        ledger=ledger,
        input_source=Source(),
    )

    # Then: only the manifest key is read; raw files remain untouched.
    assert reads == ["_manifests/iqvia_nsa/2026-Q1/manifest.json"]
    assert calls[-1] == (
        "unlock",
        {"lock_name": runner.ubist_mart_activation.WRITER_LOCK_NAME},
    )


def test_missing_affected_scope_fails_before_mart_operations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: a persisted request without an affected_scope.
    ledger = _Ledger(category="iqvia_nsa", affected_scope=None)
    manifest_path = _manifest(tmp_path, "iqvia_nsa")
    monkeypatch.setattr(
        runner.iqvia_activation,
        "from_env",
        lambda **_kwargs: pytest.fail("mart activation must not start"),
    )

    # When / Then: the runner fails closed before any mart side effect.
    with pytest.raises(runner.CompleteReingestRejected, match="affected_scope"):
        runner.run(
            manifest_path,
            request_id=REQUEST_ID,
            run_id=RUN_ID,
            ledger=ledger,
        )
    assert ledger.stage_records == []
    assert ledger.terminals == []


def test_cli_accepts_launcher_flags_and_rejects_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: launcher-style arguments with a mismatched category.
    manifest_path = _manifest(tmp_path, "ubist")
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        runner,
        "run",
        lambda manifest, **kwargs: captured.append({"manifest": manifest, **kwargs}),
    )
    manifest_sha = runner.load_manifest(manifest_path).manifest_sha

    # When: identity matches, main delegates to run.
    assert (
        runner.main(
            [
                "--manifest",
                str(manifest_path),
                "--epoch",
                "2026-Q1",
                "--category",
                "ubist",
                "--manifest-sha",
                manifest_sha,
                "--request-id",
                REQUEST_ID,
                "--run-id",
                RUN_ID,
                "--affected-scope-json",
                '{"count":1,"dimension":"source","values":["ubist"]}',
            ]
        )
        == 0
    )

    # Then: launcher flags are parsed and affected_scope is passed through.
    assert captured[0]["manifest"] == manifest_path
    assert captured[0]["expected_affected_scope"] == {
        "count": 1,
        "dimension": "source",
        "values": ["ubist"],
    }

    # When / Then: a CLI identity mismatch fails before run() is called again.
    with pytest.raises(runner.CompleteReingestRejected, match="CLI identity"):
        runner.main(
            [
                "--manifest",
                str(manifest_path),
                "--epoch",
                "2026-Q1",
                "--category",
                "iqvia_nsa",
                "--manifest-sha",
                manifest_sha,
                "--request-id",
                REQUEST_ID,
                "--run-id",
                RUN_ID,
                "--affected-scope-json",
                '{"count":1,"dimension":"source","values":["ubist"]}',
            ]
        )
    assert len(captured) == 1


def test_missing_parent_build_records_failure_without_recomputation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: a valid IQVIA request whose parent build artifact is absent.
    affected_scope = {"dimension": "source", "count": 1, "values": ["iqvia_nsa"]}
    ledger = _Ledger(category="iqvia_nsa", affected_scope=affected_scope)
    manifest_path = _manifest(tmp_path, "iqvia_nsa")
    monkeypatch.setattr(
        runner.config,
        "open_mart_connection",
        lambda _schema=None: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(
        runner.iqvia_activation,
        "from_env",
        lambda *, run_id: SimpleNamespace(
            source_db="src", target_db="dst", build_db=f"build_{run_id}"
        ),
    )
    monkeypatch.setattr(
        runner,
        "_require_existing_build_tables",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            runner.CompleteReingestRejected("parent build absent")
        ),
    )

    # When / Then: failure evidence is append-only and parent mutators are unused.
    with pytest.raises(runner.CompleteReingestRejected, match="parent build absent"):
        runner.run(manifest_path, request_id=REQUEST_ID, run_id=RUN_ID, ledger=ledger)
    assert ledger.stage_records[-1]["stage"] == "mart_build"
    assert ledger.stage_records[-1]["status"] == "failed"
    assert "parent build absent" in str(ledger.stage_records[-1]["reason"])
    assert ledger.terminals[-1]["status"] == "failed"


def test_existing_forecast_gate_failure_restores_under_writer_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: a published IQVIA mart-only attempt whose existing forecast gate fails.
    affected_scope = {"dimension": "source", "count": 1, "values": ["iqvia_nsa"]}
    ledger = _Ledger(category="iqvia_nsa", affected_scope=affected_scope)
    manifest_path = _manifest(tmp_path, "iqvia_nsa")
    actions = (SimpleNamespace(table="mart_general_brand_metric"),)
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        runner.config,
        "open_mart_connection",
        lambda _schema=None: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(
        runner.iqvia_activation,
        "from_env",
        lambda *, run_id: SimpleNamespace(
            source_db="src", target_db="dst", build_db=f"build_{run_id}"
        ),
    )
    monkeypatch.setattr(runner, "_require_existing_build_tables", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_publish_table_group", lambda *_a, **_k: actions)
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "acquire_writer_lock",
        lambda *_a, **kwargs: calls.append(("lock", kwargs)),
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "release_writer_lock",
        lambda *_a, **kwargs: calls.append(("unlock", kwargs)),
    )
    monkeypatch.setattr(
        runner,
        "_restore_publication",
        lambda *_a, **kwargs: calls.append(("restore", kwargs)),
    )
    monkeypatch.setattr(
        runner,
        "_verify_existing_forecast",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("forecast gate broke")),
    )

    # When / Then: gate failure rolls back the table group and appends failed terminal.
    with pytest.raises(RuntimeError, match="forecast gate broke"):
        runner.run(manifest_path, request_id=REQUEST_ID, run_id=RUN_ID, ledger=ledger)
    assert calls == [
        (
            "lock",
            {
                "timeout_seconds": 0,
                "lock_name": runner.ubist_mart_activation.WRITER_LOCK_NAME,
            },
        ),
        (
            "restore",
            {
                "publication": runner.Publication("dst", actions),
                "run_id": RUN_ID,
            },
        ),
        (
            "unlock",
            {"lock_name": runner.ubist_mart_activation.WRITER_LOCK_NAME},
        ),
    ]
    assert ledger.terminals[-1]["status"] == "failed"
    assert ledger.stage_records[-1]["stage"] == "refresh"
    assert ledger.stage_records[-1]["status"] == "failed"
