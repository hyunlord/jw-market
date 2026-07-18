from __future__ import annotations

from dataclasses import asdict, replace
import json

import pytest

from jw_chat_agent_poc.tools.query_layer.derived_validation import derived_parity_report
from jw_chat_agent_poc.tools.query_layer.store import MartRecord, MartSnapshot


def test_derived_snapshot_matches_live_calculation_for_every_fixture_cell() -> None:
    snapshot = MartSnapshot(_records(), 0.0)

    report = derived_parity_report(snapshot)

    assert report.classification == "census"
    assert report.checked == report.population
    assert report.population > 0
    assert report.failures == ()
    assert report.exit_code == 0


def test_derived_parity_census_handles_quarterly_periods() -> None:
    periods = ("2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4")
    values = {
        "브랜드A": (100.0, 110.0, 120.0, 130.0),
        "브랜드B": (200.0, 210.0, 220.0, 230.0),
    }
    totals = {
        period: sum(series[index] for series in values.values())
        for index, period in enumerate(periods)
    }
    snapshot = MartSnapshot(
        tuple(_record(brand, series, periods, totals) for brand, series in values.items()),
        0.0,
    )

    report = derived_parity_report(snapshot)

    assert report.checked == report.population
    assert report.population > 0
    assert report.failures == ()
    assert report.exit_code == 0


def test_derived_snapshot_keeps_missing_market_values_out_of_growth_math() -> None:
    snapshot = MartSnapshot(_records(missing_market_period=True), 0.0)

    point = snapshot.derived.market_point("ml_006", "ubist", "sales", "2026-02")
    insight = snapshot.derived.brand_insight("ml_006", "ubist", "sales", "리바로")

    assert point.total_krw is None
    assert point.hhi is None
    assert point.cr5_pct is None
    assert insight.market_growth_pct is None
    assert insight.excess_growth_pctp is None
    assert insight.missing_periods == ("2026-02",)
    assert insight.market_growth_pct != -100.0


def test_derived_snapshot_keeps_raw_cr5_precision_until_rendering() -> None:
    top_shares = (9.1264939920, 6.1277726065, 5.1167179108, 4.9487627406, 4.1960520158)
    remainder = (100.0 - sum(top_shares)) / 20
    shares = (*top_shares, *(remainder for _ in range(20)))
    records = tuple(_single_period_record(f"브랜드{index:02d}", share) for index, share in enumerate(shares))
    snapshot = MartSnapshot(records, 0.0)

    point = snapshot.derived.market_point("ml_006", "ubist", "sales", "2026-05")

    assert point.cr5_pct == sum(top_shares)
    assert round(point.cr5_pct, 2) == 29.52


def test_derived_snapshot_precomputes_named_growth_rates_from_raw_precision() -> None:
    snapshot = MartSnapshot(_records(), 0.0)

    insight = snapshot.derived.brand_insight("ml_006", "ubist", "sales", "리바로")

    assert insight.brand_mom_pct == pytest.approx((84.0 / 82.0 - 1) * 100)
    assert insight.market_mom_pct == pytest.approx((454.0 / 437.0 - 1) * 100)
    assert insight.brand_cmgr_pct == pytest.approx((84.0 / 80.0) ** (1 / 2) * 100 - 100)
    assert insight.market_cmgr_pct == pytest.approx((454.0 / 420.0) ** (1 / 2) * 100 - 100)
    assert insight.brand_cqgr_pct == pytest.approx((84.0 / 80.0) ** (3 / 2) * 100 - 100)
    assert insight.market_cqgr_pct == pytest.approx((454.0 / 420.0) ** (3 / 2) * 100 - 100)
    assert insight.brand_yoy_pct is None
    assert insight.market_yoy_pct is None


def test_empty_snapshot_is_a_failed_parity_population() -> None:
    report = derived_parity_report(MartSnapshot((), 0.0))

    assert report.population == 0
    assert report.exit_code == 1


def test_parity_report_catches_corrupted_precomputed_insight() -> None:
    snapshot = MartSnapshot(_records(), 0.0)
    key = next(iter(snapshot.derived.insights))
    snapshot.derived.insights[key] = replace(
        snapshot.derived.insights[key],
        brand_growth_pct=999.0,
    )

    report = derived_parity_report(snapshot)

    assert report.exit_code == 1
    assert any(item.startswith(f"insight:{key}") for item in report.failures)


def test_parity_report_catches_missing_precomputed_market_cell() -> None:
    snapshot = MartSnapshot(_records(), 0.0)
    key = next(iter(snapshot.derived.market_points))
    snapshot.derived.market_points.pop(key)

    report = derived_parity_report(snapshot)

    assert report.exit_code == 1
    assert f"market:missing:{key}" in report.failures


def test_parity_report_requires_canonical_byte_identity() -> None:
    snapshot = MartSnapshot(_records(), 0.0)
    key = next(iter(snapshot.derived.market_points))
    point = snapshot.derived.market_points[key]
    snapshot.derived.market_points[key] = replace(point, denominator=float(point.denominator))

    report = derived_parity_report(snapshot)

    assert report.exit_code == 1
    assert any(item.startswith(f"market:{key}:canonical") for item in report.failures)


def test_mom_is_missing_when_previous_calendar_month_is_absent() -> None:
    records = tuple(
        _record(
            brand,
            values,
            ("2026-01", "2026-03"),
            {"2026-01": 420.0, "2026-03": 454.0},
        )
        for brand, values in {
            "로수젯": (200.0, 220.0),
            "리피토": (140.0, 150.0),
            "리바로": (80.0, 84.0),
        }.items()
    )
    insight = MartSnapshot(records, 0.0).derived.brand_insight(
        "ml_006", "ubist", "sales", "리바로"
    )

    assert insight.brand_mom_pct is None
    assert insight.market_mom_pct is None


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_raw_values_are_treated_as_missing(invalid: float) -> None:
    bad = _single_period_record("오염값", invalid)
    good = _single_period_record("정상값", 100.0)
    snapshot = MartSnapshot((bad, good), 0.0)

    assert snapshot.value_or_none(bad, "2026-05") is None
    assert snapshot.value_status(bad, "2026-05") == "missing"
    assert snapshot.derived.market_point("ml_006", "ubist", "sales", "2026-05").total_krw is None
    payload = asdict(snapshot.derived.brand_insight("ml_006", "ubist", "sales", "정상값"))
    json.dumps(payload, allow_nan=False)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_stored_share_falls_back_to_finite_computed_share(invalid: float) -> None:
    contaminated = _single_period_record("오염점유율", 25.0)
    contaminated.metric_history["2026-05"]["ms"] = invalid
    snapshot = MartSnapshot((contaminated, _single_period_record("정상값", 75.0)), 0.0)

    point = snapshot.derived.brand_point("ml_006", "ubist", "sales", "오염점유율", "2026-05")

    assert point.share_pct == pytest.approx(25.0)
    json.dumps(asdict(point), allow_nan=False)


def _records(*, missing_market_period: bool = False) -> tuple[MartRecord, ...]:
    periods = ("2026-01", "2026-02", "2026-03")
    values = {
        "로수젯": (200.0, 210.0, 220.0),
        "리피토": (140.0, None if missing_market_period else 145.0, 150.0),
        "리바로": (80.0, 82.0, 84.0),
    }
    totals = {
        period: None
        if any(values[brand][index] is None for brand in values)
        else sum(float(values[brand][index]) for brand in values)
        for index, period in enumerate(periods)
    }
    return tuple(_record(brand, series, periods, totals) for brand, series in values.items())


def _record(
    brand: str,
    values: tuple[float | None, ...],
    periods: tuple[str, ...],
    totals: dict[str, float | None],
) -> MartRecord:
    history: dict[str, dict[str, float | str | None]] = {}
    for index, period in enumerate(periods):
        value = values[index]
        total = totals[period]
        history[period] = {
            "raw_value": value,
            "ms": value / total * 100 if value is not None and total else None,
            "source_status": "OK" if value is not None else "missing",
        }
    return MartRecord(
        ml_id="ml_006",
        brand_name=brand,
        source="ubist",
        measure="sales",
        metric_history=history,
        channel_data={},
        specialty_data={},
        dimension_data={},
        by_dimension={"company": "테스트제약", "molecule": f"{brand}성분"},
    )


def _single_period_record(brand: str, share: float) -> MartRecord:
    return MartRecord(
        ml_id="ml_006",
        brand_name=brand,
        source="ubist",
        measure="sales",
        metric_history={"2026-05": {"raw_value": share, "ms": share, "source_status": "OK"}},
        channel_data={},
        specialty_data={},
        dimension_data={},
        by_dimension={"company": "테스트제약", "molecule": f"{brand}성분"},
    )
