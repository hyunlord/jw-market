from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.scripts.api.market_id import to_ml_id, to_strategy_id
from pipeline.scripts.api.models.brand import BrandResponse
from pipeline.scripts.api.services import build_brands_response


def test_market_id_helpers_keep_db_ids_out_of_api_contract() -> None:
    assert to_strategy_id("ml_006") == "strategy_006"
    assert to_ml_id("strategy_006") == "ml_006"
    assert to_strategy_id("strategy_006") == "strategy_006"
    assert to_ml_id("ml_006") == "ml_006"


def test_build_brands_response_matches_v090_flat_contract() -> None:
    brands = build_brands_response()

    assert isinstance(brands, list)
    assert len(brands) == 25
    assert all(isinstance(BrandResponse.model_validate(brand), BrandResponse) for brand in brands)

    리바로 = next(brand for brand in brands if brand["brand"] == "리바로")
    assert 리바로 == {
        "brand": "리바로",
        "market_id": "strategy_006",
        "market_name": "리바로/리바로젯",
        "market_name_short": "리바로",
        "market_label_kor": "고지혈증",
        "mkt_team": "MKT 1팀",
        "sources": ["UBIST"],
        "atc_codes": ["C10A1", "C10C0", "C10C"],
        "atc_desc": "STATINS (HMG-COA RED) + 복합제제",
        "is_jw": True,
        "is_target": True,
        "is_dual_source": False,
        "rank": 1,
    }


def test_build_brands_response_filters_by_query_and_market_id() -> None:
    query_results = build_brands_response(q="리바")
    assert [brand["brand"] for brand in query_results] == [
        "리바로",
        "리바로젯",
        "리바로페노",
        "리바로하이",
        "리바로브이",
    ]

    market_results = build_brands_response(market_id="strategy_006")
    assert [brand["brand"] for brand in market_results] == ["리바로", "리바로젯"]

    combined = build_brands_response(q="젯", market_id="strategy_006")
    assert [brand["brand"] for brand in combined] == ["리바로젯"]
