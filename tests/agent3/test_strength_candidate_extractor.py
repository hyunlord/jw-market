from __future__ import annotations

from pipeline.scripts.agent3.strength_candidate_extractor import (
    CandidateFloors,
    MetricRow,
    _display_pct,
    extract_strength_candidates,
)


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
