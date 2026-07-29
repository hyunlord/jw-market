from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from jw_chat_agent_poc.genos_config import (
    resolve_deep_genos_base_url,
    resolve_final_genos_base_url,
    resolve_genos_base_url,
    resolve_planner_genos_base_url,
)
from jw_chat_agent_poc.service.runtime_provenance import trace_envelope, version_payload


_DEPLOY_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "model_upgrade_router_final_r2_patch.py"
)
_MODEL_ENV_NAMES = (
    "GENOS_SERVING_ID",
    "GENOS_FINAL_SERVING_ID",
    "GENOS_PLANNER_SERVING_ID",
    "GENOS_DEEP_SERVING_ID",
    "JW_CHAT_MODEL_FAMILY",
    "JW_CHAT_ROUTER_MODEL_FAMILY",
    "JW_CHAT_FINAL_MODEL_FAMILY",
    "JW_CHAT_PLANNER_MODEL_FAMILY",
)


def _clear_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _MODEL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _load_deploy_script() -> ModuleType:
    assert _DEPLOY_SCRIPT.is_file(), f"missing CAS patch generator: {_DEPLOY_SCRIPT}"
    spec = importlib.util.spec_from_file_location("model_upgrade_router_final_r2_patch", _DEPLOY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _deployment(env: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "metadata": {"resourceVersion": "rv-1186"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "sidecar", "env": []},
                        {"name": "app", "image": "example.invalid/chat@sha256:old", "env": env},
                    ]
                }
            }
        },
    }


def _live_model_env() -> list[dict[str, str]]:
    return [
        {"name": "UNRELATED", "value": "preserve-me"},
        {"name": "GENOS_SERVING_ID", "value": "190"},
        {"name": "GENOS_FINAL_SERVING_ID", "value": "190"},
        {"name": "GENOS_PLANNER_SERVING_ID", "value": "190"},
        {"name": "GENOS_DEEP_SERVING_ID", "value": "202"},
        {"name": "JW_CHAT_MODEL_FAMILY", "value": "gemini-3-flash-preview"},
    ]


def test_current_live_env_resolves_router_and_final_to_190(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("GENOS_BASE_URL", "https://example.test/rep/serving/517")
    monkeypatch.setenv("GENOS_SERVING_ID", "190")
    monkeypatch.setenv("GENOS_FINAL_SERVING_ID", "190")
    monkeypatch.setenv("GENOS_PLANNER_SERVING_ID", "190")
    monkeypatch.setenv("GENOS_DEEP_SERVING_ID", "202")

    assert resolve_genos_base_url().endswith("/serving/190")
    assert resolve_final_genos_base_url().endswith("/serving/190")
    assert resolve_planner_genos_base_url().endswith("/serving/190")
    assert resolve_deep_genos_base_url().endswith("/serving/202")


def test_target_env_resolves_only_router_and_final_to_202(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("GENOS_BASE_URL", "https://example.test/rep/serving/190")
    monkeypatch.setenv("GENOS_SERVING_ID", "202")
    monkeypatch.setenv("GENOS_FINAL_SERVING_ID", "202")
    monkeypatch.setenv("GENOS_PLANNER_SERVING_ID", "190")
    monkeypatch.setenv("GENOS_DEEP_SERVING_ID", "202")

    assert resolve_genos_base_url().endswith("/serving/202")
    assert resolve_final_genos_base_url().endswith("/serving/202")
    assert resolve_planner_genos_base_url().endswith("/serving/190")
    assert resolve_deep_genos_base_url().endswith("/serving/202")


def test_runtime_provenance_reports_mixed_model_families(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("JW_CHAT_MODEL_FAMILY", "gemini-3-flash-preview")
    monkeypatch.setenv("JW_CHAT_ROUTER_MODEL_FAMILY", "gemini-3.1-pro-preview")
    monkeypatch.setenv("JW_CHAT_FINAL_MODEL_FAMILY", "gemini-3.1-pro-preview")
    monkeypatch.setenv("JW_CHAT_PLANNER_MODEL_FAMILY", "gemini-3-flash-preview")
    monkeypatch.setenv("GENOS_SERVING_ID", "202")
    monkeypatch.setenv("GENOS_FINAL_SERVING_ID", "202")
    monkeypatch.setenv("GENOS_PLANNER_SERVING_ID", "190")

    payload = version_payload()

    assert payload["model_family"] == "gemini-3-flash-preview"
    assert payload["model_families"] == {
        "router": "gemini-3.1-pro-preview",
        "final": "gemini-3.1-pro-preview",
        "planner": "gemini-3-flash-preview",
    }


def test_trace_envelope_reports_stage_serving_and_family(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("JW_CHAT_MODEL_FAMILY", "gemini-3-flash-preview")
    monkeypatch.setenv("JW_CHAT_ROUTER_MODEL_FAMILY", "gemini-3.1-pro-preview")
    monkeypatch.setenv("JW_CHAT_FINAL_MODEL_FAMILY", "gemini-3.1-pro-preview")
    monkeypatch.setenv("JW_CHAT_PLANNER_MODEL_FAMILY", "gemini-3-flash-preview")
    monkeypatch.setenv("GENOS_SERVING_ID", "202")
    monkeypatch.setenv("GENOS_FINAL_SERVING_ID", "202")
    monkeypatch.setenv("GENOS_PLANNER_SERVING_ID", "190")

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


def test_cas_patch_replaces_only_router_final_and_adds_stage_families() -> None:
    module = _load_deploy_script()
    original_env = _live_model_env()

    patch = module.build_model_upgrade_patch(_deployment(original_env), container_name="app")

    assert patch[:3] == [
        {"op": "test", "path": "/metadata/resourceVersion", "value": "rv-1186"},
        {"op": "test", "path": "/spec/template/spec/containers/1/name", "value": "app"},
        {
            "op": "test",
            "path": "/spec/template/spec/containers/1/env",
            "value": original_env,
        },
    ]
    assert patch[3]["op"] == "replace"
    updated = {item["name"]: item["value"] for item in patch[3]["value"]}
    assert updated == {
        "UNRELATED": "preserve-me",
        "GENOS_SERVING_ID": "202",
        "GENOS_FINAL_SERVING_ID": "202",
        "GENOS_PLANNER_SERVING_ID": "190",
        "GENOS_DEEP_SERVING_ID": "202",
        "JW_CHAT_MODEL_FAMILY": "gemini-3-flash-preview",
        "JW_CHAT_ROUTER_MODEL_FAMILY": "gemini-3.1-pro-preview",
        "JW_CHAT_FINAL_MODEL_FAMILY": "gemini-3.1-pro-preview",
        "JW_CHAT_PLANNER_MODEL_FAMILY": "gemini-3-flash-preview",
    }


def test_cas_patch_rejects_unexpected_live_model_env() -> None:
    module = _load_deploy_script()
    drifted = _live_model_env()
    drifted[1] = {"name": "GENOS_SERVING_ID", "value": "191"}

    with pytest.raises(ValueError, match="GENOS_SERVING_ID"):
        module.build_model_upgrade_patch(_deployment(drifted), container_name="app")
