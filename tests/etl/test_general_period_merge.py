from __future__ import annotations

from copy import deepcopy

from pipeline.etl.io.mart.general_period_merge import merge_scoped_row


def _months(count: int = 65) -> tuple[str, ...]:
    return tuple(
        f"{2021 + offset // 12:04d}-{offset % 12 + 1:02d}"
        for offset in range(count)
    )


def _brand_row(periods: tuple[str, ...], *, multiplier: float) -> dict:
    metric = {
        period: {"raw_value": multiplier * index, "ms": multiplier + index}
        for index, period in enumerate(periods, start=1)
    }
    raw = {
        period: multiplier * index
        for index, period in enumerate(periods, start=1)
    }
    return {
        "brand_key": "brand-a",
        "brand_name": "Brand A",
        "atc4_code": "M1A1",
        "atc4_desc": "Example",
        "source": "ubist",
        "measure": "sales",
        "unit_label": "KRW",
        "metric_history": deepcopy(metric),
        "extended_metric_history": deepcopy(metric),
        "channel_data": {"CLINIC": deepcopy(metric)},
        "specialty_data": {"CARDIO": deepcopy(metric)},
        "channel_specialty_matrix": {
            "CLINIC": {"CARDIO": deepcopy(raw)}
        },
        "audit_code_matrix": {},
        "dimension_data": {"molecule": {"A": deepcopy(metric)}},
        "dimension_channel_data": {
            "molecule": {"A": {"CLINIC": deepcopy(metric)}}
        },
        "by_dimension": {
            "company": "Company A",
            "products": [
                {
                    "product_name": "Product A",
                    "product_code": "P1",
                    "raw_value_total": sum(raw.values()),
                    "raw_value_history": deepcopy(raw),
                }
            ],
        },
        "raw_value_history": deepcopy(raw),
        "payload": {"computed_at": "before", "period_count": len(periods)},
    }


def _market_row(periods: tuple[str, ...], *, multiplier: float) -> dict:
    series = {
        period: multiplier * index
        for index, period in enumerate(periods, start=1)
    }
    ranking = {
        period: [{"brand_key": "brand-a", "raw_value": value}]
        for period, value in series.items()
    }
    return {
        "atc4_code": "M1A1",
        "atc4_desc": "Example",
        "source": "ubist",
        "measure": "sales",
        "unit_label": "KRW",
        "market_size_series": deepcopy(series),
        "hhi_series": deepcopy(series),
        "brand_ranking": deepcopy(ranking),
        "company_ranking_stacked": deepcopy(ranking),
        "company_concentration_trend": deepcopy(series),
        "ei_ms_matrix": [{"period": periods[-1], "brand_key": "brand-a"}],
        "growth_contribution_ms_matrix": [
            {"period": periods[-1], "brand_key": "brand-a"}
        ],
        "growth_contribution": deepcopy(ranking),
        "analysis_levels": None,
        "level_top5_trend": None,
        "target_customer_competition": {"period": periods[-1], "value": multiplier},
        "payload": {"computed_at": "before", "brand_rows": 1},
    }


def test_scoped_brand_merge_replaces_only_target_and_enforces_60_61_contract() -> None:
    periods = _months()
    target = periods[-1]
    existing = _brand_row(periods, multiplier=1.0)
    candidate = _brand_row(periods[-61:], multiplier=1000.0)

    merged = merge_scoped_row(existing, candidate, period_scope=(target,))

    display = periods[-60:]
    calculation = periods[-61:]
    assert tuple(merged["metric_history"]) == display
    assert tuple(merged["raw_value_history"]) == calculation
    for period in display[:-1]:
        assert merged["metric_history"][period] == existing["metric_history"][period]
        assert merged["channel_data"]["CLINIC"][period] == existing["channel_data"]["CLINIC"][period]
        assert (
            merged["channel_specialty_matrix"]["CLINIC"]["CARDIO"][period]
            == existing["channel_specialty_matrix"]["CLINIC"]["CARDIO"][period]
        )
    assert merged["metric_history"][target] == candidate["metric_history"][target]
    assert merged["raw_value_history"][target] == candidate["raw_value_history"][target]
    assert merged["by_dimension"]["products"][0]["raw_value_history"][display[0]] == (
        existing["by_dimension"]["products"][0]["raw_value_history"][display[0]]
    )
    assert merged["by_dimension"]["products"][0]["raw_value_history"][target] == (
        candidate["by_dimension"]["products"][0]["raw_value_history"][target]
    )
    assert merged["payload"]["computed_at"] == "before"
    assert merged["payload"]["period_count"] == 60
    assert merged["payload"]["calculation_period_count"] == 61


def test_sparse_brand_uses_source_period_universe_for_contract_boundary() -> None:
    periods = _months()
    sparse_periods = (*periods[:5], *periods[-10:])
    target = periods[-1]
    existing = _brand_row(sparse_periods, multiplier=1.0)
    candidate = _brand_row(sparse_periods[-10:], multiplier=2.0)

    merged = merge_scoped_row(
        existing,
        candidate,
        period_scope=(target,),
        source_periods=periods,
    )

    assert periods[0] not in merged["metric_history"]
    assert periods[3] not in merged["raw_value_history"]
    assert set(merged["metric_history"]) <= set(periods[-60:])
    assert set(merged["raw_value_history"]) <= set(periods[-61:])
    assert merged["metric_history"][target] == candidate["metric_history"][target]


def test_scoped_market_merge_preserves_every_outside_period_byte_for_byte() -> None:
    periods = _months()
    target = periods[-1]
    existing = _market_row(periods, multiplier=1.0)
    candidate = _market_row(periods[-60:], multiplier=1000.0)

    merged = merge_scoped_row(existing, candidate, period_scope=(target,))

    display = periods[-60:]
    assert tuple(merged["market_size_series"]) == display
    for period in display[:-1]:
        assert merged["market_size_series"][period] == existing["market_size_series"][period]
        assert merged["brand_ranking"][period] == existing["brand_ranking"][period]
    assert merged["market_size_series"][target] == candidate["market_size_series"][target]
    assert merged["brand_ranking"][target] == candidate["brand_ranking"][target]
    assert merged["ei_ms_matrix"] == candidate["ei_ms_matrix"]


def test_latest_market_snapshot_tracks_only_a_latest_period_replacement() -> None:
    periods = _months(12)
    existing = _market_row(periods, multiplier=1.0)
    candidate = _market_row(periods, multiplier=2.0)
    existing["target_customer_competition"] = {
        "history": {period: {"value": 1.0} for period in periods},
        "latest": {"period": periods[-1], "value": 1.0},
    }
    candidate["target_customer_competition"] = {
        "history": {period: {"value": 2.0} for period in periods},
        "latest": {"period": periods[-1], "value": 2.0},
    }

    older = merge_scoped_row(existing, candidate, period_scope=(periods[-2],))
    latest = merge_scoped_row(existing, candidate, period_scope=(periods[-1],))

    assert older["target_customer_competition"]["latest"] == {
        "period": periods[-1],
        "value": 1.0,
    }
    assert latest["target_customer_competition"]["latest"] == {
        "period": periods[-1],
        "value": 2.0,
    }


def test_scoped_merge_removes_target_from_dimension_missing_in_candidate() -> None:
    periods = _months(12)
    existing = _brand_row(periods, multiplier=1.0)
    candidate = _brand_row(periods, multiplier=2.0)
    candidate["specialty_data"] = {}

    merged = merge_scoped_row(existing, candidate, period_scope=(periods[-1],))

    assert periods[-1] not in merged["specialty_data"]["CARDIO"]
    assert merged["specialty_data"]["CARDIO"][periods[-2]] == (
        existing["specialty_data"]["CARDIO"][periods[-2]]
    )


def test_new_row_adds_only_the_requested_period_without_backfilling_history() -> None:
    periods = _months()
    candidate = _brand_row(periods, multiplier=1.0)

    merged = merge_scoped_row(None, candidate, period_scope=(periods[-1],))

    assert tuple(merged["metric_history"]) == (periods[-1],)
    assert tuple(merged["raw_value_history"]) == (periods[-1],)
    assert tuple(merged["channel_data"]["CLINIC"]) == (periods[-1],)


def test_iqvia_contract_remains_twenty_quarters() -> None:
    periods = tuple(
        f"{2020 + index // 4:04d}-Q{index % 4 + 1}"
        for index in range(25)
    )
    existing = _market_row(periods, multiplier=1.0)
    existing["source"] = "iqvia_nsa"
    candidate = _market_row(periods[-20:], multiplier=2.0)
    candidate["source"] = "iqvia_nsa"

    merged = merge_scoped_row(existing, candidate, period_scope=(periods[-1],))

    assert tuple(merged["market_size_series"]) == periods[-20:]
