from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event

from pydantic import BaseModel, ConfigDict

from jw_chat_agent_poc.tool_use.contracts import ToolEnvelope
from jw_chat_agent_poc.tool_use.specs import ToolSpec
from jw_chat_agent_poc.tool_use.v3_execution import (
    V3ShadowToolExecutor,
    external_executable_tools,
    internal_executable_tools,
)
from jw_chat_agent_poc.tool_use.v3_execution_shadow import (
    run_v3_execution_shadow_once,
    start_v3_execution_shadow,
)
from jw_chat_agent_poc.tool_use.v3_intent import IntentFrame
from jw_chat_agent_poc.tool_use.v3_selection import (
    MultiToolChoice,
    V3SelectionResult,
)
from jw_chat_agent_poc.tool_use import v3_selection_shadow


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: int


class _InternalRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def names(self) -> tuple[str, ...]:
        return ("market.get_brand_metric", "file.query")

    def execute(self, name: str, arguments: dict[str, object]) -> object:
        self.calls.append((name, arguments))
        return {"tool": name, "arguments": arguments}


def _choice(name: str, arguments: dict[str, object]) -> MultiToolChoice:
    return MultiToolChoice(name=name, arguments=arguments)


def test_external_specs_reuse_validation_and_execution_contract() -> None:
    observed: list[int] = []

    def execute(payload: BaseModel) -> ToolEnvelope:
        parsed = _Input.model_validate(payload)
        observed.append(parsed.value)
        return ToolEnvelope(
            ok=True,
            preview="ok",
            evidence=(),
            raw={"value": parsed.value},
            error_code=None,
            error_message=None,
        )

    tools = external_executable_tools(
        (ToolSpec("hira_test", "fixture", _Input, execute, 1.0, ("external",)),)
    )
    bundle = V3ShadowToolExecutor(tools=tools).execute(
        (_choice("hira_test", {"value": "7"}),)
    )

    assert observed == [7]
    assert bundle.executions[0].tool_name == "hira_test"


def test_internal_tools_execute_only_through_shadow_registry() -> None:
    registry = _InternalRegistry()
    tools = internal_executable_tools(registry, timeout_s=1.0)

    bundle = V3ShadowToolExecutor(tools=tools).execute(
        (
            _choice(
                "market.get_brand_metric",
                {"brand": "리바로", "metric": "sales"},
            ),
        )
    )

    assert registry.calls == [
        ("market.get_brand_metric", {"brand": "리바로", "metric": "sales"})
    ]
    assert bundle.executed_call_count == 1


def test_internal_execution_validates_schema_before_adapter_call() -> None:
    registry = _InternalRegistry()
    tools = internal_executable_tools(registry, timeout_s=1.0)

    bundle = V3ShadowToolExecutor(tools=tools).execute(
        (_choice("market.get_brand_metric", {"brand": "리바로"}),)
    )

    assert registry.calls == []
    assert bundle.executions == ()
    assert bundle.failures[0].error_type == "ValidationError"


def test_execution_shadow_fails_open_without_generating_answer(monkeypatch) -> None:
    monkeypatch.setenv("JW_CHAT_V3_TOOL_EXECUTION_SHADOW", "1")
    selection = V3SelectionResult(
        intent=IntentFrame(),
        candidate_names=("market.get_brand_metric",),
        choices=(_choice("market.get_brand_metric", {"brand": "리바로"}),),
        unknown_tool_names=(),
        provider_choice_count=1,
    )

    payload = run_v3_execution_shadow_once(
        "리바로 매출",
        selection,
        executor_factory=lambda _question: (_ for _ in ()).throw(
            RuntimeError("synthetic executor construction failure")
        ),
    )

    assert payload["status"] == "error"
    assert payload["error_name"] == "RuntimeError"
    assert payload["consumed_by_serving_path"] is False
    assert payload["answer_generation_count"] == 0


def test_execution_shadow_starts_on_detached_daemon(monkeypatch) -> None:
    monkeypatch.setenv("JW_CHAT_V3_TOOL_EXECUTION_SHADOW", "1")
    started = Event()
    release = Event()
    selection = V3SelectionResult(
        intent=IntentFrame(),
        candidate_names=("market.get_brand_metric",),
        choices=(_choice("market.get_brand_metric", {"brand": "리바로"}),),
        unknown_tool_names=(),
        provider_choice_count=1,
    )

    class _BlockingExecutor:
        def execute(self, _choices: object) -> object:
            started.set()
            release.wait(timeout=1.0)
            raise RuntimeError("synthetic late execution")

    thread = start_v3_execution_shadow(
        "리바로 매출",
        selection,
        executor_factory=lambda _question: _BlockingExecutor(),
    )
    try:
        assert thread.daemon is True
        assert started.wait(timeout=1.0)
        assert thread.is_alive()
    finally:
        release.set()
        thread.join(timeout=1.0)
    assert not thread.is_alive()


def test_flag_off_fresh_process_does_not_import_executor_or_adapters() -> None:
    script = """
import os
import sys

os.environ["JW_CHAT_V3_TOOL_EXECUTION_SHADOW"] = "off"

attempts = []
class ImportBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {
            "jw_chat_agent_poc.tool_use.v3_execution",
            "jw_chat_agent_poc.tool_use.internal_adapters",
        }:
            attempts.append(fullname)
            raise AssertionError(f"unexpected execution import: {fullname}")
        return None

sys.meta_path.insert(0, ImportBlocker())
from jw_chat_agent_poc.tool_use.v3_execution_shadow import execution_shadow_enabled

if execution_shadow_enabled():
    raise SystemExit("execution shadow unexpectedly enabled")
if attempts:
    raise SystemExit(f"unexpected imports: {attempts}")
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


def test_empty_selection_is_not_a_tool_failure() -> None:
    bundle = V3ShadowToolExecutor(tools=()).execute(())

    assert bundle.status == "no_selection"
    assert bundle.failures == ()
    assert json.loads(json.dumps(bundle.summary()))["original_call_count"] == 0


def test_selection_shadow_returns_before_execution_observer_finishes(
    monkeypatch,
) -> None:
    observer_started = Event()
    release_observer = Event()
    selection = V3SelectionResult(
        intent=IntentFrame(),
        candidate_names=("market.get_brand_metric",),
        choices=(_choice("market.get_brand_metric", {"brand": "리바로"}),),
        unknown_tool_names=(),
        provider_choice_count=1,
    )

    class _Selector:
        def select(self, _question: str) -> V3SelectionResult:
            return selection

    def blocking_observer(*_args: object, **_kwargs: object) -> None:
        observer_started.set()
        release_observer.wait(timeout=1.0)

    monkeypatch.setattr(
        v3_selection_shadow,
        "_observe_v3_execution_shadow",
        blocking_observer,
    )
    thread = v3_selection_shadow.start_v3_selection_shadow(
        "리바로 매출",
        selector_factory=_Selector,
    )
    try:
        assert thread.daemon is True
        assert observer_started.wait(timeout=1.0)
        assert thread.is_alive()
    finally:
        release_observer.set()
        thread.join(timeout=1.0)
    assert not thread.is_alive()
