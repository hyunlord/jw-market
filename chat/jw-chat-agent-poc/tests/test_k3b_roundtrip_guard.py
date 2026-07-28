"""K3b: preserve market-scope capability across the real evidence round trip."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

import pytest

from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact
from jw_chat_agent_poc.service import evidence_binding_observability
from jw_chat_agent_poc.service.evidence_binding import evidence_facts_from_result
from jw_chat_agent_poc.service.evidence_binding_rules import scope_matches


EXPECTED_MARKET_IDS = frozenset({"ml_006"})
TARGET_METRIC = "시장규모"


def _market_call_without_market_id() -> dict[str, Any]:
    return {
        "tool": "get_brand_metric",
        "status": "ok",
        "render_data": {
            "brand": "리바로",
            "period": "2024-01",
            "market_size_recent_krw": 213_925_000_000,
        },
    }


def _real_result() -> dict[str, Any]:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[_market_call_without_market_id()],
        sources=["UBIST"],
    )
    return {"markdown_response": response.to_dict()}


def _market_size_fact(facts: tuple[EvidenceFact, ...]) -> EvidenceFact:
    return next(fact for fact in facts if fact.metric == TARGET_METRIC)


def _serialized_market_size_fact(result: dict[str, Any]) -> dict[str, Any]:
    evidence = result["markdown_response"]["evidence"]
    return next(item for item in evidence if item["metric"] == TARGET_METRIC)


def _assert_roundtrip_guard(result: dict[str, Any]) -> tuple[EvidenceFact, ...]:
    serialized = _serialized_market_size_fact(result)
    assert "market_scope_capable" in serialized
    assert serialized["market_scope_capable"] is True

    loaded = evidence_facts_from_result(result)
    fact = _market_size_fact(loaded)
    assert fact.market_scope_capable is True
    assert fact.market_id == ""
    assert scope_matches(fact, frozenset(), EXPECTED_MARKET_IDS) is False
    return loaded


def _capture_observability_reentry(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[EvidenceFact], Callable[..., EvidenceFact]]:
    captured: list[EvidenceFact] = []

    def capture(**values: Any) -> EvidenceFact:
        fact = EvidenceFact(**values)
        captured.append(fact)
        return fact

    monkeypatch.setattr(evidence_binding_observability, "EvidenceFact", capture)
    return captured, capture


def test_market_scope_capability_survives_both_real_reentry_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _real_result()
    loaded = _assert_roundtrip_guard(result)
    captured, _ = _capture_observability_reentry(monkeypatch)

    inventory = evidence_binding_observability.evidence_fact_input_inventory(
        result,
        loaded,
    )

    assert inventory["source"] == "serialized_markdown_evidence"
    observed = _market_size_fact(tuple(captured))
    assert observed.market_scope_capable is True
    assert scope_matches(observed, frozenset(), EXPECTED_MARKET_IDS) is False


def test_missing_serialized_capability_exempts_all_sixty_gap_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _real_result()
    serialized = _serialized_market_size_fact(result)
    without_capability = {
        key: value
        for key, value in serialized.items()
        if key != "market_scope_capable"
    }
    result["markdown_response"]["evidence"] = tuple(
        deepcopy(without_capability)
        for _ in range(60)
    )

    loaded = evidence_facts_from_result(result)
    assert len(loaded) == 60
    assert all(fact.market_scope_capable is False for fact in loaded)
    assert all(
        scope_matches(fact, frozenset(), EXPECTED_MARKET_IDS) is True
        for fact in loaded
    )

    captured, _ = _capture_observability_reentry(monkeypatch)
    inventory = evidence_binding_observability.evidence_fact_input_inventory(
        result,
        loaded,
    )
    assert inventory["input_item_count"] == 60
    assert inventory["loaded_fact_count"] == 60
    assert inventory["discarded_count"] == 0
    assert len(captured) == 60
    assert all(fact.market_scope_capable is False for fact in captured)


def test_roundtrip_guard_detects_a_whitelisting_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_to_dict = EvidenceFact.to_dict

    def without_capability(self: EvidenceFact) -> dict[str, Any]:
        serialized = original_to_dict(self)
        serialized.pop("market_scope_capable")
        return serialized

    monkeypatch.setattr(EvidenceFact, "to_dict", without_capability)

    with pytest.raises(AssertionError, match="market_scope_capable"):
        _assert_roundtrip_guard(_real_result())
