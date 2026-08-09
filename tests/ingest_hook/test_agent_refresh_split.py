from __future__ import annotations

from fastapi.testclient import TestClient

from pipeline.scripts.ingest_hook.app import IngestService, create_app
from pipeline.scripts.ingest_hook.category_map import resolve_category
from pipeline.scripts.ingest_hook import agent_refresh_runner


def _terminal_payload(*, event: str = "complete", mode: str = "production") -> dict:
    return {
        "event": event,
        "mode": mode,
        "category": "ubist",
        "epoch": "2026-05",
        "manifest_sha": "a" * 64,
        "rows_before": 10,
        "rows_after": 11,
        "rows_loaded": 1,
        "period": {"from": "2026-05", "to": "2026-05"},
        "started_at": "2026-07-30T00:00:00+00:00",
        "finished_at": "2026-07-30T00:01:00+00:00",
        "failure_reason": None,
        "log_ref": "/ingest/status",
        "event_id": "7d77770d-7a77-5777-8777-777777777777",
        "run_id": "run-1",
        "schema_version": "1",
        "source": "ubist",
        "target_schema": "jw_mart_d2_stage_20260630_r2",
        "published_at": "2026-07-30 00:01:00",
        "occurred_at": "2026-07-30 00:01:00",
    }


def test_ingest_refresh_uses_only_numeric_profile() -> None:
    argv = resolve_category("ubist").refresh_argv

    assert argv[argv.index("--profile") + 1] == "numeric"
    assert "--force" in argv


def test_agent_refresh_forces_manifest_identity_past_epoch_only_checkpoint(
    sqlite_ledger, monkeypatch
) -> None:
    from pipeline.scripts.ingest_hook import agent_refresh_runner

    commands: list[list[str]] = []
    monkeypatch.setattr(
        agent_refresh_runner.config,
        "open_configured_ledger",
        lambda: sqlite_ledger,
    )
    monkeypatch.setattr(
        agent_refresh_runner.subprocess,
        "run",
        lambda command, check: commands.append(command)
        or type("Result", (), {"returncode": 0})(),
    )

    assert agent_refresh_runner.run(
        epoch="2026-05",
        category="ubist",
        manifest_sha="a" * 64,
        ingest_run_id="run-1",
    ) == 0
    assert "--force" in commands[0]

    rows = sqlite_ledger.stage_events("2026-05", "ubist", "a" * 64)
    assert [(row.seq, row.stage, row.status) for row in rows] == [
        (1, "agent_refresh", "complete"),
        (2, "agent3", "complete"),
        (3, "agent2", "complete"),
        (4, "dashboard", "complete"),
    ]


def test_agent_refresh_reuses_complete_forecast_staging_only_when_requested(
    sqlite_ledger, monkeypatch
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        agent_refresh_runner.config,
        "open_configured_ledger",
        lambda: sqlite_ledger,
    )
    monkeypatch.setattr(
        agent_refresh_runner.subprocess,
        "run",
        lambda command, check: commands.append(command)
        or type("Result", (), {"returncode": 0})(),
    )

    assert agent_refresh_runner.run(
        epoch="2026-06",
        category="ubist",
        manifest_sha="a" * 64,
        ingest_run_id="run-1",
        agent_run_id="run-1:agent-refresh-retry-3",
        reuse_forecast_staging=True,
    ) == 0

    assert commands[0][commands[0].index("--profile") + 1] == "agent"
    assert "--force" not in commands[0]


def test_failed_agent_refresh_does_not_claim_derived_stages(
    sqlite_ledger, monkeypatch
) -> None:
    monkeypatch.setattr(
        agent_refresh_runner.config,
        "open_configured_ledger",
        lambda: sqlite_ledger,
    )
    monkeypatch.setattr(
        agent_refresh_runner.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 1})(),
    )

    assert agent_refresh_runner.run(
        epoch="2026-05",
        category="ubist",
        manifest_sha="a" * 64,
        ingest_run_id="run-1",
    ) == 1

    rows = sqlite_ledger.stage_events("2026-05", "ubist", "a" * 64)
    assert [(row.seq, row.stage, row.status) for row in rows] == [
        (1, "agent_refresh", "failed"),
    ]


def test_complete_terminal_launches_agent_job_after_ingest_is_complete(
    sqlite_ledger, fake_transport
) -> None:
    identity = ("2026-05", "ubist", "a" * 64)
    sqlite_ledger.receive(*identity, manifest_path="/input/manifest.json")
    sqlite_ledger.mark_running(*identity, job_name="jw-ingest-parent", run_id="run-1")
    sqlite_ledger.mark_complete(*identity, row_counts={"input.xlsx": 1})
    client = TestClient(
        create_app(IngestService(sqlite_ledger, None, transport=fake_transport))
    )

    response = client.post("/ingest/terminal", json=_terminal_payload())

    assert response.status_code == 200
    payload = response.json()
    assert payload["agent_trigger_status"] == "submitted"
    assert payload["agent_job_name"].startswith("jw-agent-refresh-ubist-")
    assert sqlite_ledger.status(*identity).status == "complete"
    assert len(fake_transport.submitted) == 1
    submitted = fake_transport.submitted[0][1]
    assert submitted["metadata"]["labels"]["app"] == "jw-agent-refresh"


def test_agent_refresh_retry_preserves_failed_attempt(sqlite_ledger, monkeypatch):
    identity = ("2026-05", "ubist", "a" * 64)
    sqlite_ledger.receive(*identity, manifest_path="/input/manifest.json")
    sqlite_ledger.mark_running(
        *identity, job_name="jw-ingest-parent", run_id="run-1"
    )
    sqlite_ledger.mark_complete(*identity, row_counts={"input.xlsx": 1})
    sqlite_ledger.record_stage(
        *identity,
        run_id="run-1:agent-refresh",
        seq=1,
        stage="agent_refresh",
        status="failed",
        started_at="2026-07-30T00:01:00+00:00",
        finished_at="2026-07-30T00:02:00+00:00",
    )
    monkeypatch.setattr(
        agent_refresh_runner.config,
        "open_configured_ledger",
        lambda: sqlite_ledger,
    )
    monkeypatch.setattr(
        agent_refresh_runner.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})(),
    )

    assert agent_refresh_runner.run(
        epoch=identity[0],
        category=identity[1],
        manifest_sha=identity[2],
        ingest_run_id="run-1",
        agent_run_id="run-1:agent-refresh-retry-2",
    ) == 0

    rows = sqlite_ledger.stage_events(*identity)
    assert [(row.run_id, row.stage, row.status) for row in rows] == [
        ("run-1:agent-refresh", "agent_refresh", "failed"),
        ("run-1:agent-refresh-retry-2", "agent_refresh", "complete"),
        ("run-1:agent-refresh-retry-2", "agent3", "complete"),
        ("run-1:agent-refresh-retry-2", "agent2", "complete"),
        ("run-1:agent-refresh-retry-2", "dashboard", "complete"),
    ]


def test_terminal_accepts_v1_source_without_legacy_category(
    sqlite_ledger, fake_transport
) -> None:
    identity = ("2026-05", "ubist", "a" * 64)
    sqlite_ledger.receive(*identity, manifest_path="/input/manifest.json")
    sqlite_ledger.mark_running(*identity, job_name="jw-ingest-parent", run_id="run-1")
    sqlite_ledger.mark_complete(*identity, row_counts={"input.xlsx": 1})
    payload = _terminal_payload()
    payload.pop("category")

    response = TestClient(
        create_app(IngestService(sqlite_ledger, None, transport=fake_transport))
    ).post("/ingest/terminal", json=payload)

    assert response.status_code == 200
    assert response.json()["category"] == "ubist"


def test_terminal_rejects_source_and_legacy_category_mismatch(sqlite_ledger) -> None:
    payload = _terminal_payload()
    payload["category"] = "iqvia_nsa"

    response = TestClient(create_app(IngestService(sqlite_ledger, None))).post(
        "/ingest/terminal", json=payload
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "source and legacy category must match"


def test_terminal_rejects_v1_payload_with_missing_required_field(sqlite_ledger) -> None:
    payload = _terminal_payload()
    payload.pop("category")
    payload.pop("event_id")

    response = TestClient(create_app(IngestService(sqlite_ledger, None))).post(
        "/ingest/terminal", json=payload
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "missing completion v1 fields: event_id"


def test_terminal_rejects_complete_v1_payload_without_published_at(sqlite_ledger) -> None:
    payload = _terminal_payload()
    payload.pop("published_at")

    response = TestClient(create_app(IngestService(sqlite_ledger, None))).post(
        "/ingest/terminal", json=payload
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "missing completion v1 fields: published_at"


def test_terminal_accepts_failed_v1_payload_with_null_publish_fields(sqlite_ledger) -> None:
    identity = ("2026-05", "ubist", "a" * 64)
    sqlite_ledger.receive(*identity, manifest_path="/input/manifest.json")
    sqlite_ledger.mark_running(*identity, job_name="jw-ingest-parent", run_id="run-1")
    sqlite_ledger.mark_failed(*identity, reason="injected")
    payload = _terminal_payload(event="failed")
    payload["target_schema"] = None
    payload["published_at"] = None

    response = TestClient(create_app(IngestService(sqlite_ledger, None))).post(
        "/ingest/terminal", json=payload
    )

    assert response.status_code == 200
    assert response.json()["terminal_status"] == "failed"


def test_terminal_rejects_unknown_completion_schema_version(sqlite_ledger) -> None:
    payload = _terminal_payload()
    payload["schema_version"] = "2"

    response = TestClient(create_app(IngestService(sqlite_ledger, None))).post(
        "/ingest/terminal", json=payload
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "unsupported completion schema_version '2'"


def test_global_agent_refresh_cap_blocks_a_second_active_job(sqlite_ledger) -> None:
    identity = ("2026-05", "ubist", "a" * 64)
    sqlite_ledger.receive(*identity, manifest_path="/input/manifest.json")
    sqlite_ledger.mark_running(*identity, job_name="jw-ingest-parent", run_id="run-1")
    sqlite_ledger.mark_complete(*identity, row_counts={"input.xlsx": 1})
    submitted: list[dict] = []

    def transport(_url_path: str, body: dict) -> dict:
        submitted.append(body)
        return {"status": "created"}

    def list_transport(_namespace: str, _label_selector: str) -> dict:
        return {
            "items": [
                {
                    "metadata": {"name": "jw-agent-refresh-other"},
                    "status": {"active": 1},
                }
            ]
        }

    client = TestClient(
        create_app(
            IngestService(
                sqlite_ledger,
                None,
                transport=transport,
                list_transport=list_transport,
            )
        )
    )

    response = client.post("/ingest/terminal", json=_terminal_payload())

    assert response.status_code == 200
    assert response.json()["agent_trigger_status"] == "deferred_capacity"
    assert response.json()["agent_trigger_reason"] == "global agent refresh cap reached (1/1)"
    assert submitted == []


def test_non_complete_terminal_never_submits_agent_job(sqlite_ledger) -> None:
    identity = ("2026-05", "ubist", "a" * 64)
    sqlite_ledger.receive(*identity, manifest_path="/input/manifest.json")
    sqlite_ledger.mark_running(*identity, job_name="jw-ingest-parent", run_id="run-1")
    sqlite_ledger.mark_failed(*identity, reason="injected")
    submitted: list[dict] = []
    client = TestClient(
        create_app(
            IngestService(
                sqlite_ledger,
                None,
                transport=lambda _path, body: submitted.append(body) or {},
            )
        )
    )

    response = client.post(
        "/ingest/terminal",
        json=_terminal_payload(event="failed"),
    )

    assert response.status_code == 200
    assert response.json()["agent_trigger_status"] == "not_applicable"
    assert submitted == []


def test_agent_submission_failure_never_reopens_or_fails_completed_ingest(
    sqlite_ledger,
) -> None:
    identity = ("2026-05", "ubist", "a" * 64)
    sqlite_ledger.receive(*identity, manifest_path="/input/manifest.json")
    sqlite_ledger.mark_running(*identity, job_name="jw-ingest-parent", run_id="run-1")
    sqlite_ledger.mark_complete(*identity, row_counts={"input.xlsx": 1})

    def fail_transport(_url_path: str, _body: dict) -> dict:
        raise RuntimeError("injected agent submit failure")

    client = TestClient(
        create_app(IngestService(sqlite_ledger, None, transport=fail_transport))
    )

    response = client.post("/ingest/terminal", json=_terminal_payload())

    assert response.status_code == 200
    assert response.json()["agent_trigger_status"] == "failed"
    assert response.json()["agent_trigger_reason"] == "RuntimeError"
    assert sqlite_ledger.status(*identity).status == "complete"


def test_failed_agent_retry_can_become_fresh_without_changing_ingest_status(
    sqlite_ledger,
) -> None:
    identity = ("2026-05", "ubist", "a" * 64)
    sqlite_ledger.receive(*identity, manifest_path="/input/manifest.json")
    sqlite_ledger.mark_running(*identity, job_name="jw-ingest-parent", run_id="run-1")
    sqlite_ledger.mark_complete(*identity, row_counts={"input.xlsx": 1})
    sqlite_ledger.record_stage(
        *identity,
        run_id="run-1:agent-refresh",
        seq=1,
        stage="agent_refresh",
        status="failed",
        reason="injected agent failure",
        started_at="2026-07-30T00:02:00+00:00",
        finished_at="2026-07-30T00:03:00+00:00",
    )

    failed = sqlite_ledger.agent_refresh_status("ubist")
    assert failed["agent_status"] == "failed"
    assert sqlite_ledger.status(*identity).status == "complete"


def test_same_epoch_replacement_is_stale_until_exact_manifest_agent_completes(
    sqlite_ledger,
) -> None:
    old_identity = ("2026-05", "ubist", "a" * 64)
    new_identity = ("2026-05", "ubist", "b" * 64)
    sqlite_ledger.receive(*old_identity, manifest_path="/input/old.json")
    sqlite_ledger.mark_running(
        *old_identity, job_name="jw-ingest-old", run_id="run-old"
    )
    sqlite_ledger.mark_complete(*old_identity, row_counts={"old.xlsx": 1})
    sqlite_ledger.record_stage(
        *old_identity,
        run_id="run-old:agent-refresh",
        seq=1,
        stage="agent_refresh",
        status="complete",
        started_at="2026-07-30T00:01:00+00:00",
        finished_at="2026-07-30T00:02:00+00:00",
    )
    sqlite_ledger.receive(*new_identity, manifest_path="/input/new.json")
    sqlite_ledger.mark_running(
        *new_identity, job_name="jw-ingest-new", run_id="run-new"
    )
    sqlite_ledger.mark_complete(*new_identity, row_counts={"new.xlsx": 1})

    status = sqlite_ledger.agent_refresh_status("ubist")

    assert status["agent_epoch"] == "2026-05"
    assert status["agent_status"] == "stale"


def test_agent_runner_records_launcher_exception_without_touching_ingest(
    sqlite_ledger, monkeypatch
) -> None:
    from pipeline.scripts.ingest_hook import agent_refresh_runner

    identity = ("2026-05", "ubist", "a" * 64)
    sqlite_ledger.receive(*identity, manifest_path="/input/manifest.json")
    sqlite_ledger.mark_running(*identity, job_name="jw-ingest-parent", run_id="run-1")
    sqlite_ledger.mark_complete(*identity, row_counts={"input.xlsx": 1})
    monkeypatch.setattr(
        agent_refresh_runner.config,
        "open_configured_ledger",
        lambda: sqlite_ledger,
    )
    monkeypatch.setattr(
        agent_refresh_runner.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected process launch failure")
        ),
    )

    assert agent_refresh_runner.run(
        epoch=identity[0],
        category=identity[1],
        manifest_sha=identity[2],
        ingest_run_id="run-1",
    ) == 1
    status = sqlite_ledger.agent_refresh_status("ubist")
    assert status["agent_status"] == "failed"
    assert sqlite_ledger.status(*identity).status == "complete"

    sqlite_ledger.record_stage(
        *identity,
        run_id="run-1:agent-refresh-retry-2",
        seq=1,
        stage="agent_refresh",
        status="complete",
        started_at="2026-07-30T00:04:00+00:00",
        finished_at="2026-07-30T00:05:00+00:00",
    )

    fresh = sqlite_ledger.agent_refresh_status("ubist")
    assert fresh == {
        "agent_epoch": "2026-05",
        "agent_status": "fresh",
        "last_success_at": "2026-07-30T00:05:00+00:00",
    }
    assert sqlite_ledger.status(*identity).status == "complete"
