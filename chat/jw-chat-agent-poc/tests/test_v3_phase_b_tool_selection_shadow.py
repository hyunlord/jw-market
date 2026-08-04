from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from jw_chat_agent_poc.orchestrator.shadow_gate_runtime import (
    current_shadow_request_id,
    shadow_request_scope,
)
from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import SessionStore
from jw_chat_agent_poc.tool_use.catalog import TOOL_DESCRIPTION_CATALOG
from jw_chat_agent_poc.tool_use.v3_intent import extract_intent_frame
from jw_chat_agent_poc.tool_use.v3_selection import (
    MultiToolChoice,
    V3ToolSelector,
    selection_tool_specs,
)
from jw_chat_agent_poc.tool_use.v3_selection_shadow import run_v3_selection_shadow_once
from jw_chat_agent_poc.tool_use.v3_selection_provider import (
    GenosV3ToolChoiceProvider,
)

from test_service import _fake_agent_factory, _market_scope_resolver


class _CapturingProvider:
    def __init__(self, choices: tuple[MultiToolChoice, ...]) -> None:
        self.choices = choices
        self.calls: list[dict[str, object]] = []

    def choose_many(self, *, user_text: str, messages: list[dict], tools: list[dict]):
        self.calls.append({"user_text": user_text, "messages": messages, "tools": tools})
        return self.choices


def test_intent_frame_extracts_independent_domains_axes_and_presentation() -> None:
    frame = extract_intent_frame(
        "리바로와 뇌경색 임상시험 허가 현황을 IQVIA 전략뷰 기준 차트로 비교해줘"
    )

    assert set(frame.domains) == {"market", "regulatory", "clinical"}
    assert "compare" in frame.operations
    assert frame.axes.source == "IQVIA"
    assert frame.axes.view == "strategic"
    assert frame.presentation == "chart"


def test_intent_frame_extraction_is_fail_open() -> None:
    frame = extract_intent_frame(None)  # type: ignore[arg-type]

    assert frame.domains == ()
    assert frame.operations == ()
    assert frame.entities == ()
    assert frame.presentation == "text"


def test_selector_exposes_all_catalog_tools_and_only_reorders_candidates() -> None:
    provider = _CapturingProvider(())
    selector = V3ToolSelector(provider=provider)

    result = selector.select("리바로 최근 매출 추이")

    expected_names = tuple(record.name for record in TOOL_DESCRIPTION_CATALOG)
    assert len(expected_names) == 33
    assert len(result.candidate_names) == 33
    assert set(result.candidate_names) == set(expected_names)
    assert result.candidate_names[0].startswith("market.")
    assert len(provider.calls) == 1
    exposed_names = tuple(
        tool["function"]["name"] for tool in provider.calls[0]["tools"]  # type: ignore[index]
    )
    assert exposed_names == result.candidate_names


def test_selector_uses_catalog_description_and_allows_multiple_choices() -> None:
    choices = (
        MultiToolChoice("clinicaltrials_v2_search", {"query": "뇌경색"}, "call-1"),
        MultiToolChoice("mfds_permission_search", {"brand": "뇌경색"}, "call-2"),
    )
    provider = _CapturingProvider(choices)
    result = V3ToolSelector(provider=provider).select(
        "뇌경색 관련 임상시험이랑 허가 현황 알려줘"
    )

    assert result.choices == choices
    system_prompt = provider.calls[0]["messages"][0]["content"]  # type: ignore[index]
    assert "zero or more" in system_prompt
    assert "at most one" not in system_prompt.casefold()
    descriptions = {
        tool["function"]["name"]: tool["function"]["description"]
        for tool in provider.calls[0]["tools"]  # type: ignore[index]
    }
    catalog = {record.name: record for record in TOOL_DESCRIPTION_CATALOG}
    assert descriptions["mfds_permission_search"] == catalog[
        "mfds_permission_search"
    ].catalog_description
    assert "does NOT return" in descriptions["mfds_permission_search"]


def test_unknown_tool_name_is_preserved_without_mapping() -> None:
    unknown = MultiToolChoice("meta.nonexistent", {"query": "why"}, "unknown-1")
    result = V3ToolSelector(provider=_CapturingProvider((unknown,))).select(
        "이 시장 정의가 왜 바뀌었어?"
    )

    assert result.choices == (unknown,)
    assert result.unknown_tool_names == ("meta.nonexistent",)


def test_selector_enforces_eight_call_cap_without_singleton_restriction() -> None:
    choices = tuple(
        MultiToolChoice(f"unknown.tool.{index}", {}, f"call-{index}")
        for index in range(10)
    )
    result = V3ToolSelector(provider=_CapturingProvider(choices)).select("복합 질문")

    assert len(result.choices) == 8
    assert result.provider_choice_count == 10
    assert result.unknown_tool_names == tuple(
        f"unknown.tool.{index}" for index in range(8)
    )


def test_genos_provider_requests_parallel_calls_and_returns_every_tool_call(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "a",
                                    "function": {
                                        "name": "clinicaltrials_v2_search",
                                        "arguments": '{"query":"뇌경색"}',
                                    },
                                },
                                {
                                    "id": "b",
                                    "function": {
                                        "name": "mfds_permission_search",
                                        "arguments": '{"brand":"리바로"}',
                                    },
                                },
                            ]
                        }
                    }
                ]
            }

    def post(url, *, headers, json, timeout):
        captured.update(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return _Response()

    monkeypatch.setattr(
        "jw_chat_agent_poc.tool_use.v3_selection_provider.requests.post",
        post,
    )
    choices = GenosV3ToolChoiceProvider(
        base_url="https://planner.invalid",
        token="fixture",
    ).choose_many(
        user_text="ignored",
        messages=[{"role": "user", "content": "복합 질문"}],
        tools=[{"type": "function", "function": {"name": "test"}}],
    )

    assert [choice.name for choice in choices] == [
        "clinicaltrials_v2_search",
        "mfds_permission_search",
    ]
    assert captured["json"]["parallel_tool_calls"] is True  # type: ignore[index]


def test_internal_tools_have_selection_schemas_but_no_executor() -> None:
    specs = {spec.name: spec for spec in selection_tool_specs()}

    assert specs["market.get_brand_metric"].input_model.model_fields.keys() == {
        "brand",
        "metric",
        "period",
        "market",
        "source",
        "history_points",
    }
    assert specs["market.compare_brands"].input_model.model_fields.keys() == {
        "brand",
        "comparison_brand",
        "market",
        "metric",
    }
    assert specs["file.get_schema"].input_model.model_fields.keys() == {
        "conversation_id",
        "sources",
    }
    assert all(not hasattr(spec, "execute") for spec in specs.values())


def test_selection_schema_loading_does_not_import_internal_executors() -> None:
    script = """
import sys

class InternalAdapterImportBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "jw_chat_agent_poc.tool_use.internal_adapters":
            raise AssertionError(f"unexpected executor import: {fullname}")
        return None

sys.meta_path.insert(0, InternalAdapterImportBlocker())
from jw_chat_agent_poc.tool_use.v3_selection import selection_tool_specs

specs = selection_tool_specs()
if any(hasattr(spec, "execute") for spec in specs):
    raise SystemExit("selection-only spec exposed an executor")
"""
    project_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        cwd=project_root,
        env={**os.environ, "PYTHONPATH": str(project_root)},
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_shadow_observation_binds_request_id_and_never_executes_tools(caplog) -> None:
    selector = V3ToolSelector(
        provider=_CapturingProvider(
            (MultiToolChoice("market.get_brand_metric", {"brand": "리바로"}, "c1"),)
        )
    )
    observed: dict[str, object] = {}

    @shadow_request_scope
    def run() -> None:
        observed["legacy_request_id"] = current_shadow_request_id()
        observed.update(
            run_v3_selection_shadow_once(
                "리바로 매출",
                selector=selector,
            )
        )

    with caplog.at_level(
        logging.INFO,
        logger="jw_chat_agent_poc.tool_use.v3_selection_shadow",
    ):
        run()

    assert observed["event"] == "v3_tool_selection_shadow"
    assert observed["request_id"]
    assert observed["request_id"] == observed["legacy_request_id"]
    assert observed["selected_tools"] == ["market.get_brand_metric"]
    assert observed["internal_tool_execution_count"] == 0
    assert observed["answer_action"] == "unchanged"
    assert any(observed["request_id"] in record.getMessage() for record in caplog.records)


def test_flag_off_does_not_import_or_call_v3_shadow_runtime(monkeypatch) -> None:
    module_name = "jw_chat_agent_poc.tool_use.v3_selection_shadow"
    monkeypatch.delenv("JW_CHAT_V3_TOOL_SELECTION_SHADOW", raising=False)
    sys.modules.pop(module_name, None)

    response = service_app._answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "리바로 매출 알려줘",
        "fixture",
        None,
    )

    assert response["result"]["answer"] == "fallback:리바로 매출 알려줘"
    assert module_name not in sys.modules


def test_flag_off_fresh_process_imports_no_v3_selection_modules() -> None:
    script = """
import os
import sys

os.environ["JW_CHAT_V3_TOOL_SELECTION_SHADOW"] = "off"

attempts = []
class V3ImportBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("jw_chat_agent_poc.tool_use.v3_"):
            attempts.append(fullname)
            raise AssertionError(f"unexpected V3 import attempt: {fullname}")
        return None

sys.meta_path.insert(0, V3ImportBlocker())
from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import SessionStore
from test_service import _fake_agent_factory, _market_scope_resolver

service_app._answer_question(
    SessionStore(),
    _market_scope_resolver(),
    _fake_agent_factory,
    "리바로 매출 알려줘",
    "fixture",
    None,
)
if attempts:
    raise SystemExit(f"unexpected V3 import attempts: {attempts}")
loaded = sorted(
    name
    for name in sys.modules
    if name.startswith("jw_chat_agent_poc.tool_use.v3_")
)
if loaded:
    raise SystemExit(f"unexpected V3 imports: {loaded}")
"""
    project_root = Path(__file__).resolve().parents[1]
    python_path = os.pathsep.join((str(project_root), str(project_root / "tests")))

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        cwd=project_root,
        env={**os.environ, "PYTHONPATH": python_path},
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_flag_off_does_not_call_preloaded_v3_shadow_runtime(monkeypatch) -> None:
    calls = 0

    def counted_start(*_args, **_kwargs) -> None:
        nonlocal calls
        calls += 1

    module_name = "jw_chat_agent_poc.tool_use.v3_selection_shadow"
    monkeypatch.setitem(
        sys.modules,
        module_name,
        SimpleNamespace(start_v3_selection_shadow=counted_start),
    )
    monkeypatch.setenv("JW_CHAT_V3_TOOL_SELECTION_SHADOW", "off")

    service_app._answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "리바로 매출 알려줘",
        "fixture",
        None,
    )

    assert calls == 0


def test_shadow_start_failure_is_logged_and_answer_bytes_stay_unchanged(
    monkeypatch,
    caplog,
) -> None:
    baseline = service_app._answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "리바로 매출 알려줘",
        "fixture",
        "v3-shadow-baseline",
    )
    module_name = "jw_chat_agent_poc.tool_use.v3_selection_shadow"
    fake_module = SimpleNamespace(
        start_v3_selection_shadow=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic v3 shadow failure")
        )
    )
    monkeypatch.setitem(sys.modules, module_name, fake_module)
    monkeypatch.setenv("JW_CHAT_V3_TOOL_SELECTION_SHADOW", "1")

    with caplog.at_level(
        logging.ERROR,
        logger="jw_chat_agent_poc.orchestrator.query_spec",
    ):
        actual = service_app._answer_question(
            SessionStore(),
            _market_scope_resolver(),
            _fake_agent_factory,
            "리바로 매출 알려줘",
            "fixture",
            "v3-shadow-actual",
        )

    assert actual["result"]["answer"].encode() == baseline["result"]["answer"].encode()
    assert any("v3_tool_selection_shadow_start_failed" in row.message for row in caplog.records)


def test_flag_on_starts_shadow_with_request_id_without_changing_answer(
    monkeypatch,
) -> None:
    baseline = service_app._answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "리바로 매출 알려줘",
        "fixture",
        "v3-shadow-success-baseline",
    )
    observed: dict[str, object] = {}

    def capture_start(question: str, *, request_id: str | None = None) -> None:
        observed.update({"question": question, "request_id": request_id})

    monkeypatch.setitem(
        sys.modules,
        "jw_chat_agent_poc.tool_use.v3_selection_shadow",
        SimpleNamespace(start_v3_selection_shadow=capture_start),
    )
    monkeypatch.setenv("JW_CHAT_V3_TOOL_SELECTION_SHADOW", "1")

    actual = service_app._answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "리바로 매출 알려줘",
        "fixture",
        "v3-shadow-success-actual",
    )

    assert actual["result"]["answer"].encode() == baseline["result"]["answer"].encode()
    assert observed == {
        "question": "리바로 매출 알려줘",
        "request_id": actual.shadow_request_id,
    }
    assert observed["request_id"]


def test_shadow_event_is_json_serializable() -> None:
    selector = V3ToolSelector(provider=_CapturingProvider(()))
    payload = run_v3_selection_shadow_once("도구가 필요한가?", selector=selector)

    assert json.loads(json.dumps(payload, ensure_ascii=False))["candidate_count"] == 33
