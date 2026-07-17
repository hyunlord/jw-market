"""ETL -> orchestrator kick contract (event-driven round).

Key guarantees: opt-in only, success-path only, duplicate no-op, kick failure
never fails an already-successful load.
"""

from __future__ import annotations

import json

import pytest

from pipeline.etl import run as etl_run
from pipeline.etl.kick import (
    KICK_CRONJOB_ENV,
    KICK_ENV,
    KICK_MARKER_ENV,
    kick_job_name,
    maybe_kick_orchestrator,
)


def _env(tmp_path, **extra):
    env = {KICK_ENV: "1", KICK_MARKER_ENV: str(tmp_path / "marker.json")}
    env.update(extra)
    return env


def test_disabled_without_env(tmp_path):
    calls = []
    result = maybe_kick_orchestrator({}, runner=lambda argv: calls.append(argv) or (0, ""), env={})

    assert result == {"kick": "disabled"}
    assert calls == []


def test_kick_creates_dated_job_and_marker(tmp_path):
    calls = []
    env = _env(tmp_path, **{KICK_CRONJOB_ENV: "jw-pipeline-orchestrator-poll-daily"})

    result = maybe_kick_orchestrator(
        {"mode": "all", "period": None, "source": "ubist", "incremental": True},
        runner=lambda argv: calls.append(argv) or (0, "job created"),
        env=env,
    )

    assert result["kick"] == "created"
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:5] == ["kubectl", "-n", "llmops", "create", "job"]
    assert argv[5] == kick_job_name()
    assert argv[6] == "--from=cronjob/jw-pipeline-orchestrator-poll-daily"
    marker = json.loads((tmp_path / "marker.json").read_text())
    assert marker["incremental"] is True and marker["source"] == "ubist"


def test_duplicate_kick_is_noop(tmp_path):
    result = maybe_kick_orchestrator(
        {}, runner=lambda argv: (1, 'jobs.batch "jw-orch-kick" AlreadyExists'), env=_env(tmp_path)
    )

    assert result["kick"] == "noop_already_exists"


def test_kick_failure_never_raises(tmp_path):
    def boom(argv):
        raise RuntimeError("kubectl missing")

    result = maybe_kick_orchestrator({}, runner=boom, env=_env(tmp_path))

    assert result["kick"] == "error"


def _run_main_with_stages(monkeypatch, tmp_path, stages, kick_env: bool):
    class FakeStage:
        def __init__(self, name, rc):
            self.STAGE = name
            self._rc = rc

        def run(self, params):
            return self._rc

    fake = [FakeStage(name, rc) for name, rc in stages]
    monkeypatch.setattr(etl_run, "STAGES", fake)
    kicks = []
    monkeypatch.setattr("pipeline.etl.kick.maybe_kick_orchestrator", lambda params, **kw: kicks.append(params) or {})
    if kick_env:
        monkeypatch.setenv(KICK_ENV, "1")
        monkeypatch.setenv(KICK_MARKER_ENV, str(tmp_path / "m.json"))
    else:
        monkeypatch.delenv(KICK_ENV, raising=False)
    rc = etl_run.main(["--all"])
    return rc, kicks


def test_main_kicks_only_after_full_success(monkeypatch, tmp_path):
    rc, kicks = _run_main_with_stages(
        monkeypatch, tmp_path, [("s0 fake", 0), ("s1 fake", 0)], kick_env=True
    )

    assert rc == 0
    assert len(kicks) == 1


def test_main_does_not_kick_on_stage_failure(monkeypatch, tmp_path):
    rc, kicks = _run_main_with_stages(
        monkeypatch, tmp_path, [("s0 fake", 0), ("s1 fake", 3), ("s2 fake", 0)], kick_env=True
    )

    assert rc == 3
    assert kicks == []
