from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from phase_zeta_runner.config import RunnerConfig
from phase_zeta_runner.llm_runner import call_llm


@pytest.mark.integration
def test_full_dry_run_livaro_when_enabled():
    if os.environ.get("RUN_GENOS_INTEGRATION") != "1":
        pytest.skip("Set RUN_GENOS_INTEGRATION=1 inside the GKE network to call GenOS workflow 217.")

    bundle_path = Path(os.environ["PHASE_ZETA_LIVARO_BUNDLE"])
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    config = RunnerConfig.from_yaml(os.environ["PHASE_ZETA_RUNNER_CONFIG"])

    result = call_llm(bundle, config)

    assert result.success, result.error
    assert set(result.parsed_output) == {
        "phenomenon",
        "cause",
        "prediction",
        "recommendation",
    }
    assert result.model_version == "genos_workflow_217"
