from __future__ import annotations

import pytest

from pipeline.scripts.agent3.strength_candidate_extractor import (
    CandidateFloors,
    MarketMetricRow,
    MetricRow,
    _display_pct,
    extract_strength_candidates,
)


def _monthly_history(values: list[float], *, start_month: int = 1) -> dict[str, float]:
    return {f"2026-{month:02d}": value for month, value in enumerate(values, start=start_month)}


def _quarterly_history(values: list[float]) -> dict[str, float]:
    return {f"2026-Q{quarter}": value for quarter, value in enumerate(values, start=1)}


def test_extracts_ranked_channel_and_specialty_candidates() -> None:
    row = MetricRow(
        brand_name="리바로젯",
        brand_key="리바로젯",
        source="ubist",
        measure="sales",
        raw_value_history={"2025-04": 80.0, "2026-03": 100.0, "2026-04": 130.0},
        channel_data={
            "의원": {
                "2025-04": {"raw_value": 40.0},
                "2026-03": {"raw_value": 60.0},
                "2026-04": {"raw_value": 90.0},
            }
        },
        specialty_data={
            "순환기": {
                "2025-04": {"raw_value": 30.0},
                "2026-03": {"raw_value": 35.0},
                "2026-04": {"raw_value": 60.0},
            }
        },
        channel_specialty_matrix={
            "의원": {
                "순환기": {"2025-04": 20.0, "2026-03": 30.0, "2026-04": 55.0},
            }
        },
    )

    candidates = extract_strength_candidates(
        [row],
        floors=CandidateFloors(
            min_delta_abs=10.0,
            min_delta_pct=10.0,
            min_recent_value=20.0,
            min_contribution_pct=10.0,
        ),
        top_n=5,
    )

    assert candidates
    assert candidates[0]["period"] == "2026-04"
    assert candidates[0]["candidate_score"] >= candidates[-1]["candidate_score"]
    assert any(item["slice"] == "UBIST 종별: 의원" for item in candidates)
    assert any(item["slice"] == "UBIST 진료과: 순환기" for item in candidates)
    assert any("종별×진료과" in item["slice"] for item in candidates)
    for item in candidates:
        assert item["comparison_period"] == "2026-03"
        assert item["yoy_period"] == "2025-04"
        assert item["delta_abs"] is not None
        assert item["delta_pct"] is not None
        assert item["yoy_delta_pct"] is not None


def test_candidate_floors_filter_small_noise() -> None:
    row = MetricRow(
        brand_name="소음",
        brand_key="noise",
        source="ubist",
        measure="sales",
        raw_value_history={"2026-03": 1_000.0, "2026-04": 1_001.0},
        channel_data={
            "의원": {
                "2026-03": {"raw_value": 1_000.0},
                "2026-04": {"raw_value": 1_001.0},
            }
        },
    )

    candidates = extract_strength_candidates(
        [row],
        floors=CandidateFloors(
            min_delta_abs=100.0,
            min_delta_pct=5.0,
            min_recent_value=500.0,
            min_contribution_pct=1.0,
        ),
    )

    assert candidates == []


def test_iqvia_dimension_candidates_include_audit_and_reimbursement() -> None:
    row = MetricRow(
        brand_name="헴리브라",
        brand_key="헴리브라",
        source="iqvia_nsa",
        measure="sales",
        raw_value_history={"2025-Q4": 100.0, "2026-Q3": 150.0, "2026-Q4": 210.0},
        dimension_data={
            "audit_code": {
                "KHPA": {
                    "2025-Q4": {"raw_value": 80.0},
                    "2026-Q3": {"raw_value": 120.0},
                    "2026-Q4": {"raw_value": 190.0},
                }
            },
            "nhi_type": {
                "NHI": {
                    "2025-Q4": {"raw_value": 100.0},
                    "2026-Q3": {"raw_value": 150.0},
                    "2026-Q4": {"raw_value": 210.0},
                }
            },
        },
    )

    candidates = extract_strength_candidates(
        [row],
        floors=CandidateFloors(
            min_delta_abs=20.0,
            min_delta_pct=10.0,
            min_recent_value=100.0,
            min_contribution_pct=10.0,
        ),
        top_n=5,
    )

    assert any(item["slice"] == "IQVIA audit_code: KHPA" for item in candidates)
    assert any(item["slice"] == "IQVIA 급여: NHI" for item in candidates)


def test_candidates_include_display_numbers_for_narrative_copy() -> None:
    row = MetricRow(
        brand_name="리바로젯",
        brand_key="리바로젯",
        source="ubist",
        measure="sales",
        raw_value_history={"2026-03": 100_000_000.0, "2026-04": 250_000_000.0},
    )

    candidates = extract_strength_candidates(
        [row],
        floors=CandidateFloors(
            min_delta_abs=10.0,
            min_delta_pct=10.0,
            min_recent_value=20.0,
            min_contribution_pct=10.0,
        ),
    )

    assert candidates[0]["display_numbers"]["value_current"] == "2.5억원"
    assert candidates[0]["display_numbers"]["delta_abs"] == "1.5억원"
    assert candidates[0]["display_numbers"]["delta_pct"] == "150.0%"


def test_small_currency_display_includes_won_unit_aliases() -> None:
    row = MetricRow(
        brand_name="소액",
        brand_key="small-money",
        source="ubist",
        measure="sales",
        raw_value_history={"2026-03": 100_000_000.0, "2026-04": 170_211_631.8},
    )

    candidates = extract_strength_candidates(
        [row],
        floors=CandidateFloors(
            min_delta_abs=10.0,
            min_delta_pct=10.0,
            min_recent_value=20.0,
            min_contribution_pct=10.0,
        ),
    )

    assert candidates[0]["display_numbers"]["delta_abs"] == "70,211,632원"
    assert {"70,211,632원", "7,021만원"}.issubset(candidates[0]["display_number_aliases"]["delta_abs"])


def test_tiny_currency_display_includes_won_unit_alias() -> None:
    row = MetricRow(
        brand_name="극소액",
        brand_key="tiny-money",
        source="ubist",
        measure="sales",
        raw_value_history={"2026-03": 100_000_000.0, "2026-04": 100_000_915.0},
    )

    candidates = extract_strength_candidates(
        [row],
        floors=CandidateFloors(
            min_delta_abs=1.0,
            min_delta_pct=0.0,
            min_recent_value=20.0,
            min_contribution_pct=0.0,
        ),
    )

    assert "915원" in candidates[0]["display_number_aliases"]["delta_abs"]


def test_currency_display_includes_truncated_manwon_alias() -> None:
    row = MetricRow(
        brand_name="절삭만원",
        brand_key="truncated-manwon",
        source="ubist",
        measure="sales",
        raw_value_history={"2026-03": 100_000_000.0, "2026-04": 163_077_632.0},
    )

    candidates = extract_strength_candidates(
        [row],
        floors=CandidateFloors(
            min_delta_abs=10.0,
            min_delta_pct=10.0,
            min_recent_value=20.0,
            min_contribution_pct=10.0,
        ),
    )

    assert {"6,307만원", "6,308만원", "63,077,632원"}.issubset(
        candidates[0]["display_number_aliases"]["delta_abs"]
    )


def test_tiny_percent_display_keeps_two_significant_digits() -> None:
    row = MetricRow(
        brand_name="극소퍼센트",
        brand_key="tiny-pct",
        source="ubist",
        measure="sales",
        raw_value_history={"2026-03": 100_000_000.0, "2026-04": 100_050_000.0},
    )

    candidates = extract_strength_candidates(
        [row],
        floors=CandidateFloors(
            min_delta_abs=10.0,
            min_delta_pct=0.01,
            min_recent_value=20.0,
            min_contribution_pct=10.0,
        ),
    )

    assert candidates[0]["delta_pct"] == 0.05
    assert candidates[0]["display_numbers"]["delta_pct"] == "0.05%"


def test_very_small_percent_display_avoids_scientific_notation() -> None:
    row = MetricRow(
        brand_name="제로퍼센트",
        brand_key="zero-pct",
        source="ubist",
        measure="sales",
        raw_value_history={"2026-03": 100_000_000.0, "2026-04": 100_000_001.0},
    )

    candidates = extract_strength_candidates(
        [row],
        floors=CandidateFloors(
            min_delta_abs=0.0,
            min_delta_pct=0.0,
            min_recent_value=20.0,
            min_contribution_pct=10.0,
        ),
    )

    assert candidates[0]["display_numbers"]["delta_pct"] == "0.000001%"


def test_zero_percent_display_stays_zero() -> None:
    assert _display_pct(0.0) == "0%"


def test_low_base_candidates_are_flagged_and_penalized() -> None:
    row = MetricRow(
        brand_name="저기저",
        brand_key="low-base",
        source="ubist",
        measure="sales",
        raw_value_history={"2026-03": 1_000_000_000.0, "2026-04": 2_000_000_000.0},
        channel_data={
            "작은채널": {
                "2026-03": {"raw_value": 1_000_000.0},
                "2026-04": {"raw_value": 100_000_000.0},
            }
        },
    )

    candidates = extract_strength_candidates(
        [row],
        floors=CandidateFloors(
            min_delta_abs=10.0,
            min_delta_pct=10.0,
            min_recent_value=20.0,
            min_contribution_pct=1.0,
        ),
        top_n=5,
    )

    low_base = next(item for item in candidates if item["slice"] == "UBIST 종별: 작은채널")
    assert low_base["low_base"] is True
    assert "기저가 낮아 변동성이 큼" in low_base["caveats"]
    assert low_base["candidate_score"] < low_base["candidate_score_before_low_base_penalty"]


def test_deduplicates_identical_slice_values_to_more_specific_slice() -> None:
    row = MetricRow(
        brand_name="중복",
        brand_key="dedup",
        source="iqvia_nsa",
        measure="sales",
        raw_value_history={"2026-Q1": 100_000_000.0, "2026-Q2": 200_000_000.0},
        dimension_data={
            "nhi_type": {
                "NHI": {
                    "2026-Q1": {"raw_value": 100_000_000.0},
                    "2026-Q2": {"raw_value": 200_000_000.0},
                }
            }
        },
    )

    candidates = extract_strength_candidates(
        [row],
        floors=CandidateFloors(
            min_delta_abs=10.0,
            min_delta_pct=10.0,
            min_recent_value=20.0,
            min_contribution_pct=10.0,
        ),
        top_n=5,
    )

    assert [item["slice"] for item in candidates] == ["IQVIA 급여: NHI"]


def test_scale_leadership_uses_common_latest_market_period_and_niche_gate() -> None:
    row = MetricRow(
        brand_name="리더",
        brand_key="leader",
        source="ubist",
        measure="sales",
        atc4_code="A02B2",
        raw_value_history={"2026-04": 600_000_000.0},
    )
    market_rows = [
        MarketMetricRow("leader", "리더", "ubist", "A02B2", {"2026-04": 600_000_000.0}),
        MarketMetricRow("peer-a", "경쟁A", "ubist", "A2B2", {"2026-04": 5_400_000_000.0}),
        MarketMetricRow("peer-b", "경쟁B", "ubist", "A02B2", {"2026-04": 4_000_000_000.0}),
    ]

    candidate = next(
        item
        for item in extract_strength_candidates([row], market_rows=market_rows)
        if item["metric"] == "scale_leadership"
    )

    assert candidate["period"] == "2026-04"
    assert candidate["rank"] == 3
    assert candidate["share_pct"] == 6.0
    assert candidate["market_brand_count"] == 3
    assert candidate["latest_value"] == 600_000_000.0


def test_scale_leadership_floor_boundaries() -> None:
    target = MetricRow(
        brand_name="경계",
        brand_key="target",
        source="iqvia_nsa",
        measure="sales",
        atc4_code="C10A1",
        raw_value_history={"2026-Q4": 500_000_000.0},
    )
    passing_market = [
        MarketMetricRow("target", "경계", "iqvia_nsa", "C10A1", {"2026-Q4": 500_000_000.0}),
        MarketMetricRow("a", "A", "iqvia_nsa", "C10A1", {"2026-Q4": 8_500_000_000.0}),
        MarketMetricRow("b", "B", "iqvia_nsa", "C10A1", {"2026-Q4": 1_000_000_000.0}),
    ]
    passing = extract_strength_candidates([target], market_rows=passing_market)
    assert any(item["metric"] == "scale_leadership" and item["rank"] == 3 for item in passing)

    two_brand_market = passing_market[:2]
    assert not any(
        item["metric"] == "scale_leadership"
        for item in extract_strength_candidates([target], market_rows=two_brand_market)
    )

    rank_six_market = [
        MarketMetricRow("target", "경계", "iqvia_nsa", "C10A1", {"2026-Q4": 500_000_000.0}),
        *[
            MarketMetricRow(str(index), str(index), "iqvia_nsa", "C10A1", {"2026-Q4": 500_000_000.0 + index})
            for index in range(1, 6)
        ],
    ]
    assert not any(
        item["metric"] == "scale_leadership"
        for item in extract_strength_candidates([target], market_rows=rank_six_market)
    )

    rank_five_market = [
        MarketMetricRow("target", "경계", "iqvia_nsa", "C10A1", {"2026-Q4": 500_000_000.0}),
        *[
            MarketMetricRow(str(index), str(index), "iqvia_nsa", "C10A1", {"2026-Q4": 500_000_000.0 + index})
            for index in range(1, 5)
        ],
    ]
    rank_five = extract_strength_candidates([target], market_rows=rank_five_market)
    assert any(item["metric"] == "scale_leadership" and item["rank"] == 5 for item in rank_five)


def test_stable_core_accepts_ubist_12_month_and_iqvia_4_quarter_boundaries() -> None:
    ubist = MetricRow(
        brand_name="안정월",
        brand_key="stable-month",
        source="ubist",
        measure="sales",
        raw_value_history=_monthly_history([550_000_000.0] * 11 + [500_000_000.0]),
    )
    iqvia = MetricRow(
        brand_name="안정분기",
        brand_key="stable-quarter",
        source="iqvia_nsa",
        measure="sales",
        raw_value_history=_quarterly_history([550_000_000.0, 540_000_000.0, 520_000_000.0, 500_000_000.0]),
    )

    ubist_candidate = next(item for item in extract_strength_candidates([ubist]) if item["metric"] == "stable_core")
    iqvia_candidate = next(item for item in extract_strength_candidates([iqvia]) if item["metric"] == "stable_core")

    assert ubist_candidate["observation_count"] == 12
    assert ubist_candidate["window_change_pct"] == pytest.approx(-9.0909090909)
    assert iqvia_candidate["observation_count"] == 4
    assert iqvia_candidate["latest_value"] == 500_000_000.0


def test_stable_core_rejects_nonconsecutive_or_out_of_floor_history() -> None:
    missing_month = _monthly_history([600_000_000.0] * 12)
    missing_month.pop("2026-06")
    cases = [
        missing_month,
        _monthly_history([1_000_000_000.0, 500_000_000.0] * 6),
        _monthly_history([600_000_000.0] * 11 + [499_999_999.0]),
        _monthly_history([600_000_000.0] * 11 + [539_999_999.0]),
    ]

    for history in cases:
        row = MetricRow("실패", "failed", "ubist", "sales", raw_value_history=history)
        assert not any(item["metric"] == "stable_core" for item in extract_strength_candidates([row]))


def test_stable_core_accepts_cv_and_window_change_exact_boundaries() -> None:
    cv_boundary = MetricRow(
        "CV경계",
        "cv-boundary",
        "iqvia_nsa",
        "sales",
        raw_value_history=_quarterly_history(
            [400_000_000.0, 600_000_000.0, 400_000_000.0, 600_000_000.0]
        ),
    )
    change_boundary = MetricRow(
        "증감경계",
        "change-boundary",
        "ubist",
        "sales",
        raw_value_history=_monthly_history([1_000_000_000.0] * 11 + [900_000_000.0]),
    )

    cv_candidate = next(
        item for item in extract_strength_candidates([cv_boundary]) if item["metric"] == "stable_core"
    )
    change_candidate = next(
        item for item in extract_strength_candidates([change_boundary]) if item["metric"] == "stable_core"
    )

    assert cv_candidate["cv_pct"] == pytest.approx(20.0)
    assert change_candidate["window_change_pct"] == pytest.approx(-10.0)


def test_metric_is_part_of_dedup_and_family_quotas_are_independent() -> None:
    row = MetricRow(
        brand_name="복합",
        brand_key="mixed",
        source="ubist",
        measure="sales",
        atc4_code="A02B2",
        raw_value_history=_monthly_history([500_000_000.0] * 11 + [600_000_000.0]),
        channel_data={
            name: _monthly_history([500_000_000.0] * 11 + [latest])
            for name, latest in zip(("A", "B", "C", "D"), (610_000_000.0, 620_000_000.0, 630_000_000.0, 640_000_000.0))
        },
    )
    market_rows = [
        MarketMetricRow("mixed", "복합", "ubist", "A02B2", {"2026-12": 600_000_000.0}),
        MarketMetricRow("a", "A", "ubist", "A2B2", {"2026-12": 5_400_000_000.0}),
        MarketMetricRow("b", "B", "ubist", "A02B2", {"2026-12": 4_000_000_000.0}),
    ]

    candidates = extract_strength_candidates(
        [row],
        market_rows=market_rows,
        floors=CandidateFloors(min_delta_abs=1.0, min_delta_pct=1.0, min_recent_value=1.0, min_contribution_pct=1.0),
    )

    metrics = [item["metric"] for item in candidates]
    assert metrics.count("recent_growth") == 3
    assert metrics.count("scale_leadership") == 1
    assert metrics.count("stable_core") == 1
