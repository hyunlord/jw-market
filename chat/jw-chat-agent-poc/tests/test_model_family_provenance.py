from __future__ import annotations

import pytest

from jw_chat_agent_poc.service.runtime_provenance import trace_envelope, version_payload


_MODEL_ENV_NAMES = (
    "GENOS_SERVING_ID",
    "GENOS_FINAL_SERVING_ID",
    "GENOS_PLANNER_SERVING_ID",
    "JW_CHAT_MODEL_FAMILY",
    "JW_CHAT_ROUTER_MODEL_FAMILY",
    "JW_CHAT_FINAL_MODEL_FAMILY",
    "JW_CHAT_PLANNER_MODEL_FAMILY",
)


def _set_mixed_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _MODEL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GENOS_SERVING_ID", "202")
    monkeypatch.setenv("GENOS_FINAL_SERVING_ID", "202")
    monkeypatch.setenv("GENOS_PLANNER_SERVING_ID", "190")
    monkeypatch.setenv("JW_CHAT_MODEL_FAMILY", "gemini-3-flash-preview")
    monkeypatch.setenv("JW_CHAT_ROUTER_MODEL_FAMILY", "gemini-3.1-pro-preview")
    monkeypatch.setenv("JW_CHAT_FINAL_MODEL_FAMILY", "gemini-3.1-pro-preview")
    monkeypatch.setenv("JW_CHAT_PLANNER_MODEL_FAMILY", "gemini-3-flash-preview")


def test_version_payload_reports_mixed_model_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mixed_model_env(monkeypatch)

    payload = version_payload()

    assert payload["model_family"] == "gemini-3-flash-preview"
    assert payload["model_families"] == {
        "router": "gemini-3.1-pro-preview",
        "final": "gemini-3.1-pro-preview",
        "planner": "gemini-3-flash-preview",
    }


def test_trace_envelope_reports_stage_serving_and_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mixed_model_env(monkeypatch)

    trace = trace_envelope(
        question="리바로 매출 알려줘",
        result={"markdown_response": {"fact_md": "", "data_md": ""}},
        answer="확인된 값이 없습니다.",
        charts=(),
        timing={"stages": []},
        conversation_id="model-upgrade-test",
    )

    assert trace["model_stages"] == {
        "router_serving_id": "202",
        "router_model_family": "gemini-3.1-pro-preview",
        "final_serving_id": "202",
        "final_model_family": "gemini-3.1-pro-preview",
        "planner_serving_id": "190",
        "planner_model_family": "gemini-3-flash-preview",
    }
