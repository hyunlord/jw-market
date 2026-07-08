from __future__ import annotations

import pytest

from phase_zeta_runner.config import RunnerConfig
from phase_zeta_runner.metric_validator import (
    build_bundle_path_index,
    extract_numbers,
    find_match,
    validate_stage_output,
    validate_output,
    validate_view_label_policy,
)


def test_extract_comma_raw_value():
    extracted = extract_numbers("리바로 매출 14,450,706,270.69 KRW")
    assert any(item["value"] == 14450706270.69 for item in extracted)


def test_extract_percent():
    extracted = extract_numbers("YoY +34.87% 성장, M/S 4.13%")
    assert any(item["value"] == pytest.approx(34.87) and item["pattern"] == "percent" for item in extracted)
    assert any(item["value"] == pytest.approx(4.13) and item["pattern"] == "percent" for item in extracted)
    assert not [item for item in extracted if item["pattern"] == "plain_decimal"]


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
        "5년 후 5,000 KRW (95% 신뢰구간 4,000~6,000 KRW)로 예측됩니다 "
        "(Market Landscape · UBIST 기준)."
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


def _cd_metric_bundle():
    return {
        "market_views": [
            {
                "view_id": "CD.IQVIA.sales",
                "source": "IQVIA",
                "target_brand_metric": {"history": {"2025-Q4": {"ms_pct": 58.82}}},
            },
            {
                "view_id": "ML.IQVIA.sales",
                "source": "IQVIA",
                "target_brand_metric": {"history": {"2025-Q4": {"ms_pct": 25.36}}},
            },
        ],
        "event_bundle": {
            "events_brand_centric": [
                {"news_id": "n1", "title": "페린젝트 급여 확대", "published_date": "2026-05-01"}
            ],
            "events_market_trend": [],
            "cross_match_events": [],
        },
        "competitor_events": {"by_source": {}, "by_view": {}},
    }


def test_cd_metric_is_valid_when_competitive_dynamics_label_is_present():
    parsed_output = {
        "phenomenon": {
            "title": "페린젝트 M/S 58.82% (Competitive Dynamics · IQVIA 기준)",
            "body": "전체 시장 M/S는 25.36% (Market Landscape · IQVIA 기준)입니다.",
            "bullets": [],
        },
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    result = validate_output(parsed_output, _cd_metric_bundle(), RunnerConfig.default_for_tests().validator)

    assert result.valid


def test_market_metric_without_view_label_is_rejected():
    parsed_output = {
        "phenomenon": {
            "title": "페린젝트 M/S 58.82%",
            "body": "전체 시장 M/S는 25.36%입니다.",
            "bullets": [],
        },
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    result = validate_output(parsed_output, _cd_metric_bundle(), RunnerConfig.default_for_tests().validator)

    assert not result.valid
    assert any(item["pattern"] == "market_metric_missing_view_label" for item in result.unmatched_numbers)


@pytest.mark.parametrize(
    "source_text",
    [
        "IQVIA 매출 기준",
        "Market Landscape · IQVIA 기준",
        "IQVIA기준",
        "iqvia sales basis",
    ],
)
def test_market_metric_accepts_source_only_label(source_text: str):
    parsed_output = {
        "phenomenon": {
            "title": f"페린젝트 M/S 58.82% ({source_text})",
            "body": "",
            "bullets": [],
        },
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    result = validate_output(parsed_output, _cd_metric_bundle(), RunnerConfig.default_for_tests().validator)

    assert result.valid


def test_view_label_policy_ignores_forecast_ci_metadata_values():
    bundle = {
        "forecast_simulation": {
            "available": True,
            "by_view": {
                "ML.UBIST.sales": {
                    "horizon_ci_levels": {"note": "95% confidence interval"},
                    "horizon_1y": {"base": 1000, "ci_lower_95": 800, "ci_upper_95": 1200},
                }
            },
        }
    }
    bundle_index = build_bundle_path_index(bundle)
    stage_result = validate_stage_output(
        "prediction",
        {
            "title": "95% 신뢰구간",
            "body": "95% CI 기준으로 폭을 제시합니다.",
            "bullets": [],
        },
        bundle_index,
        RunnerConfig.default_for_tests().validator,
    )

    issues = validate_view_label_policy(bundle, {"prediction": stage_result})

    assert not [
        item for item in issues if item["pattern"] == "market_metric_missing_view_label"
    ]


def test_view_label_policy_ignores_low_priority_numbers():
    bundle = _duplicate_source_metric_bundle()
    bundle_index = build_bundle_path_index(bundle)
    stage_result = validate_stage_output(
        "prediction",
        {
            "title": "IQVIA 기준 예측",
            "body": "2026년 1년 후 3년 후 5년 후 흐름을 설명합니다.",
            "bullets": [],
        },
        bundle_index,
        RunnerConfig.default_for_tests().validator,
    )

    issues = validate_view_label_policy(bundle, {"prediction": stage_result})

    assert not [
        item for item in issues if item["pattern"] == "market_metric_missing_view_label"
    ]


def _duplicate_source_metric_bundle():
    return {
        "market_views": [
            {
                "view_id": "ML.IQVIA.sales",
                "source": "IQVIA",
                "target_brand_metric": {"history": {"2026-Q1": {"ms_pct": 0.15}}},
            },
            {
                "view_id": "ML.UBIST.sales",
                "source": "UBIST",
                "target_brand_metric": {"history": {"2026-Q1": {"ms_pct": 0.15}}},
            },
        ]
    }


@pytest.mark.parametrize("source", ["UBIST", "IQVIA"])
def test_view_label_policy_prefers_source_written_in_context(source: str):
    parsed_output = {
        "phenomenon": {
            "title": f"가드렛 점유율은 0.15% (Market Landscape · {source} 기준)입니다.",
            "body": "",
            "bullets": [],
        },
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    result = validate_output(
        parsed_output,
        _duplicate_source_metric_bundle(),
        RunnerConfig.default_for_tests().validator,
    )

    assert result.valid


def test_prediction_news_claim_requires_evidence_when_source_exists():
    parsed_output = {
        "phenomenon": {"title": "", "body": "", "bullets": []},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {
            "title": "급여 확대 뉴스로 성장 전망",
            "body": "급여 확대 보도에 따라 향후 처방 증가가 예상됩니다.",
            "bullets": [],
            "evidence": [],
        },
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    result = validate_output(parsed_output, _cd_metric_bundle(), RunnerConfig.default_for_tests().validator)

    assert not result.valid
    assert any(item["pattern"] == "prediction_evidence_required" for item in result.unmatched_numbers)


def test_prediction_news_evidence_must_come_from_bundle():
    parsed_output = {
        "phenomenon": {"title": "", "body": "", "bullets": []},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {
            "title": "급여 확대 뉴스로 성장 전망",
            "body": "급여 확대 보도에 따라 향후 처방 증가가 예상됩니다.",
            "bullets": [],
            "evidence": [{"news_id": "not-in-bundle", "title": "없는 기사"}],
        },
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    result = validate_output(parsed_output, _cd_metric_bundle(), RunnerConfig.default_for_tests().validator)

    assert not result.valid
    assert any(item["pattern"] == "prediction_evidence_not_in_bundle" for item in result.unmatched_numbers)


def test_prediction_numeric_evidence_matches_market_or_simulation_basis():
    parsed_output = {
        "phenomenon": {"title": "", "body": "", "bullets": []},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {
            "title": "수치 기반 예측",
            "body": "시장 지표를 근거로 완만한 성장이 예상됩니다.",
            "bullets": [],
            "evidence": [{"title": "수치 근거", "basis": "58.82(Competitive Dynamics·IQVIA·점유율)"}],
        },
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    result = validate_output(parsed_output, _cd_metric_bundle(), RunnerConfig.default_for_tests().validator)

    assert result.valid


def test_prediction_numeric_evidence_matches_source_only_basis():
    parsed_output = {
        "phenomenon": {"title": "", "body": "", "bullets": []},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {
            "title": "수치 기반 예측",
            "body": "시장 지표를 근거로 완만한 성장이 예상됩니다.",
            "bullets": [],
            "evidence": [
                {"title": "수치 근거", "basis": "314,003,008.58 KRW(IQVIA 매출 기준 1년 후 예측값)"}
            ],
        },
        "recommendation": {"title": "", "body": "", "bullets": []},
    }
    bundle = {
        "market_views": [
            {
                "view_id": "ML.IQVIA.sales",
                "source": "IQVIA",
                "target_brand_metric": {
                    "history": {"2026-Q1": {"raw_value": 314003008.58}}
                },
            }
        ]
    }

    result = validate_output(parsed_output, bundle, RunnerConfig.default_for_tests().validator)

    assert result.valid


def test_simulation_prediction_accepts_decimal_zero_horizon():
    bundle = {
        "forecast_simulation": {
            "available": True,
            "by_view": {
                "ML.IQVIA.sales": {
                    "horizon_1y": {"base": 314003008.58},
                    "horizon_3y": {"base": 81390234.74},
                    "horizon_5y": {"base": 0.0},
                }
            },
        }
    }
    parsed_output = _parsed_with_prediction(
        "1년 후 314,003,008.58 KRW, 3년 후 81,390,234.74 KRW, "
        "5년 후 0.00 KRW로 예측됩니다(95% CI, IQVIA 매출 기준)."
    )

    result = validate_output(parsed_output, bundle, RunnerConfig.default_for_tests().validator)

    assert result.valid


def test_simulation_prediction_accepts_decimal_zero_horizon_with_other_zero_candidates():
    bundle = {
        "forecast_simulation": {
            "available": True,
            "by_view": {
                "CD.IQVIA.sales": {
                    "horizon_1y": {
                        "base": 869712.27,
                        "ci_lower_95": 0.0,
                        "ci_upper_95": 1739424.54,
                    },
                    "horizon_3y": {
                        "base": 101054.97,
                        "ci_lower_95": 0.0,
                        "ci_upper_95": 202109.94,
                    },
                    "horizon_5y": {
                        "base": 0.0,
                        "ci_lower_95": 0.0,
                        "ci_upper_95": 0.0,
                    },
                },
                "ML.IQVIA.sales": {
                    "horizon_1y": {"base": 0.0},
                    "horizon_3y": {"base": 0.0},
                    "horizon_5y": {"base": 0.0},
                },
            },
        }
    }
    parsed_output = _parsed_with_prediction(
        "1년 후 869,712.27(Competitive Dynamics · IQVIA 기준), "
        "3년 후 101,054.97(Competitive Dynamics · IQVIA 기준), "
        "5년 후 0.00(Competitive Dynamics · IQVIA 기준)으로 예측됩니다. "
        "95% 신뢰구간 기준입니다."
    )

    result = validate_output(parsed_output, bundle, RunnerConfig.default_for_tests().validator)

    assert result.valid
    assert not [
        item
        for item in result.unmatched_numbers
        if item["pattern"] == "simulation_missing_horizon_5y"
    ]


def test_approximate_rank_bucket_expression_is_warning_only():
    bundle = {
        "market_views": [
            {
                "target_brand_metric": {
                    "history": {"2026-Q2": {"rank_in_market": 110}}
                }
            }
        ]
    }
    parsed_output = {
        "phenomenon": {
            "title": "처방 순위 약세",
            "body": "처방 순위가 100위권 밖으로 밀려났습니다.",
            "bullets": [],
        },
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    result = validate_output(parsed_output, bundle, RunnerConfig.default_for_tests().validator)

    assert result.valid
    assert any(
        item["raw_text"] == "100위" and item["number_type"] == "rank"
        for item in result.warnings
    )


def test_prediction_event_evidence_can_match_title_in_basis():
    parsed_output = {
        "phenomenon": {"title": "", "body": "", "bullets": []},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {
            "title": "급여 확대 뉴스로 성장 전망",
            "body": "급여 확대 보도에 따라 향후 처방 증가가 예상됩니다.",
            "bullets": [],
            "evidence": [{"title": "정책적 리스크 해소", "basis": "뉴스 '페린젝트 급여 확대'"}],
        },
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    result = validate_output(parsed_output, _cd_metric_bundle(), RunnerConfig.default_for_tests().validator)

    assert result.valid
