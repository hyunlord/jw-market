from __future__ import annotations

import json

from pipeline.scripts.ingest_hook import agent_refresh_runner


def test_scoped_agent_refresh_recovery_does_not_recompute_forecast(
    sqlite_ledger, monkeypatch
) -> None:
    # Given an NSA retry with an already-published forecast and an exact source scope
    commands: list[list[str]] = []
    monkeypatch.setattr(
        agent_refresh_runner.config,
        "open_configured_ledger",
        lambda: sqlite_ledger,
    )
    monkeypatch.setattr(
        agent_refresh_runner,
        "resolve_affected_scope",
        lambda **_kwargs: agent_refresh_runner.ResolvedAgentScope(
            source="iqvia_nsa",
            market_ids=(),
            brand_keys=("nsa-brand",),
        ),
    )
    monkeypatch.setattr(
        agent_refresh_runner.subprocess,
        "run",
        lambda command, check: commands.append(command)
        or type("Result", (), {"returncode": 0})(),
    )

    # When recovery resumes after forecast publication
    result = agent_refresh_runner.run(
        epoch="2026-Q1",
        category="iqvia_nsa",
        manifest_sha="a" * 64,
        ingest_run_id="run-1",
        agent_run_id="run-1:agent-refresh-recovery",
        reuse_forecast_staging=True,
        affected_scope={"dimension": "source", "count": 1, "values": ["iqvia_nsa"]},
    )

    # Then only downstream stages run and planner force cannot reach forecast
    assert result == 0
    assert "--profile" not in commands[0]
    assert commands[0][commands[0].index("--stages") + 1] == "strength,shortlong,elements"
    assert "--force" in commands[0]


def test_agent2_recovery_reuses_agent3_and_records_unknown_brand_skips(
    sqlite_ledger, monkeypatch, tmp_path
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        agent_refresh_runner.config,
        "open_configured_ledger",
        lambda: sqlite_ledger,
    )
    monkeypatch.setattr(
        agent_refresh_runner,
        "resolve_affected_scope",
        lambda **_kwargs: agent_refresh_runner.ResolvedAgentScope(
            source="iqvia_nsa",
            market_ids=(),
            brand_keys=("nsa-brand",),
        ),
    )
    monkeypatch.setattr(agent_refresh_runner, "AGENT2_OUTPUT_ROOT", tmp_path)

    def capture_command(command: list[str], *, check: bool):
        commands.append(command)
        run_id = command[command.index("--run-id") + 1]
        for variant in ("short", "long"):
            output_dir = tmp_path / f"orchestrated_{run_id}_{variant}"
            output_dir.mkdir(parents=True)
            (output_dir / "run_manifest.json").write_text(
                json.dumps(
                    {
                        "diagnostics": {
                            "density_worklist": {
                                "unmatched_unknown": [
                                    "레미닐피알서방",
                                    "아토르바스타틴대웅바이오",
                                    "아트맥콤비젤",
                                    "오티렌f",
                                    "종근당글리아티린",
                                    "종근당자누비아",
                                    "카나브젯",
                                ]
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(agent_refresh_runner.subprocess, "run", capture_command)

    result = agent_refresh_runner.run(
        epoch="2026-Q1",
        category="iqvia_nsa",
        manifest_sha="a" * 64,
        ingest_run_id="run-1",
        agent_run_id="run-1:agent-refresh-recovery-agent2",
        reuse_forecast_staging=True,
        resume_from_agent2=True,
        affected_scope={"dimension": "source", "count": 1, "values": ["iqvia_nsa"]},
    )

    assert result == 0
    assert commands[0][commands[0].index("--stages") + 1] == "shortlong,elements"
    assert "strength" not in commands[0]
    rows = sqlite_ledger.stage_events("2026-Q1", "iqvia_nsa", "a" * 64)
    assert [(row.seq, row.stage, row.status) for row in rows] == [
        (1, "agent_refresh", "complete"),
        (2, "agent3", "complete"),
        (3, "agent2", "complete"),
        (4, "dashboard", "complete"),
    ]
    assert "reused prior successful strength stage" in rows[1].reason
    assert "skipped_unknown=7" in rows[2].reason
    assert "레미닐피알서방" in rows[2].reason
