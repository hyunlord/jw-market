from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "genos_determinism_probe.py"
SPEC = importlib.util.spec_from_file_location("genos_determinism_probe", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_request_uses_fixed_prompt_and_only_declared_generation_parameters() -> None:
    request = MODULE.build_request({"seed": 17, "top_p": 0.2})

    assert request["messages"][0]["content"] == MODULE.JUDGE_SYSTEM_PROMPT
    assert request["messages"][1]["content"] == MODULE.SYNTHETIC_INPUT
    assert request["temperature"] == 0
    assert request["seed"] == 17
    assert request["top_p"] == 0.2
    assert request["stream"] is False


def test_result_record_keeps_raw_provider_output_and_metadata_without_token() -> None:
    payload = {
        "id": "response-1",
        "model": "genos/202/gemini-3.1-pro-preview",
        "choices": [{"message": {"content": "DENY"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        "system_fingerprint": "revision-a",
    }

    record = MODULE.success_record(
        experiment="baseline",
        run=1,
        parameters={},
        started_at="2026-08-05T00:00:00Z",
        elapsed_ms=100.0,
        status_code=200,
        payload=payload,
    )

    assert record["raw_model_output"] == "DENY"
    assert record["response_metadata"]["model"] == payload["model"]
    assert record["response_metadata"]["system_fingerprint"] == "revision-a"
    assert record["response_metadata"]["usage"] == payload["usage"]
    serialized = str(record).lower()
    assert "authorization" not in serialized
    assert "access_token" not in serialized
    assert "password" not in serialized


def test_call_budget_rejects_the_three_hundred_and_first_call() -> None:
    budget = MODULE.CallBudget(limit=300)
    for _ in range(300):
        budget.take()

    with pytest.raises(MODULE.CallBudgetExceeded):
        budget.take()


def test_summary_distinguishes_http_acceptance_from_output_application() -> None:
    records = [
        {"accepted": True, "raw_model_output": "DENY"},
        {"accepted": True, "raw_model_output": "ALLOW"},
    ]

    summary = MODULE.summarize(records)

    assert summary["accepted_calls"] == 2
    assert summary["unique_raw_outputs"] == 2
    assert summary["all_outputs_identical"] is False


def test_cli_exposes_explicit_stop_on_four_xx_option(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe", "--experiment", "seed", "--output", "/tmp/out.json", "--stop-on-rejection"],
    )

    args = MODULE.parse_args()

    assert args.stop_on_rejection is True
