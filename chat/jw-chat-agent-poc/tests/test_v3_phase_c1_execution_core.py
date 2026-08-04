from __future__ import annotations

from threading import Event
import time

from jw_chat_agent_poc.tool_use.contracts import EvidenceFact, ToolEnvelope
from jw_chat_agent_poc.tool_use.v3_execution import (
    ClinicalTrialFact,
    ExecutableTool,
    FileCellFact,
    MarketMetricFact,
    RegulatoryRuleFact,
    V3ShadowToolExecutor,
    canonical_argument_key,
)
from jw_chat_agent_poc.tool_use.v3_selection import MultiToolChoice


def _choice(name: str, arguments: dict[str, object]) -> MultiToolChoice:
    return MultiToolChoice(name=name, arguments=arguments)


def test_canonical_argument_key_sorts_keys_and_unifies_numeric_strings() -> None:
    left = canonical_argument_key(
        "market.get_brand_metric",
        {"history_points": "10", "nested": {"b": "2.0", "a": 1}},
    )
    right = canonical_argument_key(
        "market.get_brand_metric",
        {"nested": {"a": "1", "b": 2}, "history_points": 10.0},
    )

    assert left == right


def test_exact_dedup_never_merges_different_arguments_or_fills_defaults() -> None:
    calls: list[dict[str, object]] = []

    def execute(arguments: dict[str, object]) -> object:
        calls.append(arguments)
        return {"arguments": arguments}

    executor = V3ShadowToolExecutor(
        tools=(
            ExecutableTool(
                name="market.get_brand_metric",
                domain="market",
                timeout_s=1.0,
                execute=execute,
            ),
        )
    )
    bundle = executor.execute(
        (
            _choice(
                "market.get_brand_metric",
                {"brand": "리바로", "history_points": "10"},
            ),
            _choice(
                "market.get_brand_metric",
                {"history_points": 10, "brand": "리바로"},
            ),
            _choice("market.get_brand_metric", {"brand": "리바로"}),
            _choice(
                "market.get_brand_metric",
                {"brand": "가드렛", "history_points": 10},
            ),
        )
    )

    assert bundle.original_call_count == 4
    assert bundle.executed_call_count == 3
    assert bundle.deduplicated_call_count == 1
    assert len(calls) == 3
    assert {call["brand"] for call in calls} == {"리바로", "가드렛"}
    assert any("history_points" not in call for call in calls)


def test_partial_failure_does_not_cancel_successful_tools() -> None:
    def fail(_arguments: dict[str, object]) -> object:
        raise RuntimeError("synthetic upstream failure")

    def succeed(arguments: dict[str, object]) -> object:
        return {"brand": arguments["brand"], "status": "ok"}

    executor = V3ShadowToolExecutor(
        tools=(
            ExecutableTool("broken", "regulatory", 1.0, fail),
            ExecutableTool(
                "market.get_market_size",
                "market",
                1.0,
                succeed,
            ),
        ),
        max_workers=2,
    )
    bundle = executor.execute(
        (
            _choice("broken", {"brand": "리바로"}),
            _choice("market.get_market_size", {"brand": "리바로"}),
        )
    )

    assert len(bundle.facts) == 1
    assert isinstance(bundle.facts[0], MarketMetricFact)
    assert len(bundle.failures) == 1
    assert bundle.failures[0].stage == "execution"
    assert bundle.failures[0].error_type == "RuntimeError"


def test_timeout_records_failure_without_discarding_completed_tool() -> None:
    release = Event()

    def blocked(_arguments: dict[str, object]) -> object:
        release.wait(timeout=1.0)
        return {"status": "late"}

    executor = V3ShadowToolExecutor(
        tools=(
            ExecutableTool("blocked", "regulatory", 0.01, blocked),
            ExecutableTool(
                "market.get_market_size",
                "market",
                1.0,
                lambda arguments: {"brand": arguments["brand"]},
            ),
        ),
        max_workers=2,
    )
    started = time.monotonic()
    try:
        bundle = executor.execute(
            (
                _choice("blocked", {}),
                _choice("market.get_market_size", {"brand": "리바로"}),
            )
        )
    finally:
        release.set()

    assert time.monotonic() - started < 0.2
    assert len(bundle.facts) == 1
    assert bundle.failures[0].error_type == "TOOL_TIMEOUT"


def test_result_conversion_uses_domain_types_and_preserves_raw_objects() -> None:
    market_raw = {"render_data": {"sales_krw": 10}}
    regulatory_raw = ToolEnvelope(
        ok=True,
        preview="ok",
        evidence=(
            EvidenceFact(
                fact_id="r1",
                subject="리바로",
                metric="급여",
                value=None,
                unit=None,
                period=None,
                source_name="HIRA",
                source_locator="fixture",
                raw_ref=None,
            ),
        ),
        raw={"effective_date": "2026-01-01"},
        error_code=None,
        error_message=None,
    )
    clinical_raw = ToolEnvelope(
        ok=True,
        preview="ok",
        evidence=(),
        raw={"status": "RECRUITING"},
        error_code=None,
        error_message=None,
    )
    file_raw = {"rows": [["리바로", 10]]}
    results = {
        "market.get_brand_metric": market_raw,
        "hira_reimbursement_criteria": regulatory_raw,
        "clinicaltrials_v2_search": clinical_raw,
        "file.query": file_raw,
    }
    executor = V3ShadowToolExecutor(
        tools=tuple(
            ExecutableTool(
                name=name,
                domain=domain,
                timeout_s=1.0,
                execute=lambda _arguments, raw=raw: raw,
            )
            for (name, raw), domain in zip(
                results.items(),
                ("market", "regulatory", "clinical", "file"),
                strict=True,
            )
        )
    )
    bundle = executor.execute(
        tuple(_choice(name, {"brand": "리바로"}) for name in results)
    )

    assert [type(fact) for fact in bundle.facts] == [
        MarketMetricFact,
        RegulatoryRuleFact,
        ClinicalTrialFact,
        FileCellFact,
    ]
    assert [fact.raw_result for fact in bundle.facts] == list(results.values())
    assert bundle.facts[0].raw_result is market_raw
    assert bundle.facts[1].raw_result is regulatory_raw


def test_web_search_execution_is_recorded_without_web_source_fact() -> None:
    executor = V3ShadowToolExecutor(
        tools=(
            ExecutableTool(
                "web_search",
                "general",
                1.0,
                lambda _arguments: {"url": "https://example.invalid"},
            ),
        )
    )

    bundle = executor.execute(
        (_choice("web_search", {"query": "최신 동향"}),)
    )

    assert len(bundle.executions) == 1
    assert bundle.facts == ()
    assert bundle.failures == ()
    assert bundle.status == "complete"
    assert len(bundle.deferred) == 1
    assert bundle.deferred[0].stage == "conversion"
    assert "Phase C-3" in bundle.deferred[0].reason
