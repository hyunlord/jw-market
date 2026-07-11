from __future__ import annotations

from jw_chat_agent_poc.service.conversation import ConversationSlots, ConversationTurn, RankedBrandSlot, SeriesPoint
from jw_chat_agent_poc.service.conversation_context import extract_conversation_slots, resolve_anaphora, reused_context_result


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
