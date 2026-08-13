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
        self, *, category: str, affected_scope: dict[str, object] | None,
        parent_status: str = "complete",
    ) -> None:
        self.category = category
        self.affected_scope = affected_scope
        self.parent_status = parent_status
        self.stage_records: list[dict[str, object]] = []
        self.terminals: list[dict[str, object]] = []

    def status(self, epoch: str, category: str, manifest_sha: str):
        assert epoch == "2026-Q1"
        assert category == self.category
        assert len(manifest_sha) == 64
        return SimpleNamespace(status=self.parent_status, run_id=PARENT_RUN_ID)

    def complete_reingest_request(
        self, epoch: str, category: str, manifest_sha: str, *, request_id: str
    ):
        assert epoch == "2026-Q1"
        assert category == self.category
        assert len(manifest_sha) == 64
        assert request_id == REQUEST_ID
        return SimpleNamespace(
            event_id=REQUEST_ID,
            previous_status=self.parent_status,
            status=self.parent_status,
            source="complete_reingest_request",
            evidence={
                "request_id": REQUEST_ID,
                "run_id": RUN_ID,
                "mode": "mart_from_existing_raw",
                "affected_scope": self.affected_scope,
            },
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


def _closed_connection() -> SimpleNamespace:
    return SimpleNamespace(close=lambda: None)


def _complete_stage_names(ledger: _Ledger) -> list[str]:
    return [
        str(record["stage"])
        for record in ledger.stage_records
        if record["status"] == "complete"
    ]


def _stub_terminal_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runner,
        "_emit_reingest_terminal_callback",
        lambda *_args, **_kwargs: None,
    )


@pytest.fixture(autouse=True)
def _stub_post_success_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runner,
        "_run_post_success_cleanup",
        lambda *_args, **_kwargs: None,
    )


def test_reingest_terminal_callback_uses_dedicated_queue_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = runner.RequestContext(
        identity=("2026-Q1", "ubist", "a" * 64),
        run_id=RUN_ID,
        category="ubist",
        request_id=REQUEST_ID,
        parent_run_id=PARENT_RUN_ID,
        affected_scope={"dimension": "source", "count": 1, "values": ["ubist"]},
        scope_values=("ubist",),
        period_scope=("2026-01", "2026-03"),
    )
    outcome = runner.TerminalOutcome(
        request_id=REQUEST_ID,
        run_id=RUN_ID,
        category="ubist",
        status="complete",
        reason="mart recomputation published",
    )
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        runner.config,
        "queue_drain_webhook",
        lambda: ("http://jw-ingest-hook:8080/ingest/terminal", 3),
    )
    monkeypatch.setattr(
        runner,
        "_publish_reingest_terminal",
        lambda payload, *, endpoint, attempts: captured.append(
            {"payload": payload, "endpoint": endpoint, "attempts": attempts}
        )
        or runner.PublishResult("published", 1),
    )

    runner._emit_reingest_terminal_callback(context, outcome)

    assert captured == [
        {
            "payload": {
                "epoch": "2026-Q1",
                "category": "ubist",
                "manifest_sha": "a" * 64,
                "request_id": REQUEST_ID,
                "run_id": RUN_ID,
                "status": "complete",
                "reason": "mart recomputation published",
                "job_name": None,
            },
            "endpoint": "http://jw-ingest-hook:8080/ingest/reingest/terminal",
            "attempts": 3,
        }
    ]


def _stub_numeric_success(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[tuple[str, object]],
) -> None:
    monkeypatch.setattr(
        runner.config,
        "open_mart_connection",
        lambda _schema=None: _closed_connection(),
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
        runner,
        "_publish_table_group",
        lambda *_args, **kwargs: calls.append(("publish", kwargs))
        or (SimpleNamespace(table="mart_general_brand_metric", backup_table="old"),),
    )
    monkeypatch.setattr(
        runner,
        "_run_numeric_gates",
        lambda context, prepared, spec: calls.append(
            (
                "gates",
                {
                    "category": context.category,
                    "build_db": prepared.build_db,
                    "sigma_source": spec.sigma_source,
                },
            )
        ),
    )
    monkeypatch.setattr(
        runner,
        "_run_refresh_argv",
        lambda argv, **kwargs: calls.append(("refresh_argv", {"argv": argv, **kwargs})),
    )
    monkeypatch.setattr(
        runner,
        "_removed_existing_forecast_guard",
        lambda *_args: pytest.fail("reingest must not verify/reuse existing forecast"),
        raising=False,
    )


def test_numeric_reingest_runs_core_stages_without_weekly_agent_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: a UBIST complete reingest request with existing raw corpus scope.
    affected_scope = {
        "dimension": "atc4",
        "count": 1,
        "values": ["A10A"],
        "periods": ["2026-01"],
    }
    ledger = _Ledger(category="ubist", affected_scope=affected_scope)
    calls: list[tuple[str, object]] = []
    _stub_terminal_callback(monkeypatch)
    _stub_numeric_success(monkeypatch, calls)
    monkeypatch.setattr(runner.config, "load_output_root", lambda: (tmp_path, False))
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "from_env",
        lambda *, run_id: SimpleNamespace(
            source_db="src", target_db="dst", build_db=f"build_{run_id}"
        ),
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "production_catalog_root_from_env",
        lambda: tmp_path / "catalog",
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "prepare_catalog_for_mart",
        lambda **kwargs: calls.append(("prepare_catalog", kwargs))
        or SimpleNamespace(action="noop"),
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "build_shadow",
        lambda activation, **kwargs: calls.append(
            ("build_shadow", {"activation": activation, **kwargs})
        ),
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "prepare_catalog_tables",
        lambda _conn, **kwargs: calls.append(("prepare_catalog_tables", kwargs)) or (),
    )
    monkeypatch.setattr(
        runner.ubist_mart_activation,
        "prepare_candidate_corpus",
        lambda *_args, **_kwargs: pytest.fail("raw corpus upload path must not run"),
    )

    # When: the runner handles the recomputation attempt.
    runner.run(
        _manifest(tmp_path, "ubist"),
        request_id=REQUEST_ID,
        run_id=RUN_ID,
        ledger=ledger,
    )

    # Then: it executes only the numeric ingestion stages.
    assert _complete_stage_names(ledger) == [
        "g3",
        "load",
        "load_verify",
        "mart_build",
        "sigma",
        "post_gate",
        "mart_publish",
        "refresh",
        "dashboard",
        "signal",
    ]
    assert [stage for stage in _complete_stage_names(ledger) if stage.startswith("agent")] == []
    assert not any(name == "agent_refresh" for name, _ in calls)
    assert ("gates", {"category": "ubist", "build_db": f"build_{RUN_ID}", "sigma_source": "ubist"}) in calls
    assert (
        "prepare_catalog_tables",
        {"build_db": f"build_{RUN_ID}", "catalog_root": tmp_path / "catalog"},
    ) in calls
    assert any(call[0] == "refresh_argv" for call in calls)
    assert ledger.terminals[-1]["status"] == "complete"


def test_iqvia_numeric_reingest_uses_attempt_raw_build_and_real_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: an IQVIA NSA complete request with no raw source files to materialize.
    affected_scope = {"dimension": "source", "count": 1, "values": ["iqvia_nsa"]}
    ledger = _Ledger(category="iqvia_nsa", affected_scope=affected_scope)
    calls: list[tuple[str, object]] = []
    _stub_terminal_callback(monkeypatch)
    _stub_numeric_success(monkeypatch, calls)
    monkeypatch.setattr(
        runner.iqvia_activation,
        "from_env",
        lambda *, run_id: SimpleNamespace(
            source_db="src", target_db="dst", build_db=f"build_{run_id}"
        ),
    )
    monkeypatch.setattr(
        runner.iqvia_activation,
        "initialize_build_schema",
        lambda activation: calls.append(("initialize_build_schema", activation)),
    )
    monkeypatch.setattr(
        runner.iqvia_activation,
        "copy_existing_raw",
        lambda activation: calls.append(("copy_existing_raw", activation)) or 12,
    )
    monkeypatch.setattr(
        runner.iqvia_activation,
        "trim_raw_retention",
        lambda *_args: calls.append(("trim_raw_retention", {})) or ("2025-Q4",),
    )
    monkeypatch.setattr(
        runner.iqvia_activation,
        "build_mart",
        lambda activation: calls.append(("build_mart", activation)),
    )
    monkeypatch.setattr(
        runner,
        "_removed_existing_build_table_guard",
        lambda *_args, **_kwargs: pytest.fail("parent build must not be republished"),
        raising=False,
    )

    # When: the runner handles the recomputation attempt.
    runner.run(
        _manifest(tmp_path, "iqvia_nsa"),
        request_id=REQUEST_ID,
        run_id=RUN_ID,
        ledger=ledger,
    )

    # Then: live raw seeds the attempt build, gates run, and refresh uses category refresh_argv.
    activation = SimpleNamespace(source_db="src", target_db="dst", build_db=f"build_{RUN_ID}")
    assert calls[:4] == [
        ("initialize_build_schema", activation),
        ("copy_existing_raw", activation),
        ("trim_raw_retention", {}),
        ("build_mart", activation),
    ]
    assert ("gates", {"category": "iqvia_nsa", "build_db": f"build_{RUN_ID}", "sigma_source": "iqvia_nsa"}) in calls
    refresh_calls = [payload for name, payload in calls if name == "refresh_argv"]
    assert refresh_calls and "--profile" in refresh_calls[0]["argv"]
    assert _complete_stage_names(ledger) == [
        "g3",
        "load",
        "load_verify",
        "mart_build",
        "sigma",
        "post_gate",
        "mart_publish",
        "refresh",
        "dashboard",
        "signal",
    ]


def test_refresh_failure_restores_numeric_publication_under_writer_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: a numeric publication succeeds but the real refresh command fails.
    affected_scope = {"dimension": "source", "count": 1, "values": ["iqvia_nsa"]}
    ledger = _Ledger(category="iqvia_nsa", affected_scope=affected_scope)
    actions = (SimpleNamespace(table="mart_general_brand_metric", backup_table="old"),)
    calls: list[tuple[str, object]] = []
    _stub_terminal_callback(monkeypatch)
    monkeypatch.setattr(runner.config, "open_mart_connection", lambda _schema=None: _closed_connection())
    monkeypatch.setattr(
        runner.iqvia_activation,
        "from_env",
        lambda *, run_id: SimpleNamespace(
            source_db="src", target_db="dst", build_db=f"build_{run_id}"
        ),
    )
    monkeypatch.setattr(runner.iqvia_activation, "initialize_build_schema", lambda *_args: None)
    monkeypatch.setattr(runner.iqvia_activation, "copy_existing_raw", lambda *_args: 1)
    monkeypatch.setattr(runner.iqvia_activation, "trim_raw_retention", lambda *_args: ())
    monkeypatch.setattr(runner.iqvia_activation, "build_mart", lambda *_args: None)
    monkeypatch.setattr(runner, "_run_numeric_gates", lambda *_args: None)
    monkeypatch.setattr(runner, "_publish_table_group", lambda *_args, **_kwargs: actions)
    monkeypatch.setattr(
        runner,
        "_run_refresh_argv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("refresh broke")),
    )
    monkeypatch.setattr(
        runner,
        "_restore_publication",
        lambda *_args, **kwargs: calls.append(("restore", kwargs)),
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

    # When / Then: refresh failure rolls back the atomic table group.
    with pytest.raises(RuntimeError, match="refresh broke"):
        runner.run(
            _manifest(tmp_path, "iqvia_nsa"),
            request_id=REQUEST_ID,
            run_id=RUN_ID,
            ledger=ledger,
        )
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
        ("unlock", {"lock_name": runner.ubist_mart_activation.WRITER_LOCK_NAME}),
    ]
    assert ledger.stage_records[-1]["stage"] == "refresh"
    assert ledger.stage_records[-1]["status"] == "failed"
    assert not any(
        record["stage"] == "dashboard" and record["status"] == "complete"
        for record in ledger.stage_records
    )
    assert ledger.terminals[-1]["status"] == "failed"


@pytest.mark.parametrize(
    ("category", "expected_stages"),
    [
        (
            "iqvia_csd_channel",
            ["g3", "load", "load_verify", "mart_publish", "context_bridge", "dashboard", "signal"],
        ),
        (
            "iqvia_csd_keyword",
            ["g3", "load", "load_verify", "post_gate", "mart_publish", "topic_extraction", "dashboard", "signal"],
        ),
    ],
)
def test_csd_reingest_records_only_source_core_stages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    category: str,
    expected_stages: list[str],
) -> None:
    # Given: a CSD complete request whose live raw table already contains data.
    affected_scope = {"dimension": "source", "count": 1, "values": [category]}
    ledger = _Ledger(category=category, affected_scope=affected_scope)
    calls: list[tuple[str, object]] = []
    plan = SimpleNamespace(run_id=RUN_ID)
    evidence = SimpleNamespace(raw_rows=10, stage_rows=8)
    _stub_terminal_callback(monkeypatch)
    monkeypatch.setattr(runner.config, "open_csd_channel_connection", lambda: _closed_connection())
    monkeypatch.setattr(runner.config, "open_mart_connection", lambda _schema=None: _closed_connection())
    monkeypatch.setattr(
        runner.config,
        "csd_channel_live_schemas",
        lambda *, mode: ("raw_live", "stage_live"),
    )
    monkeypatch.setattr(runner.config, "csd_keyword_live_schemas", lambda: ("raw_live", "stage_live"))
    monkeypatch.setattr(
        runner.csd_channel_activation,
        "plan_for_run",
        lambda run_id, **kwargs: calls.append(("channel_plan", {"run_id": run_id, **kwargs})) or plan,
    )
    monkeypatch.setattr(
        runner.csd_channel_activation,
        "prepare_candidate",
        lambda *_args, **kwargs: calls.append(("channel_prepare", kwargs)) or evidence,
    )
    monkeypatch.setattr(
        runner.csd_channel_activation,
        "publish_candidate",
        lambda *_args: calls.append(("channel_publish", {})) or runner.csd_channel_activation.SwapVerdict.APPLIED,
    )
    monkeypatch.setattr(
        runner.csd_keyword_activation,
        "plan_for_run",
        lambda run_id, **kwargs: calls.append(("keyword_plan", {"run_id": run_id, **kwargs})) or plan,
    )
    monkeypatch.setattr(
        runner.csd_keyword_activation,
        "prepare_candidate_from_live_raw",
        lambda *_args: calls.append(("keyword_prepare", {})) or evidence,
    )
    monkeypatch.setattr(
        runner.csd_keyword_activation,
        "publish_candidate",
        lambda *_args: calls.append(("keyword_publish", {})),
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
        runner,
        "_publish_table_group",
        lambda *_args, **_kwargs: pytest.fail("CSD must publish through source activation"),
    )

    # When: the runner handles the CSD attempt.
    runner.run(
        _manifest(tmp_path, category),
        request_id=REQUEST_ID,
        run_id=RUN_ID,
        ledger=ledger,
    )

    # Then: only the source-owned CSD core stage chain is recorded.
    assert _complete_stage_names(ledger) == expected_stages
    dashboard = next(
        record for record in ledger.stage_records if record["stage"] == "dashboard"
    )
    assert "target_schema=stage_live" in str(dashboard["reason"])
    assert "raw_rows=10" in str(dashboard["reason"])
    assert "stage_rows=8" in str(dashboard["reason"])
    assert not {"agent_refresh", "agent2", "agent3"}.intersection(_complete_stage_names(ledger))
    assert ledger.terminals[-1]["status"] == "complete"


def test_s3_input_source_reads_only_manifest_without_materializing_raw_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: a configured input source with a manifest key and raw file entries.
    local_manifest = _manifest(tmp_path, "iqvia_nsa")
    manifest_bytes = local_manifest.read_bytes()
    affected_scope = {"dimension": "source", "count": 1, "values": ["iqvia_nsa"]}
    ledger = _Ledger(category="iqvia_nsa", affected_scope=affected_scope)
    reads: list[str] = []

    class Source:
        def read(self, key: str) -> bytes:
            reads.append(key)
            return manifest_bytes

        def materialize(self, *_args: object, **_kwargs: object) -> None:
            pytest.fail("complete runner must not materialize raw source files")

    _stub_terminal_callback(monkeypatch)
    _stub_numeric_success(monkeypatch, [])
    monkeypatch.setattr(
        runner.iqvia_activation,
        "from_env",
        lambda *, run_id: SimpleNamespace(
            source_db="src", target_db="dst", build_db=f"build_{run_id}"
        ),
    )
    monkeypatch.setattr(runner.iqvia_activation, "initialize_build_schema", lambda *_args: None)
    monkeypatch.setattr(runner.iqvia_activation, "copy_existing_raw", lambda *_args: 1)
    monkeypatch.setattr(runner.iqvia_activation, "trim_raw_retention", lambda *_args: ())
    monkeypatch.setattr(runner.iqvia_activation, "build_mart", lambda *_args: None)

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


def test_noncomplete_parent_is_accepted_when_request_identity_is_persisted() -> None:
    ledger = _Ledger(
        category="iqvia_nsa",
        affected_scope={"dimension": "source", "count": 1, "values": ["iqvia_nsa"]},
        parent_status="failed",
    )

    context = runner._validate_request(
        ledger,
        identity=("2026-Q1", "iqvia_nsa", "c" * 64),
        request_id=REQUEST_ID,
        run_id=RUN_ID,
    )

    assert context.parent_run_id == PARENT_RUN_ID


def test_record_stage_emits_status_markers_with_redacted_failure_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: a durable stage logger consuming stdout markers.
    ledger = _Ledger(
        category="iqvia_nsa",
        affected_scope={"dimension": "source", "count": 1, "values": ["iqvia_nsa"]},
    )
    context = runner.RequestContext(
        identity=("2026-Q1", "iqvia_nsa", "c" * 64),
        run_id=RUN_ID,
        category="iqvia_nsa",
        request_id=REQUEST_ID,
        parent_run_id=PARENT_RUN_ID,
        affected_scope=ledger.affected_scope,
        scope_values=("iqvia_nsa",),
        period_scope=(),
    )

    # When: stages transition through running, complete, and failed.
    runner._record_stage(ledger, context, "refresh", "running")
    runner._record_stage(ledger, context, "refresh", "complete")
    runner._record_stage(
        ledger,
        context,
        "refresh",
        "failed",
        "RuntimeError: " + "pass" + "word=plain " + "to" + "ken:abc123 safe-detail",
    )

    # Then: stdout carries separable start/end markers and redacts sensitive values.
    assert capsys.readouterr().out.splitlines() == [
        "[stage] refresh start",
        "[stage] refresh end",
        "[stage] refresh end rc=1 reason=RuntimeError: "
        + "pass"
        + "word=<redacted> "
        + "to"
        + "ken:<redacted> safe-detail",
    ]


def test_cli_accepts_launcher_flags_and_rejects_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: launcher-style arguments with a matching manifest identity.
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
