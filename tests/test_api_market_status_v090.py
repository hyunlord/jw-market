from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.scripts.api.models.market_status import MarketStatusCard
from pipeline.scripts.api.services import build_market_status_response, filter_market_status_cards


def test_build_market_status_response_matches_v090_card_contract() -> None:
    cards = build_market_status_response()

    assert isinstance(cards, list)
    assert len(cards) == 25
    assert all(isinstance(MarketStatusCard.model_validate(card), MarketStatusCard) for card in cards)

    first = cards[0]
    assert {
        "rank",
        "brand",
        "company",
        "is_jw",
        "is_target",
        "market_id",
        "market_name",
        "market_name_short",
        "mkt_team",
        "atc_codes",
        "atc_desc",
        "sources",
        "front",
        "back",
        "back_extended",
    } <= set(first)
    assert set(first["sources"]) == set(first["front"]["sources_data"])
    assert first["front"]["default_source"] in first["front"]["sources_data"]


def test_market_status_filter_keeps_strategy_id_contract() -> None:
    cards = build_market_status_response()
    filtered = filter_market_status_cards(cards, market_id="strategy_006")

    assert [card["brand"] for card in filtered] == ["리바로", "리바로젯"]
    assert {card["market_id"] for card in filtered} == {"strategy_006"}
