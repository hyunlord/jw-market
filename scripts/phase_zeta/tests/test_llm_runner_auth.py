from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from phase_zeta_runner.config import RunnerConfig
from phase_zeta_runner.llm_runner import _init_vertex_ai


def test_runner_config_uses_workload_identity():
    config_path = Path(__file__).resolve().parents[1] / "configs" / "gemini_runner_v1.yaml"
    config = RunnerConfig.from_yaml(config_path)

    assert config.auth.method == "workload_identity"
    assert not hasattr(config.auth, "credentials_path")


def test_init_vertex_ai_does_not_pass_explicit_credentials(monkeypatch):
    calls = []

    def fake_init(**kwargs):
        calls.append(kwargs)

    monkeypatch.setitem(sys.modules, "vertexai", SimpleNamespace(init=fake_init))

    config = RunnerConfig.default_for_tests()
    _init_vertex_ai(config)

    assert calls == [
        {
            "project": config.llm.project_id,
            "location": config.llm.region,
        }
    ]
