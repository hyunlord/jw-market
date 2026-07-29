"""A bare follow-up nobody resolved must say so, instead of reporting no reference.

``unresolved_reference`` is a control signal — "stop and ask which entity was
meant". It is only ever raised by a recogniser that claimed the question and
then ran out of context. A bare follow-up that no recogniser claims skips all of
those returns, so the flag stays ``False`` and the request trace read as "there
was no reference here" for a question whose subject existed only in the previous
turn. These tests pin the status that tells the two apart, at the resolver and on
the delivered response.
"""

from __future__ import annotations

import pytest

from jw_chat_agent_poc.service.app import SessionStore, _answer_question, compute_final_answer
from jw_chat_agent_poc.service.conversation import ConversationSlots, ConversationTurn
from jw_chat_agent_poc.service.conversation_context import (
    ReferenceRecogniser,
    ReferenceStatus,
    anaphora_observation,
    resolve_anaphora,
)

from test_service import _fake_agent_factory, _market_scope_resolver


_KNOWN_BRANDS = frozenset({"아일리아", "리바로", "로수젯"})


def _known_brand(brand: str) -> bool:
    return brand in _KNOWN_BRANDS


def _previous_turn(brand: str = "아일리아") -> ConversationTurn:
    return ConversationTurn(
        question=f"{brand} 매출 알려줘",
        answer=f"{brand} 매출입니다.",
        slots=ConversationSlots(
            anchor_brand=brand,
            market="ml_001",
            market_definition="ATC4 S01P0 시장",
            metric="매출",
            period="2025-12",
        ),
    )


@pytest.mark.parametrize("question", ["경쟁 브랜드는?", "상위 브랜드는?", "매출 추이는?"])
def test_unclaimed_bare_followup_reports_pattern_miss_not_absence_of_reference(question: str) -> None:
    resolution = resolve_anaphora(question, _previous_turn(), known_brand=_known_brand)

    assert resolution.resolved_question == question
    assert resolution.reference_status == ReferenceStatus.PATTERN_MISS
    assert resolution.candidate_shape is True
    # The flag itself is unchanged: this is why it could not be read as a report
    # on whether a reference was present.
    assert resolution.unresolved_reference is False


def test_pattern_miss_is_brand_independent() -> None:
    for brand in ("아일리아", "리바로"):
        resolution = resolve_anaphora("경쟁 브랜드는?", _previous_turn(brand), known_brand=_known_brand)
        assert resolution.reference_status == ReferenceStatus.PATTERN_MISS


def test_first_turn_bare_followup_has_no_anchor_and_is_never_resolved() -> None:
    resolution = resolve_anaphora("경쟁 브랜드는?", None, known_brand=_known_brand)

    assert resolution.resolved_question == "경쟁 브랜드는?"
    assert resolution.reference_status == ReferenceStatus.NO_ANCHOR
    assert resolution.brand is None


@pytest.mark.parametrize(
    ("question", "recogniser"),
    [
        ("매출은?", ReferenceRecogniser.BARE_METRIC),
        ("시장 규모는?", ReferenceRecogniser.BARE_MARKET),
        ("리바로는?", ReferenceRecogniser.BARE_BRAND_SWITCH),
        ("그 브랜드 매출은?", ReferenceRecogniser.ANCHOR_BRAND),
    ],
)
def test_resolved_followups_name_the_recogniser_that_claimed_them(
    question: str, recogniser: ReferenceRecogniser
) -> None:
    resolution = resolve_anaphora(question, _previous_turn(), known_brand=_known_brand)

    assert resolution.reference_status == ReferenceStatus.RESOLVED
    assert resolution.recogniser == recogniser
    assert resolution.resolved_question != question


@pytest.mark.parametrize("question", ["리바로 매출 알려줘", "고지혈증 시장 상위 5개 브랜드는?"])
def test_self_anchored_questions_stay_not_anaphoric(question: str) -> None:
    resolution = resolve_anaphora(question, _previous_turn(), known_brand=_known_brand)

    assert resolution.reference_status == ReferenceStatus.NOT_ANAPHORIC
    assert resolution.candidate_shape is False


def test_missing_previous_intent_is_distinct_from_a_missing_anchor() -> None:
    turn = ConversationTurn(
        question="아일리아 정보 알려줘",
        answer="아일리아 정보입니다.",
        slots=ConversationSlots(anchor_brand="아일리아"),
    )

    resolution = resolve_anaphora("리바로는?", turn, known_brand=_known_brand)

    assert resolution.unresolved_reference is True
    assert resolution.reference_status == ReferenceStatus.NO_PRIOR_INTENT


def test_observation_carries_only_enumerated_values_and_bools() -> None:
    observation = anaphora_observation(
        resolve_anaphora("경쟁 브랜드는?", _previous_turn(), known_brand=_known_brand)
    )

    assert observation == {
        "status": "pattern_miss",
        "recogniser": None,
        "candidate_shape": True,
        "unresolved_reference": False,
        # A bare follow-up inherits no issue observation, and the flag says so with a
        # bool — which is what this test is about, so it is listed like the others.
        "inherited_issue_observation": False,
    }
    assert "브랜드" not in repr(observation)


def _delivered_anaphora(question: str, conversation_id: str, *, seeded: bool) -> dict:
    store = SessionStore()
    if seeded:
        turn = _previous_turn()
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
    final = compute_final_answer(question, item["result"], conversation_id)
    return final.trace["qa_trace"]["routing"]["anaphora"]


def test_pattern_miss_reaches_the_delivered_response_not_only_the_resolver() -> None:
    assert _delivered_anaphora("경쟁 브랜드는?", "ana-public-miss", seeded=True) == {
        "status": "pattern_miss",
        "recogniser": None,
        "candidate_shape": True,
        "unresolved_reference": False,
        # A bare follow-up inherits no news observation; the flag says so with a bool.
        # This assertion compares the whole dict, so the key is listed rather than
        # letting a newly projected field slip past unasserted.
        "inherited_issue_observation": False,
    }


def test_delivered_response_separates_resolved_from_unanchored_and_standalone() -> None:
    resolved = _delivered_anaphora("매출은?", "ana-public-resolved", seeded=True)
    assert resolved["status"] == "resolved"
    assert resolved["recogniser"] == "bare_metric"

    first_turn = _delivered_anaphora("경쟁 브랜드는?", "ana-public-first", seeded=False)
    assert first_turn["status"] == "no_anchor"

    standalone = _delivered_anaphora("리바로 매출 알려줘", "ana-public-plain", seeded=False)
    assert standalone["status"] == "not_anaphoric"
    assert standalone["candidate_shape"] is False


def test_projection_carries_whether_a_cause_question_inherited_an_observation() -> None:
    """The field GPT5-FIX-P3 added to the resolver has to survive the projection.

    Without this the live trace could not tell an inherited cause question from an
    identical standalone one: both plan the same contract, so the contract id says
    nothing, and the projection is what the request trace actually carries.
    """
    from jw_chat_agent_poc.service.conversation import ConversationSlots, ConversationTurn
    from jw_chat_agent_poc.service.runtime_provenance import _qa_anaphora

    turn = ConversationTurn(
        question="리바로 관련 최근 이슈 뭐 있어?",
        answer="정리했습니다.",
        slots=ConversationSlots(issue_observation=("고지혈증 치료제 약가 인하 고시",)),
    )
    inherited = _qa_anaphora(
        {"_qa_anaphora": anaphora_observation(resolve_anaphora("리바로 왜 이렇게 됐어?", turn))}
    )
    standalone = _qa_anaphora(
        {"_qa_anaphora": anaphora_observation(resolve_anaphora("리바로 왜 이렇게 됐어?", None))}
    )

    assert inherited["inherited_issue_observation"] is True
    assert inherited["status"] == "inherited_observation"
    assert inherited["recogniser"] == "issue_cause"
    assert standalone["inherited_issue_observation"] is False
    # Headlines are content and must not ride along in the trace.
    assert "약가" not in repr(inherited)


def test_projection_reports_not_observed_as_null_not_false() -> None:
    from jw_chat_agent_poc.service.runtime_provenance import _qa_anaphora

    assert _qa_anaphora({})["inherited_issue_observation"] is None
