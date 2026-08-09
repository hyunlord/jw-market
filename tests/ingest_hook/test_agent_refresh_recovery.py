from __future__ import annotations

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
