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


def test_resolve_anaphora_uses_anchor_market_for_ranked_brand_requery() -> None:
    previous = ConversationTurn(
        question="상위 3개",
        answer="1위 로수젯",
        slots=ConversationSlots(anchor_brand="리바로", ranked_brands=("로수젯",)),
    )

    resolved = resolve_anaphora("그중 1위 브랜드 점유율 추이는?", previous)

    assert resolved.resolved_question == "리바로 시장의 로수젯 점유율 추이는?"
    assert resolved.reusable_ranked is None


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
