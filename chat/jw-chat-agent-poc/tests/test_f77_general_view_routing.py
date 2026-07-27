from __future__ import annotations

from jw_chat_agent_poc.resolver.catalog_membership import (
    StaticCatalogMembershipReader,
    TtlCatalogMembershipReader,
)
from jw_chat_agent_poc.service.conversation import ConversationTurn
from jw_chat_agent_poc.service.conversation_context import (
    extract_conversation_slots,
    resolve_anaphora,
)
from jw_chat_agent_poc.service.general_view_routing import GeneralRoute, GeneralViewService
from jw_chat_agent_poc.tools.metrics.cache_live import StaticMetricsCacheReader
from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver


class _StrategicQueryLayer:
    @staticmethod
    def _base_data() -> dict:
        return {
            "status": "ok",
            "market": "ml_006",
            "market_id": "ml_006",
            "market_name": "ml_006",
            "scope": "market",
            "scope_label": "시장 전체",
            "period": "2026-05",
            "anchor_brand": "리바로",
            "total_brands_in_market": 555,
            "market_size_recent_krw": 213_925_043_319.36,
            "market_size_억원": 2_139.2504331936,
            "hhi_recent": 253.6207,
            "level_segments": [],
            "source_label": "UBIST",
        }

    def market_scope_from_mart(self, brand: str) -> dict:
        assert brand == "리바로"
        return {
            "source": "UBIST",
            "tool": "get_market_landscape",
            "summary_text": "리바로 시장 범위를 조회했습니다.",
            "render_data": self._base_data(),
        }

    def market_members(
        self,
        brand: str = "",
        *,
        market: str | None = None,
        period: str = "latest",
        limit: int = 20,
        include_other: bool = False,
    ) -> dict:
        assert brand == "리바로"
        assert market is None
        assert period == "latest"
        assert limit == 20
        assert include_other is False
        data = self._base_data()
        data.update(
            {
                "scope_label": "시장 구성 브랜드",
                "member_brands": ("로수젯", "리피토"),
                "displayed_brand_count": 2,
            }
        )
        return {
            "source": "UBIST",
            "tool": "get_market_members",
            "summary_text": "ml_006 시장의 구성 브랜드를 전략 mart에서 조회했습니다.",
            "render_data": data,
        }


def _resolver() -> MarketScopeResolver:
    memberships = TtlCatalogMembershipReader(
        StaticCatalogMembershipReader(
            (
                {
                    "brand": "리바로",
                    "market_id": "ml_006",
                    "market_name": "고지혈증 시장",
                },
            )
        ),
        ttl_seconds=300,
    )
    return MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=[], market_status={}),
        membership_reader=memberships,
        query_layer=_StrategicQueryLayer(),  # type: ignore[arg-type]
    )


def test_general_view_followup_preserves_catalog_market_label() -> None:
    first = _resolver().answer_monthly_market_golden(
        "고지혈증 시장 HHI",
        anchor_brand="리바로",
    )
    slots = extract_conversation_slots(first)
    previous = ConversationTurn(question="고지혈증 시장 HHI", answer=first["answer"], slots=slots)

    resolution = resolve_anaphora("일반뷰로는?", previous)
    route = GeneralViewService(object(), object(), enabled=True).route(resolution.resolved_question)

    assert slots.market == "ml_006"
    assert slots.market_definition == "고지혈증 시장"
    assert resolution.resolved_question == "고지혈증 시장 일반뷰로는?"
    assert resolution.interpretation_notice == "고지혈증 시장의 일반뷰로는 요청으로 이해했어요."
    assert route is GeneralRoute.GENERAL_ONLY


def test_brand_market_members_preserve_catalog_market_label() -> None:
    result = _resolver().answer(
        "리바로 시장 구성 브랜드 50개",
        view_type="market_landscape",
    )

    data = result["tool_calls"][0]["render_data"]
    assert data["market_id"] == "ml_006"
    assert data["market_name"] == "고지혈증 시장"
    assert "고지혈증 시장의 구성 브랜드" in result["tool_calls"][0]["summary_text"]
    assert "고지혈증 시장" in result["answer"]
    assert "ml_006" not in result["answer"]
