from __future__ import annotations

import pytest

from phase_zeta_runner.config import RunnerConfig
from phase_zeta_runner.metric_validator import (
    build_bundle_path_index,
    extract_numbers,
    find_match,
    validate_output,
)


def test_extract_comma_raw_value():
    extracted = extract_numbers("리바로 매출 14,450,706,270.69 KRW")
    assert any(item["value"] == 14450706270.69 for item in extracted)


def test_extract_percent():
    extracted = extract_numbers("YoY +34.87% 성장, M/S 4.13%")
    assert any(item["value"] == pytest.approx(34.87) and item["pattern"] == "percent" for item in extracted)
    assert any(item["value"] == pytest.approx(4.13) and item["pattern"] == "percent" for item in extracted)


def test_bundle_path_matching():
    bundle = {
        "market_views": [
            {
                "target_brand_metric": {
                    "history": {"2026-04": {"raw_value": 14450706270.69}}
                }
            }
        ]
    }
    index = build_bundle_path_index(bundle)
    assert 14450706270.69 in index
    assert "raw_value" in find_match(14450706270.69, index, tolerance=0.01)


def test_bundle_path_index_includes_numbers_from_source_text():
    bundle = {
        "event_bundle": {
            "events_brand_centric": [
                {
                    "title": "임상 3상에서 장 정결률 97% 확인",
                    "summary": "3분기 연속 20% 이상의 성장률을 유지",
                }
            ]
        }
    }

    index = build_bundle_path_index(bundle)

    assert 97.0 in index
    assert 20.0 in index


def test_threshold_percent_without_exact_source_is_warning_only():
    bundle = {
        "market_views": [
            {
                "target_brand_metric": {
                    "history": {
                        "2025-Q2": {"yoy_pct": 24.83},
                        "2025-Q3": {"yoy_pct": 27.74},
                        "2025-Q4": {"yoy_pct": 26.48},
                    }
                }
            }
        ]
    }
    parsed_output = {
        "phenomenon": {
            "title": "뉴트로진 성장세",
            "body": "",
            "bullets": ["3분기 연속 20% 이상의 높은 성장률을 유지했습니다."],
        },
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    result = validate_output(parsed_output, bundle, RunnerConfig.default_for_tests().validator)

    assert result.valid
    assert any(item["raw_text"] == "20%" for item in result.warnings)
    assert not result.unmatched_numbers


def test_validation_full_flow():
    bundle = {
        "market_views": [
            {
                "target_brand_metric": {
                    "history": {
                        "2026-04": {
                            "raw_value": 14450706270.69,
                            "ms_pct": 4.13,
                            "rank": 2,
                            "yoy_pct": 34.87,
                        }
                    }
                }
            }
        ]
    }
    parsed_output = {
        "phenomenon": {
            "title": "리바로 14,450,706,270.69 KRW",
            "body": "M/S 4.13%이고 순위 2위입니다.",
            "bullets": ["YoY +34.87%"],
        },
        "cause": {"title": "원인", "body": "bundle 기반", "bullets": ["2위 유지"]},
        "prediction": {"title": "예측", "body": "4.13% 수준 유지", "bullets": ["+34.87% 성장률 참고"]},
        "recommendation": {"title": "권고", "body": "2위 방어", "bullets": ["14,450,706,270.69 KRW 근거"]},
    }

    result = validate_output(parsed_output, bundle, RunnerConfig.default_for_tests().validator)

    assert result.total_numbers_extracted >= 4
    assert result.total_numbers_matched >= 4
    assert result.valid


def _simulation_bundle():
    return {
        "forecast_simulation": {
            "available": True,
            "by_view": {
                "ML.UBIST.sales": {
                    "horizon_1y": {
                        "period": "2027-03",
                        "base": 1000,
                        "ci_lower_95": 800,
                        "ci_upper_95": 1200,
                    },
                    "horizon_3y": {
                        "period": "2029-03",
                        "base": 3000,
                        "ci_lower_95": 2400,
                        "ci_upper_95": 3600,
                    },
                    "horizon_5y": {
                        "period": "2031-03",
                        "base": 5000,
                        "ci_lower_95": 4000,
                        "ci_upper_95": 6000,
                    },
                }
            },
        }
    }


def _parsed_with_prediction(body: str):
    return {
        "phenomenon": {"title": "", "body": "", "bullets": []},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "예측", "body": body, "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }


def test_simulation_prediction_accepts_raw_krw_with_ci_wording():
    parsed_output = _parsed_with_prediction(
        "1년 후 1,000 KRW (95% 신뢰구간 800~1,200 KRW), "
        "3년 후 3,000 KRW (95% 신뢰구간 2,400~3,600 KRW), "
        "5년 후 5,000 KRW (95% 신뢰구간 4,000~6,000 KRW)로 예측됩니다."
    )

    result = validate_output(parsed_output, _simulation_bundle(), RunnerConfig.default_for_tests().validator)

    assert result.valid
    assert any(
        item.get("matched_path", "").startswith("forecast_simulation.by_view.ML.UBIST.sales.horizon_1y")
        for item in result.stage_results["prediction"].extracted
    )


def test_simulation_prediction_rejects_optimistic_pessimistic_scenario_words():
    parsed_output = _parsed_with_prediction(
        "1년 후 1,000 KRW (95% 신뢰구간 800~1,200 KRW)이며 낙관 시나리오에서는 1,200 KRW입니다. "
        "3년 후 3,000 KRW (95% 신뢰구간 2,400~3,600 KRW), "
        "5년 후 5,000 KRW (95% 신뢰구간 4,000~6,000 KRW)입니다."
    )

    result = validate_output(parsed_output, _simulation_bundle(), RunnerConfig.default_for_tests().validator)

    assert not result.valid
    assert any(item["pattern"] == "simulation_forbidden_scenario_phrase" for item in result.unmatched_numbers)


def test_simulation_prediction_rejects_unit_conversion():
    parsed_output = _parsed_with_prediction(
        "1년 후 10억 (95% 신뢰구간 8억~12억), "
        "3년 후 3,000 KRW (95% 신뢰구간 2,400~3,600 KRW), "
        "5년 후 5,000 KRW (95% 신뢰구간 4,000~6,000 KRW)입니다."
    )

    result = validate_output(parsed_output, _simulation_bundle(), RunnerConfig.default_for_tests().validator)

    assert not result.valid
    assert any(item["pattern"] == "simulation_unit_conversion" for item in result.unmatched_numbers)


def test_simulation_prediction_requires_ci_wording():
    parsed_output = _parsed_with_prediction(
        "1년 후 1,000 KRW (800~1,200 KRW), 3년 후 3,000 KRW (2,400~3,600 KRW), "
        "5년 후 5,000 KRW (4,000~6,000 KRW)로 예측됩니다."
    )

    result = validate_output(parsed_output, _simulation_bundle(), RunnerConfig.default_for_tests().validator)

    assert not result.valid
    assert any(item["pattern"] == "simulation_missing_ci_wording" for item in result.unmatched_numbers)
