from __future__ import annotations

from decimal import Decimal

import pytest

from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.composers.number_format import deep_format_numbers
from pipeline.scripts.api.dynamic_market import cause_payload
from pipeline.scripts.api.dynamic_market.cause_payload import build_cause_payload
from pipeline.scripts.api.dynamic_market.cause_time import hhi_series
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric, MarketDefinition


def _brand(
    key: str,
    values: tuple[tuple[str, float], ...],
    *,
    rank: int,
) -> BrandMetric:
    total = sum(value for _period, value in values)
    latest_period, latest_value = values[-1]
    return BrandMetric(
        key,
        key.upper(),
        "S01P0",
        total,
        0.0,
        rank,
        latest_period,
        latest_value,
        tuple({"period": period, "value": value} for period, value in values),
    )


def _metrics(
    brands: tuple[BrandMetric, ...],
    *,
    source: str = "ubist",
) -> AggregatedMetrics:
    periods = sorted(
        {
            str(point["period"])
            for brand in brands
            for point in brand.monthly_series
        }
    )
    monthly = tuple(
        {
            "period": period,
            "market_size": sum(
                float(point["value"])
                for brand in brands
                for point in brand.monthly_series
                if point["period"] == period
            ),
        }
        for period in periods
    )
    return AggregatedMetrics(
        source=source,
        measure="sales",
        unit_label="KRW",
        market_size=sum(float(item["market_size"]) for item in monthly),
        hhi=None,
        cagr=None,
        monthly_series=monthly,
        brands=brands,
        all_brands=brands,
    )


def _general_definition(*, source: str = "ubist") -> MarketDefinition:
    return MarketDefinition(
        view="general",
        filter_echo={"view": "general", "atc4": ["S01P0"], "source": source, "measure": "sales"},
        source=source,
        measure="sales",
    )


def test_hhi_series_squares_raw_shares_without_intermediate_rounding() -> None:
    values = (12_345.678, 98_765.432, 33_333.333)
    brands = tuple(
        _brand(key, (("2025-01", value),), rank=index)
        for index, (key, value) in enumerate(
            zip(("a", "b", "c"), values, strict=True),
            start=1,
        )
    )
    market = sum(values)
    expected = sum((value / market * 100) ** 2 for value in values)

    actual = hhi_series(brands)[0]["hhi"]

    assert actual == pytest.approx(expected, abs=1e-12)
    assert actual != 5280.8856


@pytest.mark.parametrize("formatter", [deep_format_numbers, compose_cached_json])
def test_hhi_fields_round_half_up_without_changing_global_truncation(formatter) -> None:
    payload = {
        "hhi_recent": 3188.040362260885,
        "hhi_series_5y": [{"period": "2026", "hhi": Decimal("3015.4124533412323")}],
        "company_concentration_trend": {"hhi_values": [5652.065915370253]},
        "market_size_recent": 3188.040362260885,
    }

    actual = formatter(payload)

    assert actual["hhi_recent"] == 3188.0404
    assert actual["hhi_series_5y"][0]["hhi"] == 3015.4125
    assert actual["company_concentration_trend"]["hhi_values"] == [5652.0659]
    assert actual["market_size_recent"] == 3188.0403


@pytest.mark.parametrize(
    ("raw_hhi", "display_hhi"),
    [
        (3188.040362260885, 3188.0404),
        (3015.4124533412323, 3015.4125),
        (5652.065915370253, 5652.0659),
        (2773.840547521344, 2773.8405),
        (717.6910084589456, 717.6910),
        (1092.7497212632295, 1092.7497),
    ],
)
def test_hhi_golden_values_round_only_at_display_boundary(
    raw_hhi: float,
    display_hhi: float,
) -> None:
    assert deep_format_numbers({"hhi_recent": raw_hhi})["hhi_recent"] == display_hhi


def test_general_unbounded_hhi_recent_uses_same_latest_period_as_market_size(monkeypatch) -> None:
    monkeypatch.setattr(
        cause_payload,
        "build_analysis_level_sections",
        lambda **_kwargs: {},
    )
    complete_a = tuple((f"2025-{month:02d}", 75.0) for month in range(1, 13))
    complete_b = tuple((f"2025-{month:02d}", 25.0) for month in range(1, 13))
    latest_a = (("2026-01", 10.0),)
    latest_b = (("2026-01", 90.0),)
    brands = (
        _brand("a", complete_a + latest_a, rank=1),
        _brand("b", complete_b + latest_b, rank=2),
    )

    payload = build_cause_payload(definition=_general_definition(), metrics=_metrics(brands))

    assert payload["data"]["market_size_series"][-1]["period"] == "2026-01"
    assert payload["data"]["kpi"]["market_size_recent"] == 100.0
    assert payload["data"]["hhi_recent"] == pytest.approx(8200.0)
    assert payload["data"]["kpi"]["hhi_recent"] == pytest.approx(8200.0)
    assert payload["data"]["hhi_series_5y"][-1]["period"] == "2025"


def test_member_population_layers_are_additive_and_distinct(monkeypatch) -> None:
    monkeypatch.setattr(
        cause_payload,
        "build_analysis_level_sections",
        lambda **_kwargs: {},
    )
    values = (
        21_867_326_960.0,
        8_159_719_897.0,
        4_084_678_829.0,
        2_736_900_000.0,
        2_149_659_065.0,
        1_328_074_813.0,
        1_302_801_381.0,
        509_040_000.0,
        421_363_416.0,
        0.0,
    )
    brands = tuple(
        _brand(key, (("2026-Q1", value),), rank=index)
        for index, (key, value) in enumerate(
            zip("abcdefghij", values, strict=True),
            start=1,
        )
    )

    payload = build_cause_payload(
        definition=_general_definition(source="iqvia_nsa"),
        metrics=_metrics(brands, source="iqvia_nsa"),
    )
    data = payload["data"]

    assert data["member_population"] == {
        "count": 10,
        "members": [
            {"brand_key": key, "brand": key.upper()}
            for key in "abcdefghij"
        ],
    }
    assert data["active_members"] == {
        "period": "2026-Q1",
        "count": 9,
        "members": [
            {"brand_key": key, "brand": key.upper()}
            for key in "abcdefghi"
        ],
    }
    assert data["display_members"] == {
        "top_n": 5,
        "count": 7,
        "has_others": True,
        "members": [
            {"brand_key": key, "brand": key.upper(), "is_others": False}
            for key in "abcdef"
        ]
        + [{"brand_key": None, "brand": "기타", "is_others": True}],
    }
    assert data["brand_ranking"]["top_brands"] == ["A", "B", "C", "D", "E", "F", "기타"]
