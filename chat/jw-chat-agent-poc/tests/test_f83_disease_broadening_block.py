from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import SessionStore
from jw_chat_agent_poc.service.conversation import (
    DiseaseCodeCandidateSlot,
    PendingClarification,
)
from jw_chat_agent_poc.tools.metrics.cache_live import StaticMetricsCacheReader
from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver

from test_metrics_cache import BRAND_CARDS, CACHE_BRANDS


_LIVE_CAPTURE = Path(__file__).parent / "fixtures" / "f83_live" / "F79_sequence.json"


def _live_turns() -> list[dict[str, Any]]:
    return json.loads(_LIVE_CAPTURE.read_text(encoding="utf-8"))


class _LiveF79ReplayAgent:
    def __init__(self, turns: list[dict[str, Any]], calls: list[str]) -> None:
        self._turns = {str(turn["question"]): turn for turn in turns}
        self._calls = calls

    def answer(self, question: str, _documents=None) -> dict[str, Any]:
        self._calls.append(question)
        turn = self._turns[question]
        return {
            "answer": turn["text"],
            "sources": list(turn["sources"]),
            "tool_calls": [
                {
                    "tool": tool,
                    "status": status,
                    "source": "hira_disease",
                    "render_data": {
                        "reason": "hira_disease_code_search_no_data",
                        "candidates": [],
                    },
                }
                for tool, status in turn["tools"]
            ],
        }


def _resolver() -> MarketScopeResolver:
    return MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(
            cache_brands=CACHE_BRANDS,
            market_status=BRAND_CARDS,
        )
    )


@pytest.mark.parametrize("reply", ("2형", "두번째"))
def test_live_f79_indirect_reply_without_candidate_slot_never_reaches_agent(
    reply: str,
) -> None:
    turns = _live_turns()
    calls: list[str] = []

    def factory(*, external_mode: str = "live") -> _LiveF79ReplayAgent:
        del external_mode
        return _LiveF79ReplayAgent(turns, calls)

    store = SessionStore()
    conversation_id = str(turns[0]["conversation_id"])
    first = service_app._answer_question(
        store,
        _resolver(),
        factory,
        str(turns[0]["question"]),
        "fixture",
        conversation_id,
    )

    assert store.conversations.get_pending(conversation_id) is None
    assert first["result"]["tool_calls"][0]["tool"] == "hira_disease_code_absent"

    second = service_app._answer_question(
        store,
        _resolver(),
        factory,
        reply,
        "fixture",
        conversation_id,
    )

    assert calls == [str(turns[0]["question"])]
    assert second["result"]["tool_calls"] == []
    assert "E11" not in second["result"]["answer"]
    assert not any(character.isdigit() for character in second["result"]["answer"])


def test_live_f79_three_turn_sequence_never_reaches_parent_code() -> None:
    turns = _live_turns()
    calls: list[str] = []

    def factory(*, external_mode: str = "live") -> _LiveF79ReplayAgent:
        del external_mode
        return _LiveF79ReplayAgent(turns, calls)

    store = SessionStore()
    conversation_id = str(turns[0]["conversation_id"])
    service_app._answer_question(
        store,
        _resolver(),
        factory,
        str(turns[0]["question"]),
        "fixture",
        conversation_id,
    )

    for reply in ("2형", "두번째"):
        result = service_app._answer_question(
            store,
            _resolver(),
            factory,
            reply,
            "fixture",
            conversation_id,
        )
        assert result["result"]["tool_calls"] == []
        assert "E11" not in result["result"]["answer"]
        assert not any(character.isdigit() for character in result["result"]["answer"])

    assert calls == [str(turns[0]["question"])]


@pytest.mark.parametrize(
    ("reply", "expected_code"),
    (
        ("1형", "E10.3"),
        ("2형", "E11.3"),
        ("두번째", "E11.3"),
        ("기타 명시된", "E13.3"),
    ),
)
def test_candidate_slot_keeps_qualified_code(
    reply: str,
    expected_code: str,
) -> None:
    turns = _live_turns()
    calls: list[str] = []

    def factory(*, external_mode: str = "live") -> _LiveF79ReplayAgent:
        del external_mode
        return _LiveF79ReplayAgent(turns, calls)

    store = SessionStore()
    conversation_id = "f83-qualified-candidate"
    expires_at = store.conversations.pending_expiry()
    store.conversations.set_pending(
        conversation_id,
        PendingClarification(
            kind="hira_disease_code",
            original_question=str(turns[0]["question"]),
            brand="",
            metric="patient_count",
            created_at=expires_at - store.conversations.pending_ttl_seconds,
            expires_at=expires_at,
            disease_candidates=(
                DiseaseCodeCandidateSlot("E10.3", "1형 당뇨병·망막병증 동반"),
                DiseaseCodeCandidateSlot("E11.3", "2형 당뇨병·망막병증 동반"),
                DiseaseCodeCandidateSlot("E13.3", "기타 명시된 당뇨병·망막병증 동반"),
            ),
        ),
    )

    selected = service_app._select_hira_disease_candidate(
        reply,
        store.conversations.get_pending(conversation_id).disease_candidates,
    )

    assert selected is not None
    assert selected.sick_cd == expected_code


@pytest.mark.parametrize("code", ("E10.3", "E11.3", "E13.3"))
def test_direct_qualified_code_never_broadens(code: str) -> None:
    assert (
        service_app.explicit_hira_disease_code(
            f"질병코드 {code} 환자수 통계 알려줘"
        )
        == code
    )
