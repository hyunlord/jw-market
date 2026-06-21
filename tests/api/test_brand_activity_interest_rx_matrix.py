from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetResolution
from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice, BrandMeta, CsdCrosswalk, ViewConfig
from pipeline.scripts.api.routes import brand_activity


def test_interest_rx_route_wraps_success_envelope(monkeypatch) -> None:
    # Given
    expected = {"scope": {"view": "general"}, "brands": [], "market_average": {}}
    monkeypatch.setattr(brand_activity, "get_interest_rx_matrix", lambda _payload: expected, raising=False)
    app = FastAPI()
    app.include_router(brand_activity.router)

    # When
    response = TestClient(app).post(
        "/api/brand-activity/interest-rx-matrix",
        json={"view": "general", "market_id": "C10A1", "selected_brand": "리바로", "filter": {}},
    )

    # Then
    assert response.status_code == 200
    assert response.json() == {"data": expected}


def test_interest_rx_service_returns_dynamic_period_distributions_and_scores(monkeypatch) -> None:
    # Given
    from pipeline.scripts.api import brand_activity_interest_rx_matrix as service
    from pipeline.scripts.api import brand_activity_interest_rx_source as source

    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: _brand_set())
    monkeypatch.setattr(service, "resolve_csd_market", lambda _codes: _crosswalk())
    monkeypatch.setattr(service, "_alias_lookup", lambda: {})
    monkeypatch.setattr(source.db, "fetch_all", _fetch_all)

    # When
    payload = service.get_interest_rx_matrix({"view": "general", "market_id": "C10A1", "selected_brand": "리바로"})

    # Then
    assert payload is not None
    assert payload["period"] == {
        "start": "2023-12",
        "end": "2025-12",
        "default_start": "2023-12",
        "default_end": "2025-12",
        "source": "dynamic_overlap",
    }
    brands = {brand["brand_key"]: brand for brand in payload["brands"]}
    assert [brand["brand_key"] for brand in payload["brands"]] == ["리바로", "리피토", "크레스토"]
    assert brands["리바로"]["event_count"] == 3
    assert brands["리바로"]["confidence"] == "insufficient"
    assert brands["리바로"]["detailing"] == 100.0
    assert brands["리바로"]["interest_distribution"] == {"VERY USEFUL": 2, "SOMEWHAT USEFUL": 1, "NOT AT ALL": 0}
    assert brands["리바로"]["rx_frequency_distribution"]["frequently"] == 2
    assert brands["리바로"]["rx_frequency_distribution"]["occasionally"] == 1
    assert brands["리바로"]["interest_score"] == pytest.approx((2.0 + 0.5) / 3.0)
    assert brands["리바로"]["rx_frequency_score"] == pytest.approx((2.0 + 0.6) / 3.0)
    assert brands["리피토"]["event_count"] == 4
    assert brands["리피토"]["interest_distribution"] == {"VERY USEFUL": 0, "SOMEWHAT USEFUL": 3, "NOT AT ALL": 1}
    assert payload["market_average"]["event_count"] == 19
    assert payload["market_average"]["event_count"] != sum(brand["event_count"] for brand in payload["brands"])
    assert payload["scope"]["csd_market"] == "LIVALO"


def test_interest_rx_service_rebuilds_distribution_for_specialty_filter(monkeypatch) -> None:
    # Given
    from pipeline.scripts.api import brand_activity_interest_rx_matrix as service
    from pipeline.scripts.api import brand_activity_interest_rx_source as source

    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: _brand_set())
    monkeypatch.setattr(service, "resolve_csd_market", lambda _codes: _crosswalk())
    monkeypatch.setattr(service, "_alias_lookup", lambda: {})
    monkeypatch.setattr(source.db, "fetch_all", _fetch_all)

    # When
    payload = service.get_interest_rx_matrix(
        {"view": "general", "market_id": "C10A1", "selected_brand": "리바로", "specialty": "Cardio"}
    )

    # Then
    assert payload is not None
    livalo = next(brand for brand in payload["brands"] if brand["brand_key"] == "리바로")
    assert livalo["event_count"] == 1
    assert livalo["interest_distribution"] == {"VERY USEFUL": 1, "SOMEWHAT USEFUL": 0, "NOT AT ALL": 0}
    assert payload["filters_applied"]["specialty"] == "Cardio"


def test_interest_rx_service_applies_weight_overrides(monkeypatch) -> None:
    # Given
    from pipeline.scripts.api import brand_activity_interest_rx_matrix as service
    from pipeline.scripts.api import brand_activity_interest_rx_source as source

    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: _brand_set())
    monkeypatch.setattr(service, "resolve_csd_market", lambda _codes: _crosswalk())
    monkeypatch.setattr(service, "_alias_lookup", lambda: {})
    monkeypatch.setattr(source.db, "fetch_all", _fetch_all)

    # When
    payload = service.get_interest_rx_matrix(
        {
            "view": "general",
            "market_id": "C10A1",
            "selected_brand": "리바로",
            "weights": {"interest": {"SOMEWHAT USEFUL": 0.25}, "rx_frequency": {"occasionally": 0.2}},
        }
    )

    # Then
    assert payload is not None
    livalo = next(brand for brand in payload["brands"] if brand["brand_key"] == "리바로")
    assert livalo["interest_score"] == pytest.approx((2.0 + 0.25) / 3.0)
    assert livalo["rx_frequency_score"] == pytest.approx((2.0 + 0.2) / 3.0)
    assert payload["weights"]["interest"]["SOMEWHAT USEFUL"] == 0.25


def test_interest_rx_service_marks_thin_slice_insufficient(monkeypatch) -> None:
    # Given
    from pipeline.scripts.api import brand_activity_interest_rx_matrix as service
    from pipeline.scripts.api import brand_activity_interest_rx_source as source

    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: _brand_set())
    monkeypatch.setattr(service, "resolve_csd_market", lambda _codes: _crosswalk())
    monkeypatch.setattr(service, "_alias_lookup", lambda: {})
    monkeypatch.setattr(source.db, "fetch_all", _fetch_all)

    # When
    payload = service.get_interest_rx_matrix(
        {"view": "general", "market_id": "C10A1", "selected_brand": "리바로", "specialty": "Psy"}
    )

    # Then
    assert payload is not None
    assert all(brand["confidence"] == "insufficient" for brand in payload["brands"])
    assert payload["market_average"]["event_count"] == 1


def test_interest_rx_service_uses_select_only_sql() -> None:
    # Given
    source = "\n".join(
        path.read_text()
        for path in [
            Path("pipeline/scripts/api/brand_activity_interest_rx_matrix.py"),
            Path("pipeline/scripts/api/brand_activity_interest_rx_source.py"),
        ]
    )
    forbidden = ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "CREATE ", "ALTER ", "TRUNCATE ", "REPLACE ")

    # When
    has_write_token = any(token in source.upper() for token in forbidden)

    # Then
    assert has_write_token is False


def _brand_set() -> BrandSetResolution:
    view = ViewConfig("mart_general_brand_metric", "mart_general_market_metric", "atc4_code", "atc4_desc", "brand_ranking", False)
    brand_meta = {
        "리바로": BrandMeta("리바로", "리바로", ("LIVALO",), True),
        "리피토": BrandMeta("리피토", "리피토", ("LIPITOR",), False),
        "크레스토": BrandMeta("크레스토", "크레스토", ("CRESTOR",), False),
    }
    return BrandSetResolution(
        view_name="general",
        market_id="C10A1",
        selected_brand="리바로",
        view=view,
        market_row={"atc4_desc": "LIVALO C10A1"},
        brand_rows=(),
        brand_meta=brand_meta,
        choices=(
            BrandChoice("리바로", "리바로", 3, True),
            BrandChoice("리피토", "리피토", 1, False),
            BrandChoice("크레스토", "크레스토", 2, False),
        ),
        candidates=(),
        ranking_quarter="2025-Q4",
        applied_filter={"atc4": ["C10A1"]},
    )


def _crosswalk() -> CsdCrosswalk:
    return CsdCrosswalk(market="LIVALO Market", display_market="LIVALO", overlap=("LIVALO", "LIPITOR", "CRESTOR"), score=3)


def _fetch_all(sql: str, params=None) -> list[dict[str, str | int | float]]:
    params_tuple = tuple(params or ())
    if "MIN(period_ym)" in sql:
        return [
            {"source": "keyword", "min_period": "2023-12", "max_period": "2026-04"},
            {"source": "csd", "min_period": "2023-05", "max_period": "2025-12"},
        ]
    if "km_keyword_event_stage" in sql:
        return _keyword_rows(params_tuple)
    if "csd_channel_dynamics_stage" in sql:
        return [
            {"master_product": "LIVALO", "detailing": 100.0},
            {"master_product": "LIPITOR", "detailing": 80.0},
            {"master_product": "CRESTOR", "detailing": 60.0},
        ]
    return []


def _keyword_rows(params: tuple[str, ...]) -> list[dict[str, str | int]]:
    if "Psy" in params:
        return [_keyword("LIVALO", "SOMEWHAT USEFUL", "occasionally", "remain unchanged", 1)]
    if "Cardio" in params:
        return [
            _keyword("LIVALO", "VERY USEFUL", "frequently", "increase (or will begin to prescribe)", 1),
            _keyword("LIPITOR", "NOT AT ALL", "never", "decrease", 1),
            _keyword("OPEN MARKET", "VERY USEFUL", "frequently", "increase (or will begin to prescribe)", 5),
        ]
    return [
        _keyword("LIVALO", "VERY USEFUL", "frequently", "increase (or will begin to prescribe)", 2),
        _keyword("LIVALO", "SOMEWHAT USEFUL", "occasionally", "remain unchanged", 1),
        _keyword("LIPITOR", "SOMEWHAT USEFUL", "occasionally", "remain unchanged", 3),
        _keyword("LIPITOR", "NOT AT ALL", "never", "decrease", 1),
        _keyword("CRESTOR", "VERY USEFUL", "frequently", "increase (or will begin to prescribe)", 1),
        _keyword("OPEN MARKET", "VERY USEFUL", "frequently", "increase (or will begin to prescribe)", 11),
    ]


def _keyword(product_name: str, interest: str, rx: str, evolution: str, count: int) -> dict[str, str | int]:
    return {
        "product_name": product_name,
        "interest": interest,
        "prescription_frequency": rx,
        "prescription_evolution": evolution,
        "event_count": count,
    }
