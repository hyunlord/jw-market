from __future__ import annotations

from jw_chat_agent_poc.service.conversation import ConversationSlots, ConversationTurn, RankedBrandSlot, SeriesPoint
from jw_chat_agent_poc.service.conversation_context import extract_conversation_slots, resolve_anaphora, reused_context_result
from jw_chat_agent_poc.service.app import compute_final_answer


def _ranked_slot() -> RankedBrandSlot:
    return RankedBrandSlot(
        brand="로수젯",
        rank=1,
        series=(
            SeriesPoint(period="2026-03", value_krw=19_500_000_000.0, ms_pct=8.7, rank=1),
            SeriesPoint(period="2026-04", value_krw=20_685_385_934.33, ms_pct=9.1659, rank=1),
        ),
    )


def test_extract_slots_keeps_anchor_market_period_denominator_and_ranked_series() -> None:
    result = {
        "resolution": {"canonical_brand": "리바로"},
        "tool_calls": [
            {
                "tool": "get_brand_metric",
                "render_data": {
                    "market_id": "ml_006",
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
    assert slots.denominator == "2256.77억원"
    assert slots.ranked_brands == ("로수젯",)
    assert slots.ranked[0].series[-1].ms_pct == 9.1659


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


def test_resolve_anaphora_never_guesses_without_previous_basis() -> None:
    resolved = resolve_anaphora("그 브랜드 점유율 추이는?", None)

    assert resolved.resolved_question == "그 브랜드 점유율 추이는?"
    assert resolved.brand is None
    assert resolved.unresolved_reference is True


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
