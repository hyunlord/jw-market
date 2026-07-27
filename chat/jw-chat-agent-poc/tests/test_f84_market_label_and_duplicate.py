from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from jw_chat_agent_poc.orchestrator.general_view_contract import enforce_general_view_contract
from jw_chat_agent_poc.resolver.brand_resolver import BrandResolver
from jw_chat_agent_poc.resolver.catalog_membership import (
    StaticCatalogMembershipReader,
    TtlCatalogMembershipReader,
)
from jw_chat_agent_poc.service.general_view_routing import GeneralViewService
from jw_chat_agent_poc.service.markdown_cleanup import cleanup_markdown_answer
from jw_chat_agent_poc.tools.general_view_backend import AtcCandidate, GeneralMarket, TopBrand
from jw_chat_agent_poc.tools.metrics.cache_live import StaticMetricsCacheReader


_LIVE_CAPTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "f84_gen1116_live_capture.json").read_text(encoding="utf-8")
)


class _GeneralMembership:
    @staticmethod
    def resolve(brand: str, source: str) -> SimpleNamespace | None:
        candidates = {
            "리바로": (AtcCandidate("C10A1", "지질조절제"),),
            "리바로젯": (
                AtcCandidate("C10A1", "지질조절제"),
                AtcCandidate("C10C", "지질조절제 복합제제"),
            ),
        }.get(brand)
        return SimpleNamespace(candidates=candidates, brand_key=brand) if candidates else None


class _GeneralBackend:
    @staticmethod
    def candidates(brand: str, source: str) -> tuple[AtcCandidate, ...]:
        return ()

    @staticmethod
    def market(atc4: str, brand: str | None, source: str, measure: str) -> GeneralMarket:
        return GeneralMarket(
            view_type="general_view",
            market_basis="ATC4",
            atc4_code=atc4,
            atc4_description="지질조절제" if atc4 == "C10A1" else "지질조절제 복합제제",
            source="UBIST",
            measure="sales",
            unit="KRW",
            period="2026-05",
            market_size=100_000_000_000.0 if atc4 == "C10A1" else 40_000_000_000.0,
            brand=brand,
            brand_value=None,
            brand_share_pct=None,
            brand_rank=None,
            top_brands=(),
            market_size_series=(("2026-05", 1.0),),
        )


def _live_catalog_resolver() -> BrandResolver:
    membership = TtlCatalogMembershipReader(
        StaticCatalogMembershipReader(
            (
                {
                    "brand": "리바로",
                    "market_id": "ml_006",
                    "market_name": "리바로 리바로젯",
                },
                {
                    "brand": "리바로젯",
                    "market_id": "ml_006",
                    "market_name": "리바로 리바로젯",
                },
            )
        ),
        ttl_seconds=300,
    )
    return BrandResolver(
        mode="cache",
        brand_reader=StaticMetricsCacheReader(cache_brands=[], market_status=[]),
        membership_reader=membership,
    )


def test_gen1116_capture_contains_the_market_label_and_split_regressions() -> None:
    captures = _LIVE_CAPTURE["captures"]

    assert "| 전략뷰 | 리바로 리바로젯 |" in captures["strategic_hhi"]["text"]
    assert captures["general_followup"]["text"].startswith("리바로 리바로젯의 일반뷰로는")
    assert "[C10C]" in captures["general_followup"]["text"]
    assert "[C10A1]" not in captures["general_followup"]["text"]
    assert "리바로 리바로젯 시장의 구성 브랜드" in captures["strategic_members"]["text"]


def test_live_catalog_brand_list_uses_the_existing_human_market_alias() -> None:
    resolution = _live_catalog_resolver().resolve("리바로 시장 구성 브랜드 50개")

    assert resolution.market_id == "ml_006"
    assert resolution.market_name == "고지혈증 치료제 시장"
    assert "리바로 리바로젯" not in resolution.market_names


def test_internal_market_id_uses_the_existing_human_market_alias() -> None:
    resolver = _live_catalog_resolver()

    assert resolver._public_market_name("ml_006", "ml_006", {"리바로", "리바로젯"}) == "고지혈증 치료제 시장"


def test_human_market_alias_keeps_the_f47_two_atc_split() -> None:
    resolver = _live_catalog_resolver()
    service = GeneralViewService(
        _GeneralBackend(),
        resolver,
        enabled=True,
        general_membership=_GeneralMembership(),
    )

    result = service.answer("고지혈증 치료제 시장 일반뷰로는?", compact=False, dual=False)

    assert result["general_view_contract"]["atc4_codes"] == ["C10A1", "C10C"]
    assert "### ATC4 C10A1" in result["answer"]
    assert "### ATC4 C10C" in result["answer"]
    assert "1,400.00억원" not in result["answer"]


def test_cleaned_general_member_section_is_not_appended_twice() -> None:
    members = tuple(
        TopBrand(brand=brand, rank=rank, value=None, share_pct=None)
        for rank, brand in enumerate(
            ("아일리아", "비오뷰", "바비스모", "아이덴젤트", "루센티스", "루센비에스", "아필리부", "비젠프리", "아멜리부"),
            1,
        )
    )
    market = GeneralMarket(
        view_type="general_view",
        market_basis="ATC4",
        atc4_code="S01P0",
        atc4_description="OPHTHALMOLOGICALS",
        source="IQVIA",
        measure="sales",
        unit="KRW",
        period="2026-Q1",
        market_size=10_000_000_000.0,
        brand="아일리아",
        brand_value=1_000_000_000.0,
        brand_share_pct=10.0,
        brand_rank=1,
        top_brands=members[:5],
        market_size_series=(),
        member_brands=members,
    )
    result = GeneralViewService(
        _GeneralBackendForMarket(market),
        SimpleNamespace(),
        enabled=True,
    ).answer("아일리아 시장 구성 브랜드 50개", compact=False, dual=False)

    answer = enforce_general_view_contract(
        cleanup_markdown_answer(result["answer"]),
        result["general_view_contract"],
    )

    assert _LIVE_CAPTURE["captures"]["general_members"]["text"].count("### 시장 구성") == 2
    assert answer.count("### 시장 구성") == 1
    assert answer.count("### 구성 브랜드") == 1


class _GeneralBackendForMarket:
    def __init__(self, market: GeneralMarket) -> None:
        self._market = market

    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]:
        return (AtcCandidate(self._market.atc4_code, self._market.atc4_description),)

    def market(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMarket:
        return self._market
