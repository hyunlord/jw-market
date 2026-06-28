from __future__ import annotations

import json
import sys
from pathlib import Path

import phase_zeta_runner.genos_caller as genos_caller
from phase_zeta_runner.config import RunnerConfig
from phase_zeta_runner.llm_runner import call_llm


def test_runner_config_uses_genos_workflow_217():
    config_path = Path(__file__).resolve().parents[1] / "configs" / "genos_runner_v1.yaml"
    config = RunnerConfig.from_yaml(config_path)

    assert config.genos.workflow_id == 217
    assert config.genos.endpoint_path == "/run/v2"
    assert config.genos.request_payload_mode == "root_question"


def test_llm_runner_has_no_vertex_dependency_loaded():
    assert "vertexai" not in sys.modules


def test_call_llm_routes_to_genos(monkeypatch):
    captured = {}

    def fake_call(question: str, config: RunnerConfig):
        captured["question"] = question
        captured["workflow_id"] = config.genos.workflow_id
        return {
            "success": True,
            "parsed_output": {
                "phenomenon": {"title": "t", "body": "b", "bullets": ["a", "b"]},
                "cause": {"title": "t", "body": "b", "bullets": ["a", "b"]},
                "prediction": {"title": "t", "body": "b", "bullets": ["a", "b"]},
                "recommendation": {"title": "t", "body": "b", "bullets": ["a", "b"]},
            },
            "raw_response": json.dumps({"ok": True}),
            "tokens_in": 10,
            "tokens_out": 20,
            "duration_sec": 1.0,
            "error": None,
        }

    monkeypatch.setattr(genos_caller, "call_genos_workflow", fake_call)
    import phase_zeta_runner.llm_runner as llm_runner

    monkeypatch.setattr(llm_runner, "call_genos_workflow", fake_call)
    result = call_llm({"brand_context": {"name": "리바로"}}, RunnerConfig.default_for_tests())

    assert result.success
    assert result.model_version == "genos_workflow_217"
    assert captured["workflow_id"] == 217
    assert "리바로" in captured["question"]
