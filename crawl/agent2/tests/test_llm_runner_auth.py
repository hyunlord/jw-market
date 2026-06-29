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


def test_genos_output_requires_operational_four_bullets_per_stage():
    def stage_with_bullets(count: int) -> dict[str, str | list[str]]:
        return {"title": "t", "body": "b", "bullets": [str(index) for index in range(count)]}

    four_bullet_output = {
        "phenomenon": stage_with_bullets(4),
        "cause": stage_with_bullets(4),
        "prediction": stage_with_bullets(4),
        "recommendation": stage_with_bullets(4),
    }
    three_bullet_output = {
        "phenomenon": stage_with_bullets(3),
        "cause": stage_with_bullets(3),
        "prediction": stage_with_bullets(3),
        "recommendation": stage_with_bullets(3),
    }

    assert genos_caller.validate_genos_output(four_bullet_output)["valid"]
    result = genos_caller.validate_genos_output(three_bullet_output)
    assert not result["valid"]
    assert "expected exactly 4" in "; ".join(result["errors"])


def test_call_llm_routes_to_genos(monkeypatch):
    captured = {}

    def fake_call(question: str, config: RunnerConfig):
        captured["question"] = question
        captured["workflow_id"] = config.genos.workflow_id
        return {
            "success": True,
            "parsed_output": {
                "phenomenon": {"title": "t", "body": "b", "bullets": ["a", "b", "c", "d"]},
                "cause": {"title": "t", "body": "b", "bullets": ["a", "b", "c", "d"]},
                "prediction": {"title": "t", "body": "b", "bullets": ["a", "b", "c", "d"]},
                "recommendation": {"title": "t", "body": "b", "bullets": ["a", "b", "c", "d"]},
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
