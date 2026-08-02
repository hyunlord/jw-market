from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import subprocess
import sys

import pytest

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.context_scope import ContextScope
from jw_chat_agent_poc.service.routing_boundary_contract import (
    ROUTING_BOUNDARIES_ENV,
    MarketRouteKind,
)
from jw_chat_agent_poc.service.routing_boundaries import (
    decide_app_scope_route,
    decide_market_shortcut,
)

from test_service import FakeAgent, _market_scope_resolver


@pytest.mark.parametrize(
    (
        "file_question",
        "effective_question",
        "has_file",
        "has_market_intent",
        "has_market_anchor",
        "columns",
        "needs_brand_clarification",
        "expected_scope",
    ),
    (
        ("리바로 매출", "리바로 매출", False, True, True, (), False, ContextScope.MARKET),
        ("이 보고서 매출", "이 보고서 매출", True, False, False, (), False, ContextScope.FILE),
        (
            "이 보고서와 리바로 시장 데이터 비교",
            "이 보고서와 리바로 시장 데이터 비교",
            True,
            True,
            True,
            (),
            False,
            ContextScope.MIXED,
        ),
        ("브랜드가 모호한 질문", "브랜드가 모호한 질문", False, False, False, (), True, ContextScope.MARKET),
        ("채널 비교", "채널 비교", True, False, False, ("channel",), False, ContextScope.FILE),
    ),
)
def test_app_scope_decision_matches_legacy_for_five_scenarios(
    file_question: str,
    effective_question: str,
    has_file: bool,
    has_market_intent: bool,
    has_market_anchor: bool,
    columns: tuple[str, ...],
    needs_brand_clarification: bool,
    expected_scope: ContextScope,
) -> None:
    kwargs = {
        "file_question": file_question,
        "effective_question": effective_question,
        "has_file": has_file,
        "is_fresh_upload": False,
        "has_market_intent": has_market_intent,
        "has_market_anchor": has_market_anchor,
        "file_schema_columns": columns,
        "needs_brand_clarification": needs_brand_clarification,
        "needs_market_clarification": False,
    }

    extracted = decide_app_scope_route(**kwargs)
    legacy = service_app._legacy_app_scope_decision(**kwargs)

    assert extracted == legacy
    assert extracted.context_scope is expected_scope


@pytest.mark.parametrize(
    ("question", "has_documents", "use_direct_agent_loop", "expected_kind"),
    (
        ("ml_123 시장 알려줘", False, False, MarketRouteKind.EXPLICIT_MARKET_ID),
        ("리바로와 같은 시장 브랜드 알려줘", False, False, MarketRouteKind.MARKET_MEMBERS_BRAND),
        ("고지혈증 시장 브랜드 알려줘", False, False, MarketRouteKind.NAMED_MARKET),
        ("리바로와 같은 시장 규모 알려줘", False, False, MarketRouteKind.MARKET_SCOPE_ANSWER),
        ("리바로 매출 알려줘", False, True, MarketRouteKind.AGENT_LOOP),
    ),
)
def test_market_shortcut_decision_matches_legacy_for_five_scenarios(
    question: str,
    has_documents: bool,
    use_direct_agent_loop: bool,
    expected_kind: MarketRouteKind,
) -> None:
    resolver = _market_scope_resolver()
    kwargs = {
        "question": question,
        "has_documents": has_documents,
        "use_direct_agent_loop": use_direct_agent_loop,
        "market_scope_resolver": resolver,
    }

    extracted = decide_market_shortcut(**kwargs)
    legacy = service_app._legacy_market_shortcut_decision(**kwargs)

    assert asdict(extracted) == asdict(legacy)
    assert extracted.kind is expected_kind


def test_routing_boundaries_default_path_uses_extracted_decisions(monkeypatch) -> None:
    calls: list[str] = []
    original_scope = service_app.decide_app_scope_route
    original_market = service_app.decide_market_shortcut

    def record_scope(**kwargs):
        calls.append("app_scope")
        return original_scope(**kwargs)

    def record_market(**kwargs):
        calls.append("market_shortcut")
        return original_market(**kwargs)

    monkeypatch.delenv(ROUTING_BOUNDARIES_ENV, raising=False)
    monkeypatch.setattr(service_app, "decide_app_scope_route", record_scope)
    monkeypatch.setattr(service_app, "decide_market_shortcut", record_market)

    service_app._answer_question(
        service_app.SessionStore(),
        _market_scope_resolver(),
        lambda **_kwargs: FakeAgent(external_mode="fixture"),
        "리바로 매출 알려줘",
        "fixture",
        "phase4d-default-on",
    )

    assert calls == ["app_scope", "market_shortcut"]


def test_routing_boundaries_flag_off_does_not_call_extracted_decisions(monkeypatch) -> None:
    def fail(**_kwargs):
        raise AssertionError("extracted routing boundary must not run when disabled")

    monkeypatch.setenv(ROUTING_BOUNDARIES_ENV, "0")
    monkeypatch.setattr(service_app, "decide_app_scope_route", fail)
    monkeypatch.setattr(service_app, "decide_market_shortcut", fail)

    result = service_app._answer_question(
        service_app.SessionStore(),
        _market_scope_resolver(),
        lambda **_kwargs: FakeAgent(external_mode="fixture"),
        "리바로 매출 알려줘",
        "fixture",
        "phase4d-flag-off",
    )

    assert result["result"]["answer"]


def test_routing_boundaries_flag_off_imports_app_without_extracted_module() -> None:
    project_root = Path(__file__).resolve().parents[1]
    module_name = "jw_chat_agent_poc.service.routing_boundaries"
    script = f"""
import sys

class BlockExtractedRoutingModule:
    def find_spec(self, fullname, path, target=None):
        if fullname == {module_name!r}:
            raise RuntimeError("extracted routing module import attempted")
        return None

sys.meta_path.insert(0, BlockExtractedRoutingModule())
from jw_chat_agent_poc.service import app
assert {module_name!r} not in sys.modules
assert not app.routing_boundaries_enabled()
"""
    env = dict(os.environ)
    env[ROUTING_BOUNDARIES_ENV] = "0"

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_extracted_decisions_use_app_monkeypatch_seams(monkeypatch) -> None:
    observed: list[str] = []
    original_resolve = service_app.resolve_context_scope

    def record_resolve(*args, **kwargs):
        observed.append("resolve_context_scope")
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(service_app, "resolve_context_scope", record_resolve)
    monkeypatch.setattr(service_app, "requested_period", lambda _question: "2024")

    app_scope = service_app.decide_app_scope_route(
        file_question="리바로 매출",
        effective_question="리바로 매출",
        has_file=False,
        is_fresh_upload=False,
        has_market_intent=True,
        has_market_anchor=True,
        file_schema_columns=(),
        needs_brand_clarification=False,
        needs_market_clarification=False,
        resolve_context_scope_fn=service_app.resolve_context_scope,
        matches_file_schema_fn=service_app.matches_file_schema,
        has_file_reference_fn=service_app.has_file_reference,
    )
    market = service_app.decide_market_shortcut(
        question="ml_123 시장 알려줘",
        has_documents=False,
        use_direct_agent_loop=False,
        market_scope_resolver=_market_scope_resolver(),
        requested_period_fn=service_app.requested_period,
    )

    assert app_scope.context_scope is ContextScope.MARKET
    assert observed == ["resolve_context_scope"]
    assert market.period == "2024"


def test_divergent_market_case_keeps_phase3_decisions(monkeypatch) -> None:
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(
        service_app,
        "observe_route_decision",
        lambda **fields: observed.append(fields),
    )

    service_app._answer_question(
        service_app.SessionStore(),
        _market_scope_resolver(),
        lambda **_kwargs: FakeAgent(external_mode="fixture"),
        "리바로 매출 알려줘",
        "fixture",
        "phase4d-divergent",
    )

    by_point = {item["decided_by"]: item for item in observed}
    assert by_point["app_scope"]["mode"].value == "deterministic"
    assert by_point["app_scope"]["handler"] == "context_scope_dispatch"
    assert by_point["market_shortcut"]["mode"].value == "agentic"
    assert by_point["market_shortcut"]["handler"] == "agent_loop"
    assert by_point["app_scope"]["rejected_alternatives"]
    assert by_point["market_shortcut"]["rejected_alternatives"]
