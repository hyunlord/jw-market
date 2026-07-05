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
