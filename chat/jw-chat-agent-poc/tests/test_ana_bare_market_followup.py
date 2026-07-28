"""'시장은?' after a brand turn asks about that brand's market.

The bare noun shape was only ever looked up in the brand namespace: '시장은?'
matched the bare-brand-switch pattern, was rejected there for not naming a
brand, and no later branch consulted the market slot the previous turn had
already established. The unrewritten string then reached the router with no
brand in it. '시장 규모는?' worked only because a separate closed vocabulary
spelled the metric out, so the shortest form of the same question fell through
the gap between the two.
"""

from __future__ import annotations

import pytest

from jw_chat_agent_poc.service.app import SessionStore, _answer_question, compute_final_answer
from jw_chat_agent_poc.service.conversation import ConversationSlots, ConversationTurn
from jw_chat_agent_poc.service.conversation_context import (
    ReferenceRecogniser,
    ReferenceStatus,
    requires_previous_turn,
    resolve_anaphora,
)

from test_service import _fake_agent_factory, _market_scope_resolver


_KNOWN_BRANDS = frozenset({"아일리아", "리바로", "로수젯"})


def _known_brand(brand: str) -> bool:
    return brand in _KNOWN_BRANDS


def _turn(brand: str, market: str, market_definition: str) -> ConversationTurn:
    return ConversationTurn(
        question=f"{brand} 매출 알려줘",
        answer=f"{brand} 매출입니다.",
        slots=ConversationSlots(
            anchor_brand=brand,
            market=market,
            market_definition=market_definition,
            metric="매출",
            period="2025-12",
        ),
    )


@pytest.mark.parametrize(
    ("brand", "market_definition"),
    [("아일리아", "ATC4 S01P0 시장"), ("리바로", "Statin 시장")],
)
def test_bare_market_followup_resolves_against_the_previous_turns_market(
    brand: str, market_definition: str
) -> None:
    resolution = resolve_anaphora(
        "시장은?", _turn(brand, "ml_001", market_definition), known_brand=_known_brand
    )

    assert resolution.resolved_question == f"{market_definition} 규모는?"
    assert resolution.reference_status == ReferenceStatus.RESOLVED
    assert resolution.recogniser == ReferenceRecogniser.BARE_MARKET
    assert resolution.interpretation_notice is not None


def test_bare_and_spelled_out_market_questions_resolve_to_one_question() -> None:
    turn = _turn("아일리아", "ml_001", "ATC4 S01P0 시장")

    bare = resolve_anaphora("시장은?", turn, known_brand=_known_brand)
    spelled_out = resolve_anaphora("시장 규모는?", turn, known_brand=_known_brand)

    assert bare.resolved_question == spelled_out.resolved_question
    assert bare.interpretation_notice == spelled_out.interpretation_notice


@pytest.mark.parametrize("question", ["시장은?", "시장은", "시장이?", "시장?"])
def test_particle_and_punctuation_variants_resolve_the_same_way(question: str) -> None:
    resolution = resolve_anaphora(
        question, _turn("아일리아", "ml_001", "ATC4 S01P0 시장"), known_brand=_known_brand
    )

    assert resolution.resolved_question == "ATC4 S01P0 시장 규모는?"


def test_market_without_a_definition_falls_back_to_the_market_id() -> None:
    turn = ConversationTurn(
        question="아일리아 매출 알려줘",
        answer="아일리아 매출입니다.",
        slots=ConversationSlots(anchor_brand="아일리아", market="ml_001", metric="매출"),
    )

    resolution = resolve_anaphora("시장은?", turn, known_brand=_known_brand)

    assert resolution.resolved_question == "ml_001 시장 규모는?"


def test_general_view_request_is_untouched_by_the_bare_market_branch() -> None:
    resolution = resolve_anaphora(
        "일반뷰로는?", _turn("아일리아", "ml_001", "ATC4 S01P0 시장"), known_brand=_known_brand
    )

    assert resolution.resolved_question == "ATC4 S01P0 시장 일반뷰로는?"


def test_bare_market_without_an_anchor_is_never_invented() -> None:
    first_turn = resolve_anaphora("시장은?", None, known_brand=_known_brand)

    assert first_turn.resolved_question == "시장은?"
    assert first_turn.reference_status == ReferenceStatus.NO_ANCHOR
    assert first_turn.brand is None
    assert first_turn.unresolved_reference is True

    brand_turn_without_market = ConversationTurn(
        question="아일리아 허가정보 알려줘",
        answer="아일리아 허가정보입니다.",
        slots=ConversationSlots(anchor_brand="아일리아"),
    )
    no_market = resolve_anaphora("시장은?", brand_turn_without_market, known_brand=_known_brand)

    assert no_market.resolved_question == "시장은?"
    assert no_market.unresolved_reference is True


def test_bare_market_followup_now_triggers_previous_turn_hydration() -> None:
    """Cross-pod: the previous turn is only fetched when this returns True."""
    assert requires_previous_turn("시장은?", known_brand=_known_brand) is True


def test_competitor_followup_stays_unresolved_and_stays_observable() -> None:
    resolution = resolve_anaphora(
        "경쟁 브랜드는?", _turn("아일리아", "ml_001", "ATC4 S01P0 시장"), known_brand=_known_brand
    )

    assert resolution.resolved_question == "경쟁 브랜드는?"
    assert resolution.reference_status == ReferenceStatus.PATTERN_MISS


def _delivered(question: str, conversation_id: str, *, seeded: bool) -> tuple[str, dict]:
    store = SessionStore()
    if seeded:
        turn = _turn("아일리아", "ml_001", "ATC4 S01P0 시장")
        store.conversations.record_exchange(
            conversation_id, turn.question, turn.answer, slots=turn.slots
        )
    item = _answer_question(
        store,
        _market_scope_resolver(),
        _fake_agent_factory,
        question,
        "live",
        conversation_id,
        use_direct_agent_loop=True,
    )
    result = item["result"]
    final = compute_final_answer(question, result, conversation_id)
    return str(result.get("answer") or ""), final.trace["qa_trace"]["routing"]["anaphora"]


def test_router_receives_the_resolved_market_question_not_the_bare_one() -> None:
    answer, anaphora = _delivered("시장은?", "ana-market-e2e", seeded=True)

    assert "ATC4 S01P0 시장 규모는?" in answer
    assert anaphora["status"] == "resolved"
    assert anaphora["recogniser"] == "bare_market"


def test_first_turn_bare_market_asks_which_market_instead_of_guessing() -> None:
    answer, anaphora = _delivered("시장은?", "ana-market-first", seeded=False)

    assert answer.startswith("직전 대화에서 가리키는 대상을 확인할 수 없습니다")
    assert anaphora["status"] == "no_anchor"
    assert anaphora["unresolved_reference"] is True
