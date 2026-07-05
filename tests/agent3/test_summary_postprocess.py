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
