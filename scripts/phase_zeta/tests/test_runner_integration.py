from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from phase_zeta_runner.config import RunnerConfig
from phase_zeta_runner.llm_runner import call_gemini
from phase_zeta_runner.prompt_builder import build_unified_prompt


@pytest.mark.integration
def test_full_dry_run_livaro_when_enabled():
    if os.environ.get("RUN_VERTEX_INTEGRATION") != "1":
        pytest.skip("Set RUN_VERTEX_INTEGRATION=1 to call Vertex AI.")

    bundle_path = Path(os.environ["PHASE_ZETA_LIVARO_BUNDLE"])
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    config = RunnerConfig.from_yaml(os.environ["PHASE_ZETA_RUNNER_CONFIG"])

    prompt = build_unified_prompt(bundle, config)
    result = call_gemini(prompt, config)

    assert result.success, result.error
    assert set(result.parsed_output) == {
        "phenomenon",
        "cause",
        "prediction",
        "recommendation",
    }
    assert result.cost_usd < 0.10
