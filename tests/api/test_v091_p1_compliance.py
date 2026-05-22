from __future__ import annotations

import json
import urllib.request


BASE_URL = "http://127.0.0.1:8013"
EXPECTED_TOP_LEVEL = {
    "rank",
    "company",
    "market_name_short",
    "mkt_team",
    "atc_codes",
    "atc_desc",
    "nhi_type",
    "sources",
}
EXPECTED_BACK_EXTENDED = {
    "brand_cagr_5y_pct",
    "excess_growth_pct",
    "source_label",
    "is_dual_source",
    "sources",
    "market_definition_label",
    "market_definition_full",
    "atc_count",
    "direct_competition_count",
    "market_label_kor",
}


def get_api(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as response:
        return json.load(response)


def market_status() -> dict:
    return get_api("/api/market-status")


def card_for(brand: str) -> dict:
    cards = market_status()["brand_cards"]
    return next(card for card in cards if card["brand"] == brand)


def test_brand_cards_top_level_p1_fields_exist() -> None:
    for card in market_status()["brand_cards"]:
        missing = EXPECTED_TOP_LEVEL - set(card)
        assert not missing, f"{card['brand']} missing top-level fields: {sorted(missing)}"
        assert isinstance(card["rank"], int)
        assert card["company"]
        assert card["market_name_short"]
        assert card["mkt_team"]
        assert isinstance(card["atc_codes"], list)
        assert card["atc_codes"]
        assert card["atc_desc"]
        assert card["nhi_type"]


def test_brand_cards_sources_are_normalized() -> None:
    for card in market_status()["brand_cards"]:
        assert card["sources"] in (["UBIST"], ["IQVIA"], ["UBIST", "IQVIA"])
        assert card["back_extended"]["sources"] == card["sources"]


def test_brand_cards_back_period_first_exists() -> None:
    for card in market_status()["brand_cards"]:
        assert "period_first" in card["back"]
        assert card["back"]["period_first"], card["brand"]


def test_brand_cards_back_extended_p1_fields_exist() -> None:
    for card in market_status()["brand_cards"]:
        missing = EXPECTED_BACK_EXTENDED - set(card["back_extended"])
        assert not missing, f"{card['brand']} missing back_extended fields: {sorted(missing)}"
        assert card["back_extended"]["source_label"]
        assert isinstance(card["back_extended"]["is_dual_source"], bool)
        assert card["back_extended"]["market_definition_label"]
        assert card["back_extended"]["market_definition_full"]
        assert card["back_extended"]["market_label_kor"]


def test_brand_cards_atc_count_matches_atc_codes() -> None:
    for card in market_status()["brand_cards"]:
        assert card["back_extended"]["atc_count"] == len(card["atc_codes"]), card["brand"]


def test_brand_cards_excess_growth_calculation_when_cagr_available() -> None:
    for card in market_status()["brand_cards"]:
        back_extended = card["back_extended"]
        brand_cagr = back_extended["brand_cagr_5y_pct"]
        market_cagr = back_extended["market_cagr_5y_pct"]
        excess = back_extended["excess_growth_pct"]
        if brand_cagr is None or market_cagr is None:
            assert excess is None
            continue
        assert abs(excess - round(brand_cagr - market_cagr, 4)) < 0.01, card["brand"]


def test_dual_source_cards_have_ubist_iqvia_order() -> None:
    for brand in ("가드메트", "엔커버"):
        card = card_for(brand)
        assert card["sources"] == ["UBIST", "IQVIA"]
        assert card["back_extended"]["sources"] == ["UBIST", "IQVIA"]
        assert card["back_extended"]["is_dual_source"] is True
        assert card["back_extended"]["source_label"] == "UBIST + IQVIA"


def test_direct_competition_count_populated_for_cd_markets() -> None:
    for brand in ("라베칸", "가드메트", "엔커버"):
        count = card_for(brand)["back_extended"]["direct_competition_count"]
        assert isinstance(count, int)
        assert count > 0


def test_market_label_kor_present_for_all_cards() -> None:
    for card in market_status()["brand_cards"]:
        assert card["back_extended"]["market_label_kor"], card["brand"]
