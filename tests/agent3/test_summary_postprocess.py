import pytest

from pipeline.scripts.agent3.summary_postprocess import (
    CandidateMatchError,
    inject_candidate_numbers,
    validate_display_number_narratives,
)


def _candidate() -> dict:
    return {
        "slice": "IQVIA 급여: NON-NHI",
        "metric": "recent_growth",
        "value_current": 191610275723.0,
        "value_baseline": 29358367221.0,
        "delta_abs": 162251908502.0,
        "delta_pct": 552.6598508718885,
        "display_numbers": {
            "value_current": "1916.1억원",
            "value_baseline": "293.6억원",
            "delta_abs": "1622.5억원",
            "delta_pct": "552.7%",
        },
    }


def test_inject_candidate_numbers_copies_raw_values_from_matched_candidate() -> None:
    summary = {
        "strength_items": [
            {
                "candidate_index": 0,
                "slice": "IQVIA 급여: NON-NHI",
                "metric": "recent_growth",
                "numbers": {"delta_pct": 552.7},
                "narrative": "NON-NHI 매출이 1916.1억원으로 552.7% 성장했습니다.",
            }
        ]
    }

    enriched = inject_candidate_numbers(summary, [_candidate()])

    assert enriched["strength_items"][0]["numbers"] == {
        "value_current": 191610275723.0,
        "value_baseline": 29358367221.0,
        "delta_abs": 162251908502.0,
        "delta_pct": 552.6598508718885,
    }


def test_inject_candidate_numbers_rejects_unmatched_item() -> None:
    summary = {
        "strength_items": [
            {
                "candidate_index": 9,
                "slice": "없는 후보",
                "metric": "recent_growth",
                "narrative": "없는 후보입니다.",
            }
        ]
    }

    with pytest.raises(CandidateMatchError, match="candidate"):
        inject_candidate_numbers(summary, [_candidate()])


def test_validate_display_number_narratives_rejects_raw_decimal() -> None:
    summary = {
        "strength_items": [
            {
                "candidate_index": 0,
                "slice": "IQVIA 급여: NON-NHI",
                "metric": "recent_growth",
                "narrative": "전 분기 대비 552.6598508718885% 성장했습니다.",
            }
        ]
    }

    errors = validate_display_number_narratives(summary, [_candidate()])

    assert errors == [
        "item 0 narrative contains raw decimal: 전 분기 대비 552.6598508718885% 성장했습니다."
    ]


def test_validate_display_number_narratives_accepts_display_strings() -> None:
    summary = {
        "strength_items": [
            {
                "candidate_index": 0,
                "slice": "IQVIA 급여: NON-NHI",
                "metric": "recent_growth",
                "narrative": "NON-NHI 매출이 1916.1억원으로 552.7% 성장했습니다.",
            }
        ]
    }

    assert validate_display_number_narratives(summary, [_candidate()]) == []


def test_validate_display_number_narratives_accepts_slice_label_number() -> None:
    candidate = {
        "slice": "IQVIA 성분용량: 0.05%",
        "metric": "recent_growth",
        "display_numbers": {
            "value_current": "1.7억원",
            "value_baseline": "1.2억원",
            "delta_abs": "55,873,286",
            "delta_pct": "46.9%",
        },
    }
    summary = {
        "strength_items": [
            {
                "candidate_index": 0,
                "slice": "IQVIA 성분용량: 0.05%",
                "metric": "recent_growth",
                "narrative": "0.05% 성분용량 매출이 1.7억원으로 46.9% 증가했습니다.",
            }
        ]
    }

    assert validate_display_number_narratives(summary, [candidate]) == []


def test_validate_display_number_narratives_accepts_won_alias_for_small_currency() -> None:
    candidate = {
        "slice": "전체 UBIST",
        "metric": "recent_growth",
        "display_numbers": {
            "value_current": "1.7억원",
            "value_baseline": "1.0억원",
            "delta_abs": "7,021만원",
            "delta_pct": "70.2%",
        },
        "display_number_aliases": {
            "delta_abs": ["7,021만원", "70,211,632원"],
        },
    }
    summary = {
        "strength_items": [
            {
                "candidate_index": 0,
                "slice": "전체 UBIST",
                "metric": "recent_growth",
                "narrative": "전체 UBIST 매출이 70,211,632원 증가하며 70.2% 성장했습니다.",
            }
        ]
    }

    assert validate_display_number_narratives(summary, [candidate]) == []


def test_validate_display_number_narratives_rejects_unknown_currency_value() -> None:
    candidate = {
        "slice": "전체 UBIST",
        "metric": "recent_growth",
        "display_numbers": {
            "value_current": "1.7억원",
            "value_baseline": "1.0억원",
            "delta_abs": "70,211,632원",
            "delta_pct": "70.2%",
        },
    }
    summary = {
        "strength_items": [
            {
                "candidate_index": 0,
                "slice": "전체 UBIST",
                "metric": "recent_growth",
                "narrative": "전체 UBIST 매출이 70,999,999원 증가했습니다.",
            }
        ]
    }

    assert validate_display_number_narratives(summary, [candidate]) == [
        "item 0 narrative number is not in display_numbers: 70,999,999원"
    ]


def test_validate_display_number_narratives_accepts_malformed_comma_grouping_when_value_matches() -> None:
    candidate = {
        "slice": "IQVIA 급여: NHI",
        "metric": "recent_growth",
        "display_numbers": {
            "value_current": "5.6억원",
            "value_baseline": "5.0억원",
            "delta_abs": "56,878,382원",
            "delta_pct": "11.4%",
        },
        "display_number_aliases": {
            "delta_abs": ["56,878,382원", "5,688만원"],
        },
    }
    summary = {
        "strength_items": [
            {
                "candidate_index": 0,
                "slice": "IQVIA 급여: NHI",
                "metric": "recent_growth",
                "narrative": "매출 증가가 5,6878,382원으로 표기됐습니다.",
            }
        ]
    }

    assert validate_display_number_narratives(summary, [candidate]) == []


def test_validate_display_number_narratives_accepts_signed_percent_alias() -> None:
    candidate = {
        "slice": "IQVIA 성분용량: 2250MG",
        "metric": "recent_growth",
        "display_numbers": {
            "value_current": "5.4억원",
            "value_baseline": "4.7억원",
            "delta_abs": "7,000만원",
            "delta_pct": "14.4%",
            "yoy_delta_pct": "-17.5%",
        },
        "display_number_aliases": {
            "yoy_delta_pct": ["-17.5%"],
        },
    }
    summary = {
        "strength_items": [
            {
                "candidate_index": 0,
                "slice": "IQVIA 성분용량: 2250MG",
                "metric": "recent_growth",
                "narrative": "전년 동기 대비로는 -17.5% 변동을 보였습니다.",
            }
        ]
    }

    assert validate_display_number_narratives(summary, [candidate]) == []


def test_validate_display_number_narratives_still_rejects_raw_metric_number() -> None:
    candidate = {
        "slice": "IQVIA 성분용량: 0.05%",
        "metric": "recent_growth",
        "display_numbers": {
            "value_current": "1.7억원",
            "value_baseline": "1.2억원",
            "delta_abs": "55,873,286",
            "delta_pct": "46.9%",
        },
    }
    summary = {
        "strength_items": [
            {
                "candidate_index": 0,
                "slice": "IQVIA 성분용량: 0.05%",
                "metric": "recent_growth",
                "narrative": "0.05% 성분용량 매출이 1.7억원으로 46.944% 증가했습니다.",
            }
        ]
    }

    assert validate_display_number_narratives(summary, [candidate]) == [
        "item 0 narrative number is not in display_numbers: 46.944%"
    ]


def test_inject_candidate_numbers_includes_new_taxonomy_fields() -> None:
    candidate = {
        "slice": "전체 UBIST",
        "metric": "stable_core",
        "cv_pct": 8.2,
        "window_change_pct": -3.0,
        "rank": 2,
        "share_pct": 12.5,
        "market_brand_count": 8,
        "observation_count": 12,
        "latest_value": 900_000_000.0,
    }
    summary = {
        "strength_items": [
            {"candidate_index": 0, "slice": "전체 UBIST", "metric": "stable_core", "narrative": "안정적입니다."}
        ]
    }

    enriched = inject_candidate_numbers(summary, [candidate])

    assert enriched["strength_items"][0]["numbers"] == {
        "cv_pct": 8.2,
        "window_change_pct": -3.0,
        "rank": 2,
        "share_pct": 12.5,
        "market_brand_count": 8,
        "observation_count": 12,
        "latest_value": 900_000_000.0,
    }


def test_duration_and_rank_units_are_not_truncated_to_count_tokens() -> None:
    candidate = {
        "slice": "전체 UBIST",
        "metric": "stable_core",
        "display_numbers": {
            "observation_count": "12개월",
            "rank": "2위",
            "window_change_pct": "-3.0%",
            "latest_value": "9억원",
        },
    }
    summary = {
        "strength_items": [
            {
                "candidate_index": 0,
                "slice": "전체 UBIST",
                "metric": "stable_core",
                "narrative": "12개월 동안 2위를 유지했고 기간 증감은 -3.0%, 최신 매출은 9억원입니다.",
            }
        ]
    }

    assert validate_display_number_narratives(summary, [candidate]) == []


def test_validate_display_number_narratives_does_not_whitelist_evidence_numbers() -> None:
    candidate = {
        "slice": "IQVIA 성분용량: 0.05%",
        "metric": "recent_growth",
        "evidence": "원 수치 delta_pct=46.944%",
        "display_numbers": {
            "value_current": "1.7억원",
            "value_baseline": "1.2억원",
            "delta_abs": "55,873,286",
            "delta_pct": "46.9%",
        },
    }
    summary = {
        "strength_items": [
            {
                "candidate_index": 0,
                "slice": "IQVIA 성분용량: 0.05%",
                "metric": "recent_growth",
                "narrative": "0.05% 성분용량 매출이 46.944% 증가했습니다.",
            }
        ]
    }

    assert validate_display_number_narratives(summary, [candidate]) == [
        "item 0 narrative number is not in display_numbers: 46.944%"
    ]
