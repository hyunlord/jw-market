from __future__ import annotations

from jw_chat_agent_poc.service.conversation import (
    ConversationSlots,
    ConversationTurn,
    RankedBrandSlot,
    ResultReference,
    SeriesPoint,
    conversation_slots_from_dict,
    conversation_slots_to_dict,
)
from jw_chat_agent_poc.service.conversation_context import (
    extract_conversation_slots,
    requires_previous_turn,
    resolve_anaphora,
    reused_context_result,
)
from jw_chat_agent_poc.service.app import SessionStore, _answer_question, compute_final_answer

from test_service import _fake_agent_factory, _market_scope_resolver


def _ranked_slot() -> RankedBrandSlot:
    return RankedBrandSlot(
        brand="로수젯",
        rank=1,
        series=(
            SeriesPoint(period="2026-03", value_krw=19_500_000_000.0, ms_pct=8.7, rank=1),
            SeriesPoint(period="2026-04", value_krw=20_685_385_934.33, ms_pct=9.1659, rank=1),
        ),
    )


def test_conversation_slots_json_round_trip_preserves_verified_context() -> None:
    slots = ConversationSlots(
        anchor_brand="리바로",
        market="ml_006",
        period="2026-05",
        metric="매출",
        view="strategic_ml",
        result_ref=ResultReference(
            tool="get_brand_metric",
            source="UBIST",
            brand="리바로",
            market="ml_006",
            period="2026-05",
        ),
        ranked_brands=("로수젯",),
        ranked=(_ranked_slot(),),
        file_name="sellout.xlsx",
        file_measure="PRICE",
    )

    assert conversation_slots_from_dict(conversation_slots_to_dict(slots)) == slots


def test_extract_slots_keeps_anchor_market_period_denominator_and_ranked_series() -> None:
    result = {
        "resolution": {"canonical_brand": "리바로"},
        "tool_calls": [
            {
                "tool": "get_brand_metric",
                "render_data": {
                    "market_id": "ml_006",
                    "metric": "시장점유율",
                    "view_source_id": "strategic_ml",
                    "market_definition_full": "Statin 시장",
                    "period": "2026-04",
                    "market_size_억원": 2256.77,
                    "level_top5_trend_series": [
                        {
                            "brand": "로수젯",
                            "rank": 1,
                            "series": [
                                {"period": "2026-03", "value_krw": 19_500_000_000.0, "ms_pct": 8.7, "rank": 1},
                                {"period": "2026-04", "value_krw": 20_685_385_934.33, "ms_pct": 9.1659, "rank": 1},
                            ],
                        }
                    ],
                },
            }
        ],
    }

    slots = extract_conversation_slots(result)

    assert slots.anchor_brand == "리바로"
    assert slots.market == "ml_006"
    assert slots.market_definition == "Statin 시장"
    assert slots.period == "2026-04"
    assert slots.metric == "시장점유율"
    assert slots.view == "strategic_ml"
    assert slots.result_ref == ResultReference(
        tool="get_brand_metric",
        source=None,
        brand="리바로",
        market="ml_006",
        period="2026-04",
    )
    assert slots.denominator == "2256.77억원"
    assert slots.ranked_brands == ("로수젯",)
    assert slots.ranked[0].series[-1].ms_pct == 9.1659


def test_extract_slots_prefers_metric_result_over_query_plan_metadata() -> None:
    result = {
        "tool_calls": [
            {
                "tool": "get_brand_metric",
                "render_data": {
                    "metric": "query_spec",
                    "period": "2024",
                    "query_spec": {"filters": {"brand": "리바로", "period": "2024"}},
                },
            },
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "brand": "리바로",
                    "market_id": "ml_006",
                    "metric": "매출",
                    "view_source_id": "strategic_ml",
                    "period": "2025-Q2",
                },
            },
        ]
    }

    slots = extract_conversation_slots(result)

    assert slots.anchor_brand == "리바로"
    assert slots.market == "ml_006"
    assert slots.metric == "매출"
    assert slots.period == "2025-Q2"
    assert slots.result_ref == ResultReference(
        tool="get_brand_metric",
        source="UBIST",
        brand="리바로",
        market="ml_006",
        period="2025-Q2",
    )


def test_resolve_anaphora_maps_first_rank_only_from_previous_turn() -> None:
    previous = ConversationTurn(
        question="리바로 시장 상위 3개 브랜드 점유율",
        answer="1위 로수젯",
        slots=ConversationSlots(anchor_brand="리바로", ranked=(_ranked_slot(),), ranked_brands=("로수젯",)),
    )

    resolved = resolve_anaphora("그중 1위 브랜드 점유율 추이는?", previous)

    assert resolved.resolved_question == "로수젯 점유율 추이는?"
    assert resolved.brand == "로수젯"
    assert resolved.reusable_ranked == _ranked_slot()
    assert resolved.unresolved_reference is False


def test_resolve_anaphora_accepts_spaced_first_rank_reference_from_portal() -> None:
    previous = ConversationTurn(
        question="고지혈증 상위 5개",
        answer="1위 로수젯",
        slots=ConversationSlots(anchor_brand="리바로", ranked=(_ranked_slot(),), ranked_brands=("로수젯",)),
    )

    assert requires_previous_turn("그 중 1위 브랜드 추이는?") is True

    resolved = resolve_anaphora("그 중 1위 브랜드 추이는?", previous)

    assert resolved.resolved_question == "로수젯 추이는?"
    assert resolved.brand == "로수젯"
    assert resolved.reusable_ranked == _ranked_slot()
    assert resolved.unresolved_reference is False


def test_resolve_anaphora_never_guesses_without_previous_basis() -> None:
    resolved = resolve_anaphora("그 브랜드 점유율 추이는?", None)

    assert resolved.resolved_question == "그 브랜드 점유율 추이는?"
    assert resolved.brand is None
    assert resolved.unresolved_reference is True


def test_generic_demonstrative_without_resolvable_slot_fails_closed() -> None:
    previous = ConversationTurn(
        question="리바로 어때?",
        answer="어떤 지표가 궁금한지 물었습니다.",
        slots=ConversationSlots(anchor_brand="리바로"),
    )

    assert requires_previous_turn("그건?") is True
    resolved = resolve_anaphora("그건?", previous)

    assert resolved.unresolved_reference is True
    assert resolved.resolved_question == "그건?"


def test_resolve_anaphora_keeps_first_rank_target_for_ranked_brand_requery() -> None:
    previous = ConversationTurn(
        question="상위 3개",
        answer="1위 로수젯",
        slots=ConversationSlots(anchor_brand="리바로", ranked_brands=("로수젯",)),
    )

    resolved = resolve_anaphora("그중 1위 브랜드 점유율 추이는?", previous)

    assert resolved.resolved_question == "로수젯 점유율 추이는?"
    assert resolved.brand == "로수젯"
    assert resolved.reusable_ranked is None


def test_first_rank_followup_persists_ranked_target_for_next_pronoun(monkeypatch) -> None:
    store = SessionStore()
    conversation_id = "f41-first-rank-slot-precedence"
    routed_questions: list[str] = []

    store.conversations.record_exchange(
        conversation_id,
        "고지혈증 시장 상위 5개",
        "1위 로수젯",
        slots=ConversationSlots(
            anchor_brand="리바로",
            market="ml_006",
            market_definition="고지혈증 시장",
            metric="시장점유율",
            ranked_brands=("로수젯", "리피토", "리바로"),
        ),
    )

    def capture_answer(_resolver, _factory, _conversation_id, routed_question, *_args, **_kwargs):
        routed_questions.append(routed_question)
        canonical_brand = "리바로" if routed_question.startswith("리바로 시장의 ") else "로수젯"
        return {
            "answer": f"{canonical_brand}의 시장점유율을 확인했습니다.",
            "resolution": {"canonical_brand": canonical_brand},
            "sources": ["UBIST"],
            "tool_calls": [
                {
                    "tool": "get_brand_metric",
                    "source": "UBIST",
                    "render_data": {
                        "anchor_brand": canonical_brand,
                        "market_id": "ml_006",
                        "metric": "시장점유율",
                        "period": "2026-05",
                    },
                }
            ],
        }

    monkeypatch.setattr("jw_chat_agent_poc.service.app._answer_without_pending", capture_answer)

    first_snapshot = store.conversations.get_or_create(conversation_id).turns[-1].slots
    _answer_question(
        store,
        _market_scope_resolver(),
        _fake_agent_factory,
        "그중 1위 점유율은?",
        "live",
        conversation_id,
        use_direct_agent_loop=True,
    )
    second_snapshot = store.conversations.get_or_create(conversation_id).turns[-1].slots
    _answer_question(
        store,
        _market_scope_resolver(),
        _fake_agent_factory,
        "걔 최근 추세는?",
        "live",
        conversation_id,
        use_direct_agent_loop=True,
    )
    third_snapshot = store.conversations.get_or_create(conversation_id).turns[-1].slots

    assert first_snapshot.ranked_brands[0] == "로수젯"
    assert first_snapshot.anchor_brand == "리바로"
    assert first_snapshot.market == "ml_006"
    assert first_snapshot.metric == "시장점유율"
    assert routed_questions == ["로수젯 점유율은?", "로수젯 최근 추세는?"]
    assert second_snapshot.anchor_brand == "로수젯"
    assert second_snapshot.market == "ml_006"
    assert second_snapshot.metric == "시장점유율"
    assert third_snapshot.anchor_brand == "로수젯"
    assert third_snapshot.market == "ml_006"
    assert third_snapshot.metric == "시장점유율"


def test_first_rank_followup_fails_closed_without_ranked_slots() -> None:
    previous = ConversationTurn(
        question="고지혈증 시장을 확인해줘",
        answer="시장 정보를 확인했습니다.",
        slots=ConversationSlots(anchor_brand="리바로", market="ml_006"),
    )

    resolved = resolve_anaphora("그중 1위 점유율은?", previous)

    assert resolved.resolved_question == "그중 1위 점유율은?"
    assert resolved.brand is None
    assert resolved.unresolved_reference is True


def test_resolve_anaphora_inherits_market_for_common_market_pronouns() -> None:
    previous = ConversationTurn(
        question="리바로와 로수젯 비교",
        answer="두 브랜드를 비교했습니다.",
        slots=ConversationSlots(
            anchor_brand="리바로",
            market="ml_006",
            market_definition="Statin 시장",
        ),
    )

    for question in (
        "이 시장 상위 5개 브랜드 점유율과 합계",
        "해당 시장 집중도는 어때?",
        "그 시장의 HHI는?",
    ):
        resolved = resolve_anaphora(question, previous)
        assert resolved.resolved_question.startswith("리바로 시장")
        assert resolved.unresolved_reference is False


def test_resolve_anaphora_inherits_only_missing_intent_for_contrast_followup() -> None:
    previous = ConversationTurn(
        question="리피토 매출 추이 알려줘",
        answer="리피토 매출 추이를 확인했습니다.",
        slots=ConversationSlots(anchor_brand="리피토"),
    )

    resolved = resolve_anaphora("그럼 리바로는?", previous)

    assert resolved.resolved_question == "리바로 매출 추이는?"
    assert resolved.brand == "리바로"
    assert resolved.interpretation_notice == "리바로의 매출 추이로 이해했어요."
    assert resolved.unresolved_reference is False


def test_resolve_anaphora_does_not_override_complete_contrast_question() -> None:
    previous = ConversationTurn(
        question="리피토 매출 추이 알려줘",
        answer="리피토 매출 추이를 확인했습니다.",
        slots=ConversationSlots(anchor_brand="리피토"),
    )

    resolved = resolve_anaphora("그럼 리바로 임상시험은?", previous)

    assert resolved.resolved_question == "그럼 리바로 임상시험은?"
    assert resolved.unresolved_reference is False


def test_resolve_anaphora_refuses_contrast_followup_without_grounded_intent() -> None:
    no_history = resolve_anaphora("그럼 리바로는?", None)
    vague_history = resolve_anaphora(
        "그럼 리바로는?",
        ConversationTurn(question="리피토 어때?", answer="무엇이 궁금한지 물었습니다."),
    )

    assert no_history.unresolved_reference is True
    assert vague_history.unresolved_reference is True


def test_resolve_anaphora_never_treats_period_or_metric_as_contrast_brand() -> None:
    previous = ConversationTurn(
        question="리피토 매출 추이 알려줘",
        answer="리피토 매출 추이를 확인했습니다.",
        slots=ConversationSlots(anchor_brand="리피토"),
    )

    for question in ("그럼 3분기는?", "그럼 점유율은?", "그럼 시장은?", "그럼 그건?"):
        resolved = resolve_anaphora(question, previous)

        assert resolved.resolved_question == question
        assert resolved.brand is None
        assert resolved.interpretation_notice is None
        assert resolved.unresolved_reference is True


def test_resolve_anaphora_inherits_grounded_brand_for_metric_only_followup() -> None:
    previous = ConversationTurn(
        question="리바로 매출",
        answer="리바로 매출을 확인했습니다.",
        slots=ConversationSlots(anchor_brand="리바로"),
    )

    resolved = resolve_anaphora("점유율은?", previous)

    assert resolved.resolved_question == "리바로 점유율은?"
    assert resolved.brand == "리바로"
    assert resolved.interpretation_notice == "리바로의 점유율로 이해했어요."
    assert resolved.unresolved_reference is False


def test_resolve_anaphora_inherits_grounded_brand_and_metric_for_period_only_followup() -> None:
    previous = ConversationTurn(
        question="리바로 2025년 매출",
        answer="리바로 2025년 매출을 확인했습니다.",
        slots=ConversationSlots(anchor_brand="리바로", period="2025"),
    )

    resolved = resolve_anaphora("2024년은?", previous)

    assert resolved.resolved_question == "리바로 2024년 매출은?"
    assert resolved.brand == "리바로"
    assert resolved.interpretation_notice == "리바로의 2024년 매출로 이해했어요."
    assert resolved.unresolved_reference is False


def test_period_followup_prefers_persisted_metric_when_question_text_is_opaque() -> None:
    previous = ConversationTurn(
        question="요청을 처리했습니다.",
        answer="리바로 매출을 확인했습니다.",
        slots=ConversationSlots(anchor_brand="리바로", period="2025", metric="매출"),
    )

    resolved = resolve_anaphora("2024년은?", previous)

    assert resolved.resolved_question == "리바로 2024년 매출은?"
    assert resolved.unresolved_reference is False


def test_relative_year_followup_uses_grounded_previous_period() -> None:
    previous = ConversationTurn(
        question="2024년은?",
        answer="리바로 2024년 매출을 확인했습니다.",
        slots=ConversationSlots(anchor_brand="리바로", period="2024", metric="매출"),
    )

    assert requires_previous_turn("그 전 해는?") is True

    resolved = resolve_anaphora("그 전 해는?", previous)

    assert resolved.resolved_question == "리바로 2023년 매출은?"
    assert resolved.brand == "리바로"
    assert resolved.unresolved_reference is False


def test_brand_pronoun_followup_uses_grounded_ranked_brand() -> None:
    previous = ConversationTurn(
        question="그중 1위",
        answer="1위는 로수젯입니다.",
        slots=ConversationSlots(anchor_brand="로수젯"),
    )

    assert requires_previous_turn("걔 최근 추세는?") is True

    resolved = resolve_anaphora("걔 최근 추세는?", previous)

    assert resolved.resolved_question == "로수젯 최근 추세는?"
    assert resolved.brand == "로수젯"
    assert resolved.unresolved_reference is False


def test_relative_period_and_brand_pronoun_fail_closed_without_grounded_slots() -> None:
    no_period = ConversationTurn(
        question="리바로 매출",
        answer="리바로 매출을 확인했습니다.",
        slots=ConversationSlots(anchor_brand="리바로", metric="매출"),
    )
    no_brand = ConversationTurn(
        question="상위 5개",
        answer="시장 순위를 확인했습니다.",
        slots=ConversationSlots(period="2024", metric="매출"),
    )

    relative = resolve_anaphora("그 전 해는?", no_period)
    pronoun = resolve_anaphora("걔 최근 추세는?", no_brand)

    assert relative.resolved_question == "그 전 해는?"
    assert relative.unresolved_reference is True
    assert pronoun.resolved_question == "걔 최근 추세는?"
    assert pronoun.unresolved_reference is True


def test_relative_period_followup_only_resolves_matching_grounded_granularity() -> None:
    cases = (
        ("그 다음 해는?", "2024", "리바로 2025년 매출은?"),
        ("전년은?", "2024년", "리바로 2023년 매출은?"),
        ("이전 분기는?", "2025-Q1", "리바로 2024년 4분기 매출은?"),
        ("그 다음 분기는?", "2025-Q4", "리바로 2026년 1분기 매출은?"),
        ("전월은?", "2025-01", "리바로 2024년 12월 매출은?"),
        ("그 다음 달은?", "2025-12", "리바로 2026년 1월 매출은?"),
    )

    for question, period, expected in cases:
        previous = ConversationTurn(
            question="리바로 매출",
            answer="리바로 매출을 확인했습니다.",
            slots=ConversationSlots(anchor_brand="리바로", period=period, metric="매출"),
        )

        resolved = resolve_anaphora(question, previous)

        assert resolved.resolved_question == expected
        assert resolved.unresolved_reference is False

    ambiguous = resolve_anaphora(
        "이전 분기는?",
        ConversationTurn(
            question="리바로 2024년 매출",
            answer="리바로 2024년 매출을 확인했습니다.",
            slots=ConversationSlots(anchor_brand="리바로", period="2024", metric="매출"),
        ),
    )
    assert ambiguous.resolved_question == "이전 분기는?"
    assert ambiguous.unresolved_reference is True


def test_unrelated_topic_never_inherits_previous_brand_after_pronoun_extension() -> None:
    previous = ConversationTurn(
        question="리바로 매출",
        answer="리바로 매출을 확인했습니다.",
        slots=ConversationSlots(anchor_brand="리바로", period="2024", metric="매출"),
    )

    assert requires_previous_turn("오늘 서울 날씨") is False

    resolved = resolve_anaphora("오늘 서울 날씨", previous)

    assert resolved.resolved_question == "오늘 서울 날씨"
    assert resolved.brand is None
    assert resolved.unresolved_reference is False


def test_bare_market_followups_use_grounded_previous_market() -> None:
    previous = ConversationTurn(
        question="고지혈증 시장 HHI",
        answer="고지혈증 시장의 HHI를 확인했습니다.",
        slots=ConversationSlots(
            market="ml_006",
            market_definition="고지혈증 시장",
            metric="HHI",
            view="market_landscape",
        ),
    )

    assert requires_previous_turn("시장 규모는?") is True
    assert requires_previous_turn("일반뷰로는?") is True

    market_size = resolve_anaphora("시장 규모는?", previous)
    general_view = resolve_anaphora("일반뷰로는?", previous)

    assert market_size.resolved_question == "고지혈증 시장 규모는?"
    assert market_size.unresolved_reference is False
    assert general_view.resolved_question == "고지혈증 시장 일반뷰로는?"
    assert general_view.unresolved_reference is False


def test_bare_market_followups_fail_closed_without_grounded_market() -> None:
    previous = ConversationTurn(
        question="리바로 매출",
        answer="리바로 매출을 확인했습니다.",
        slots=ConversationSlots(anchor_brand="리바로", metric="매출"),
    )

    for question in ("시장 규모는?", "일반뷰로는?"):
        resolved = resolve_anaphora(question, previous)

        assert resolved.resolved_question == question
        assert resolved.unresolved_reference is True


def test_bare_brand_switch_inherits_grounded_metric_through_existing_brand_resolver() -> None:
    previous = ConversationTurn(
        question="리바로 매출",
        answer="리바로 매출을 확인했습니다.",
        slots=ConversationSlots(anchor_brand="리바로", metric="매출"),
    )
    known_brand = lambda question: "리바로젯" in question

    assert requires_previous_turn("리바로젯은?", known_brand=known_brand) is True

    resolved = resolve_anaphora("리바로젯은?", previous, known_brand=known_brand)

    assert resolved.resolved_question == "리바로젯 매출은?"
    assert resolved.brand == "리바로젯"
    assert resolved.interpretation_notice == "리바로젯의 매출로 이해했어요."
    assert resolved.unresolved_reference is False


def test_bare_brand_switch_never_inherits_without_metric_or_known_brand() -> None:
    no_metric = ConversationTurn(
        question="리바로 알려줘",
        answer="리바로를 확인했습니다.",
        slots=ConversationSlots(anchor_brand="리바로"),
    )
    with_metric = ConversationTurn(
        question="리바로 매출",
        answer="리바로 매출을 확인했습니다.",
        slots=ConversationSlots(anchor_brand="리바로", metric="매출"),
    )
    known_brand = lambda question: "리바로젯" in question

    no_metric_result = resolve_anaphora("리바로젯은?", no_metric, known_brand=known_brand)
    unknown_brand_result = resolve_anaphora("없는브랜드는?", with_metric, known_brand=known_brand)

    assert no_metric_result.resolved_question == "리바로젯은?"
    assert no_metric_result.unresolved_reference is True
    assert unknown_brand_result.resolved_question == "없는브랜드는?"
    assert unknown_brand_result.unresolved_reference is False
    assert requires_previous_turn("없는브랜드는?", known_brand=known_brand) is False


def test_f35_followups_hydrate_persisted_context_and_reach_tool_execution(monkeypatch) -> None:
    class SharedHistory:
        def __init__(self, conversation_id: str, turn: ConversationTurn) -> None:
            self.conversation_id = conversation_id
            self.turn = turn

        def latest_turn(self, requested_conversation_id: str):
            assert requested_conversation_id == self.conversation_id
            return self.turn

    captured: list[str] = []

    def capture_answer(_resolver, _factory, _conversation_id, routed_question, *_args, **_kwargs):
        captured.append(routed_question)
        return {
            "answer": "도구 실행 결과",
            "sources": ["UBIST"],
            "tool_calls": [{"tool": "get_brand_metric", "render_data": {}}],
        }

    monkeypatch.setattr("jw_chat_agent_poc.service.app._answer_without_pending", capture_answer)
    cases = (
        (
            "f35-market-size",
            "시장 규모는?",
            ConversationTurn(
                question="고지혈증 시장 HHI",
                answer="고지혈증 시장의 HHI를 확인했습니다.",
                slots=ConversationSlots(
                    market="ml_006",
                    market_definition="고지혈증 시장",
                    metric="HHI",
                ),
            ),
            "고지혈증 시장 규모는?",
        ),
        (
            "f35-general-view",
            "일반뷰로는?",
            ConversationTurn(
                question="고지혈증 시장 HHI",
                answer="고지혈증 시장의 HHI를 확인했습니다.",
                slots=ConversationSlots(
                    market="ml_006",
                    market_definition="고지혈증 시장",
                    metric="HHI",
                ),
            ),
            "고지혈증 시장 일반뷰로는?",
        ),
        (
            "f35-brand-switch",
            "리바로젯은?",
            ConversationTurn(
                question="리바로 매출",
                answer="리바로 매출을 확인했습니다.",
                slots=ConversationSlots(anchor_brand="리바로", metric="매출"),
            ),
            "리바로젯 매출은?",
        ),
    )

    for conversation_id, question, previous_turn, expected_question in cases:
        captured.clear()
        item = _answer_question(
            SessionStore(),
            _market_scope_resolver(),
            _fake_agent_factory,
            question,
            "live",
            conversation_id,
            use_direct_agent_loop=True,
            conversation_history=SharedHistory(conversation_id, previous_turn),
        )

        assert captured == [expected_question]
        assert item["result"]["tool_calls"][0]["tool"] == "get_brand_metric"
        span_names = [span["name"] for span in item["result"]["_qa_spans"]]
        assert "conversation_history_fetch" in span_names
        assert "conversation_history_replay" in span_names
        assert "anaphora_resolution" in span_names


def test_rc3_followups_hydrate_context_and_reach_the_tool_execution_path(monkeypatch) -> None:
    class SharedHistory:
        def __init__(self, conversation_id: str, turn: ConversationTurn) -> None:
            self.conversation_id = conversation_id
            self.turn = turn

        def latest_turn(self, requested_conversation_id: str):
            assert requested_conversation_id == self.conversation_id
            return self.turn

    def capture_with(captured: list[str]):
        def capture_answer(_resolver, _factory, _conversation_id, routed_question, *_args, **_kwargs):
            captured.append(routed_question)
            return {
                "answer": "도구 실행 결과",
                "sources": ["UBIST"],
                "tool_calls": [{"tool": "get_brand_metric", "render_data": {}}],
            }

        return capture_answer

    cases = (
        (
            "relative-period-followup",
            "그 전 해는?",
            ConversationTurn(
                question="2024년은?",
                answer="리바로 2024년 매출을 확인했습니다.",
                slots=ConversationSlots(anchor_brand="리바로", period="2024", metric="매출"),
            ),
            "리바로 2023년 매출은?",
        ),
        (
            "brand-pronoun-followup",
            "걔 최근 추세는?",
            ConversationTurn(
                question="그중 1위",
                answer="1위는 로수젯입니다.",
                slots=ConversationSlots(anchor_brand="로수젯"),
            ),
            "로수젯 최근 추세는?",
        ),
    )

    for conversation_id, question, previous_turn, expected_question in cases:
        captured: list[str] = []
        monkeypatch.setattr(
            "jw_chat_agent_poc.service.app._answer_without_pending",
            capture_with(captured),
        )

        item = _answer_question(
            SessionStore(),
            _market_scope_resolver(),
            _fake_agent_factory,
            question,
            "live",
            conversation_id,
            use_direct_agent_loop=True,
            conversation_history=SharedHistory(conversation_id, previous_turn),
        )

        assert captured == [expected_question]
        assert item["result"]["tool_calls"][0]["tool"] == "get_brand_metric"
        span_names = [span["name"] for span in item["result"]["_qa_spans"]]
        assert "conversation_history_fetch" in span_names
        assert "conversation_history_replay" in span_names
        assert "anaphora_resolution" in span_names


def test_followup_hydrates_latest_persisted_turn_when_local_pod_state_is_empty(monkeypatch) -> None:
    class SharedHistory:
        def latest_turn(self, conversation_id: str):
            assert conversation_id == "cross-pod-conversation"
            return ConversationTurn(
                question="리바로 매출 추이 알려줘",
                answer="리바로 매출 추이를 확인했습니다.",
                slots=ConversationSlots(anchor_brand="리바로"),
            )

    captured: list[str] = []

    def capture_answer(_resolver, _factory, _conversation_id, question, *_args, **_kwargs):
        captured.append(question)
        return {"answer": "가드렛 매출 추이", "sources": ["UBIST"], "tool_calls": []}

    monkeypatch.setattr("jw_chat_agent_poc.service.app._answer_without_pending", capture_answer)

    item = _answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "그럼 가드렛은?",
        "live",
        "cross-pod-conversation",
        use_direct_agent_loop=True,
        conversation_history=SharedHistory(),
    )

    assert captured == ["가드렛 매출 추이는?"]
    assert item["result"]["conversation_interpretation"] == "가드렛의 매출 추이로 이해했어요."
    span_names = [span["name"] for span in item["result"]["_qa_spans"]]
    assert "conversation_history_fetch" in span_names
    assert "conversation_history_replay" in span_names
    assert "anaphora_resolution" in span_names
    assert "context_scope_resolution" in span_names
    assert "conversation_state_persist" in span_names


def test_implicit_nedrug_followup_inherits_grounded_brand() -> None:
    previous = ConversationTurn(
        question="아일리아 매출 알려줘",
        answer="아일리아 매출을 확인했습니다.",
        slots=ConversationSlots(anchor_brand="아일리아"),
    )

    assert requires_previous_turn("NeDrug 효능효과·용법용량") is True

    resolved = resolve_anaphora("NeDrug 효능효과·용법용량", previous)

    assert resolved.resolved_question == "NeDrug: 아일리아 효능효과·용법용량"
    assert resolved.brand == "아일리아"
    assert resolved.interpretation_notice == "아일리아의 효능효과·용법용량 요청으로 이해했어요."
    assert resolved.unresolved_reference is False


def test_implicit_nedrug_followup_with_topic_particle_inherits_grounded_brand() -> None:
    previous = ConversationTurn(
        question="리바로 허가정보 알려줘",
        answer="리바로 허가정보를 확인했습니다.",
        slots=ConversationSlots(anchor_brand="리바로"),
    )

    assert requires_previous_turn("효능효과는?") is True

    resolved = resolve_anaphora("효능효과는?", previous)

    assert resolved.resolved_question == "리바로 효능효과는"
    assert resolved.brand == "리바로"
    assert resolved.interpretation_notice == "리바로의 효능효과는 요청으로 이해했어요."
    assert resolved.unresolved_reference is False


def test_implicit_brand_followup_fails_closed_without_anchor() -> None:
    assert requires_previous_turn("효능효과") is True

    resolved = resolve_anaphora("효능효과", None)

    assert resolved.resolved_question == "효능효과"
    assert resolved.brand is None
    assert resolved.unresolved_reference is True


def test_independent_market_question_never_inherits_previous_brand() -> None:
    previous = ConversationTurn(
        question="아일리아 매출 알려줘",
        answer="아일리아 매출을 확인했습니다.",
        slots=ConversationSlots(anchor_brand="아일리아"),
    )

    assert requires_previous_turn("고지혈증 시장 브랜드") is False

    resolved = resolve_anaphora("고지혈증 시장 브랜드", previous)

    assert resolved.resolved_question == "고지혈증 시장 브랜드"
    assert resolved.brand is None
    assert resolved.interpretation_notice is None
    assert resolved.unresolved_reference is False


def test_implicit_nedrug_followup_hydrates_cross_pod_anchor(monkeypatch) -> None:
    class SharedHistory:
        def latest_turn(self, conversation_id: str):
            assert conversation_id == "cross-pod-nedrug-conversation"
            return ConversationTurn(
                question="아일리아 매출 알려줘",
                answer="아일리아 매출을 확인했습니다.",
                slots=ConversationSlots(anchor_brand="아일리아"),
            )

    captured: list[str] = []

    def capture_answer(_resolver, _factory, _conversation_id, question, *_args, **_kwargs):
        captured.append(question)
        return {"answer": "아일리아 허가정보", "sources": ["MFDS"], "tool_calls": []}

    monkeypatch.setattr("jw_chat_agent_poc.service.app._answer_without_pending", capture_answer)

    item = _answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "NeDrug 효능효과·용법용량",
        "live",
        "cross-pod-nedrug-conversation",
        use_direct_agent_loop=True,
        conversation_history=SharedHistory(),
    )

    assert captured == ["NeDrug: 아일리아 효능효과·용법용량"]
    assert item["result"]["conversation_interpretation"] == (
        "아일리아의 효능효과·용법용량 요청으로 이해했어요."
    )
    span_names = [span["name"] for span in item["result"]["_qa_spans"]]
    assert "conversation_history_fetch" in span_names
    assert "conversation_history_replay" in span_names


def test_deep_mode_followup_uses_resolved_state_and_discloses_interpretation(monkeypatch) -> None:
    store = SessionStore()
    store.conversations.record_exchange(
        "deep-followup",
        "가드렛 매출 추이",
        "가드렛 매출 추이를 확인했습니다.",
        slots=ConversationSlots(anchor_brand="가드렛", metric="매출", view="strategic_cd"),
    )
    captured: list[str] = []

    def capture_deep(question: str, _external_mode: str) -> dict:
        captured.append(question)
        return {"answer": "리바로 매출 추이", "sources": ["UBIST"], "tool_calls": []}

    monkeypatch.setattr("jw_chat_agent_poc.service.app._answer_deep_research", capture_deep)

    item = _answer_question(
        store,
        _market_scope_resolver(),
        _fake_agent_factory,
        "/deep 그럼 리바로는?",
        "live",
        "deep-followup",
        use_direct_agent_loop=True,
    )

    assert captured == ["리바로 매출 추이는?"]
    assert item["result"]["effective_question"] == "리바로 매출 추이는?"
    assert item["result"]["conversation_interpretation"] == "리바로의 매출 추이로 이해했어요."


def test_unresolved_deep_followup_never_calls_deep_or_web_tools(monkeypatch) -> None:
    def fail_deep(*_args, **_kwargs):
        raise AssertionError("unresolved follow-ups must stop before deep or web tools")

    monkeypatch.setattr("jw_chat_agent_poc.service.app._answer_deep_research", fail_deep)

    item = _answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "/deep 그건?",
        "live",
        "deep-unresolved",
        use_direct_agent_loop=True,
    )

    assert item["result"]["conversation_reference_unresolved"] is True
    assert item["result"]["tool_calls"] == []
    assert item["result"]["sources"] == []


def test_spaced_first_rank_followup_hydrates_verified_cross_pod_series() -> None:
    class SharedHistory:
        def latest_turn(self, conversation_id: str):
            assert conversation_id == "cross-pod-ranked-conversation"
            return ConversationTurn(
                question="고지혈증 상위 5개",
                answer="1위 로수젯",
                slots=ConversationSlots(
                    anchor_brand="리바로",
                    ranked=(_ranked_slot(),),
                    ranked_brands=("로수젯",),
                ),
            )

    item = _answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "그 중 1위 브랜드 추이는?",
        "live",
        "cross-pod-ranked-conversation",
        use_direct_agent_loop=True,
        conversation_history=SharedHistory(),
    )

    assert item["result"]["context_fact_reused"] is True
    assert item["result"]["resolution"]["canonical_brand"] == "로수젯"
    assert "2026-04" in item["result"]["answer"]


def test_complete_question_does_not_read_shared_history(monkeypatch) -> None:
    class SharedHistory:
        def latest_turn(self, _conversation_id: str):
            raise AssertionError("complete questions must not read prior history")

    monkeypatch.setattr(
        "jw_chat_agent_poc.service.app._answer_without_pending",
        lambda *_args, **_kwargs: {"answer": "리바로 매출", "sources": ["UBIST"], "tool_calls": []},
    )

    item = _answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "리바로 매출 알려줘",
        "live",
        "complete-question",
        conversation_history=SharedHistory(),
    )

    assert item["result"]["answer"] == "리바로 매출"


def test_final_answer_discloses_inherited_contrast_interpretation_once() -> None:
    result = {
        "conversation_fallback_ready": True,
        "answer": "확인된 자료를 기준으로 답변합니다.",
        "conversation_interpretation": "리바로의 매출 추이로 이해했어요.",
    }

    final = compute_final_answer("그럼 리바로는?", result, "conversation-1")

    assert final.text == (
        "리바로의 매출 추이로 이해했어요.\n\n"
        "확인된 자료를 기준으로 답변합니다."
    )
    assert final.text.count("리바로의 매출 추이로 이해했어요.") == 1


def test_reused_context_result_contains_verified_series_without_backend_call() -> None:
    result = reused_context_result(
        "그중 1위 브랜드 점유율 추이는?",
        _ranked_slot(),
        ConversationSlots(market="ml_006", denominator="2256.77억원"),
    )

    assert result["context_fact_reused"] is True
    assert [call["tool"] for call in result["tool_calls"]] == ["conversation_context"]
    data = result["tool_calls"][0]["render_data"]
    assert data["brand"] == "로수젯"
    assert data["market_id"] == "ml_006"
    assert data["period"] == "2026-04"
    assert data["brand_value_series_10pt"][-1]["ms_pct"] == 9.1659
    assert "로수젯" in result["markdown_response"]["fact_md"]
    assert "| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |" in result["answer"]
    assert "직전 턴에서 이미 조회한 검증 fact" not in result["answer"]
