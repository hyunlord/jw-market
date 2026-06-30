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
        "5년 후 5,000 KRW (95% 신뢰구간 4,000~6,000 KRW)로 예측됩니다 "
        "(Market Landscape · UBIST 기준). 1년에서 5년으로 갈수록 기준 예측값이 높아져 "
        "중장기 성장 방향성이 유지될 가능성을 시사합니다. 신뢰구간 폭도 함께 확대되므로 "
        "장기 구간에서는 실제 시장 성과의 변동성 리스크를 함께 봐야 합니다."
    )

    result = validate_output(parsed_output, _simulation_bundle(), RunnerConfig.default_for_tests().validator)

    assert result.valid
    assert any(
        item.get("matched_path", "").startswith("forecast_simulation.by_view.ML.UBIST.sales.horizon_1y")
        for item in result.stage_results["prediction"].extracted
    )


def test_simulation_prediction_rejects_number_listing_without_insight():
    parsed_output = _parsed_with_prediction(
        "1년 후 1,000 KRW (95% 신뢰구간 800~1,200 KRW)입니다. "
        "3년 후 3,000 KRW (95% 신뢰구간 2,400~3,600 KRW)입니다. "
        "5년 후 5,000 KRW (95% 신뢰구간 4,000~6,000 KRW)입니다. "
        "예측 모델은 Prophet입니다."
    )

    result = validate_output(parsed_output, _simulation_bundle(), RunnerConfig.default_for_tests().validator)

    assert not result.valid
    assert any(item["pattern"] == "prediction_insight_too_sparse" for item in result.unmatched_numbers)


def test_short_variant_requires_one_year_and_rejects_five_year_forecast_focus():
    parsed_output = _parsed_with_prediction(
        "1년 후 1,000 KRW (95% 신뢰구간 800~1,200 KRW)로 예측됩니다. "
        "5년 후 5,000 KRW (95% 신뢰구간 4,000~6,000 KRW)도 함께 제시됩니다. "
        "1년 구간의 기준 예측값은 가까운 처방 대응의 방향성을 보여줍니다. "
        "CI 폭은 단기 실행에서 변동성 리스크를 같이 봐야 한다는 의미입니다. "
        "현재 지표와 연결하면 단기 시장 대응의 우선순위를 조정할 필요가 있습니다."
    )

    config = RunnerConfig.default_for_tests().with_analysis_variant("short")
    result = validate_output(parsed_output, _simulation_bundle(), config.validator)

    assert not result.valid
    assert any(item["pattern"] == "simulation_short_uses_horizon_5y" for item in result.unmatched_numbers)
    assert not any(item["pattern"] == "simulation_missing_horizon_3y" for item in result.unmatched_numbers)


def test_long_variant_requires_five_year_and_allows_three_year_bridge():
    parsed_output = _parsed_with_prediction(
        "3년 후 3,000 KRW(ML·UBIST·매출·2029-03, 95% 신뢰구간 2,400~3,600 KRW)는 중간 점검점입니다. "
        "5년 후 5,000 KRW(ML·UBIST·매출·2031-03, 95% 신뢰구간 4,000~6,000 KRW)로 예측됩니다. "
        "5년 구간의 기준 예측값은 장기 시장 구조 변화의 방향성을 보여줍니다. "
        "CI 폭 확대는 구조적 불확실성 리스크가 커진다는 의미입니다. "
        "현재 지표와 연결하면 장기 포지셔닝과 경쟁력 관리가 중요해집니다."
    )

    config = RunnerConfig.default_for_tests().with_analysis_variant("long")
    result = validate_output(parsed_output, _simulation_bundle(), config.validator)

    assert result.valid
    assert any(
        item.get("matched_path", "").startswith("forecast_simulation.by_view.ML.UBIST.sales.horizon_5y")
        for item in result.stage_results["prediction"].extracted
    )
    assert not any(item["pattern"] == "simulation_missing_horizon_1y" for item in result.unmatched_numbers)


def test_simulation_prediction_rejects_numeric_heavy_sparse_insight():
    parsed_output = _parsed_with_prediction(
        "1년 후 1,000 KRW (95% 신뢰구간 800~1,200 KRW)입니다. "
        "3년 후 3,000 KRW (95% 신뢰구간 2,400~3,600 KRW)입니다. "
        "5년 후 5,000 KRW (95% 신뢰구간 4,000~6,000 KRW)입니다. "
        "1년 하한은 800 KRW, 상한은 1,200 KRW입니다. "
        "5년 하한은 4,000 KRW, 상한은 6,000 KRW입니다. "
        "장기 예측값 상승은 성장 방향성이 이어질 가능성을 시사합니다. "
        "CI 확대는 장기 불확실성 리스크가 커진다는 의미입니다."
    )

    result = validate_output(parsed_output, _simulation_bundle(), RunnerConfig.default_for_tests().validator)

    assert not result.valid
    assert any(item["pattern"] == "prediction_insight_too_sparse" for item in result.unmatched_numbers)


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


def test_market_metric_accepts_compact_view_label_from_production_shape():
    parsed_output = {
        "phenomenon": {
            "title": "페린젝트 M/S 58.82%(CD·IQVIA·매출·2025-Q4)",
            "body": "전체 시장 M/S는 25.36%(ML·IQVIA·매출·2025-Q4)입니다.",
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


def test_view_label_policy_ignores_bare_year_that_matches_period_metadata():
    parsed_output = {
        "phenomenon": {
            "title": "",
            "body": "",
            "bullets": ["2025년 2분기 이후 매출 및 처방량의 가속화된 성장세 관찰"],
        },
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }
    bundle = _cd_metric_bundle()
    bundle["market_views"][0]["target_brand_metric"]["mat_12m_absolute"] = {
        "latest_period": "2025-Q4",
    }

    result = validate_output(parsed_output, bundle, RunnerConfig.default_for_tests().validator)

    assert result.valid
    assert not any(item["pattern"] == "market_metric_missing_view_label" for item in result.unmatched_numbers)


def test_view_label_policy_ignores_ci_confidence_literal():
    parsed_output = {
        "phenomenon": {"title": "", "body": "", "bullets": []},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {
            "title": "예측",
            "body": (
                "1년 후 1000(ML·UBIST·매출·2027-03), "
                "3년 후 3000(ML·UBIST·매출·2029-03), "
                "5년 후 5000(ML·UBIST·매출·2031-03)이며 95% 신뢰구간을 함께 봅니다."
            ),
            "bullets": [],
            "evidence": [{"title": "예측 시뮬레이션", "basis": "1000(ML·UBIST·매출·2027-03)"}],
        },
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    result = validate_output(parsed_output, _simulation_bundle(), RunnerConfig.default_for_tests().validator)

    assert not any(item["raw_text"] == "95%" for item in result.unmatched_numbers)
    assert not any(item["pattern"] == "market_metric_missing_view_label" for item in result.unmatched_numbers)


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


def test_prediction_news_evidence_accepts_news_title_wrapper():
    parsed_output = {
        "phenomenon": {"title": "", "body": "", "bullets": []},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {
            "title": "급여 확대 뉴스로 성장 전망",
            "body": "급여 확대 보도에 따라 향후 처방 증가가 예상됩니다.",
            "bullets": [],
            "evidence": [{"title": "뉴스 '페린젝트 급여 확대'", "source": "뉴스"}],
        },
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    result = validate_output(parsed_output, _cd_metric_bundle(), RunnerConfig.default_for_tests().validator)

    assert not any(item["pattern"] == "prediction_evidence_not_in_bundle" for item in result.unmatched_numbers)


def test_prediction_simulation_evidence_basis_must_match_bundle_number():
    bundle = _simulation_bundle()
    parsed_output = _parsed_with_prediction(
        "1년 후 1,000 KRW (95% 신뢰구간 800~1,200 KRW), "
        "3년 후 3,000 KRW (95% 신뢰구간 2,400~3,600 KRW), "
        "5년 후 5,000 KRW (95% 신뢰구간 4,000~6,000 KRW)로 예측됩니다 "
        "(ML·UBIST·매출·2031-03). 기준 예측값이 장기 구간으로 갈수록 확대되어 "
        "성장 방향성이 이어질 가능성을 시사합니다. 신뢰구간 폭 확대는 장기 전망의 "
        "불확실성 리스크가 함께 커진다는 의미입니다."
    )
    parsed_output["prediction"]["evidence"] = [
        {"title": "매출 및 처방량 예측 시뮬레이션", "basis": "5,000(ML·UBIST·매출·2031-03)"}
    ]

    result = validate_output(parsed_output, bundle, RunnerConfig.default_for_tests().validator)

    assert result.valid


def test_prediction_simulation_evidence_accepts_compact_tagged_integer_basis():
    bundle = _simulation_bundle()
    parsed_output = _parsed_with_prediction(
        "1년 후 1,000(ML·UBIST·sales·2027-03), "
        "3년 후 3,000(ML·UBIST·sales·2029-03), "
        "5년 후 5,000(ML·UBIST·sales·2031-03)로 예측됩니다. "
        "95% 신뢰구간은 800(ML·UBIST·sales·2027-03)에서 "
        "1,200(ML·UBIST·sales·2027-03)입니다. "
        "기준 예측값이 장기 구간으로 갈수록 확대되어 성장 방향성이 이어질 가능성을 시사합니다. "
        "신뢰구간 폭 확대는 장기 전망의 불확실성 리스크가 함께 커진다는 의미입니다."
    )
    parsed_output["prediction"]["evidence"] = [{"title": "수치 근거", "basis": "1,000(ML·UBIST·sales·2027-03)"}]

    result = validate_output(parsed_output, bundle, RunnerConfig.default_for_tests().validator)

    assert not any(item["pattern"] == "prediction_evidence_not_in_bundle" for item in result.unmatched_numbers)


def test_prediction_simulation_evidence_accepts_integer_rounded_unit_forecast_basis():
    bundle = {
        "forecast_simulation": {
            "available": True,
            "by_view": {
                "ML.IQVIA.unit": {
                    "horizon_1y": {
                        "period": "2026-Q3",
                        "base": 338.49,
                        "ci_lower_95": 275.99,
                        "ci_upper_95": 499.68,
                    }
                }
            },
        }
    }
    parsed_output = _parsed_with_prediction(
        "1년 후 처방량은 339(ML·IQVIA·unit·2026-Q3)으로 예측됩니다. "
        "현재 대비 단기 처방 기반이 확대될 가능성을 시사합니다. "
        "CI 폭은 실제 처방 변동성 리스크를 함께 봐야 한다는 의미입니다."
    )
    parsed_output["prediction"]["evidence"] = [{"title": "수치 근거", "basis": "339(ML·IQVIA·unit·2026-Q3)"}]

    result = validate_output(parsed_output, bundle, RunnerConfig.default_for_tests().validator)

    assert not any(item["pattern"] == "prediction_evidence_not_in_bundle" for item in result.unmatched_numbers)


def test_prediction_simulation_evidence_rejects_basis_number_not_in_bundle():
    bundle = _simulation_bundle()
    parsed_output = _parsed_with_prediction(
        "1년 후 1,000 KRW (95% 신뢰구간 800~1,200 KRW), "
        "3년 후 3,000 KRW (95% 신뢰구간 2,400~3,600 KRW), "
        "5년 후 5,000 KRW (95% 신뢰구간 4,000~6,000 KRW)로 예측됩니다 "
        "(ML·UBIST·매출·2031-03)."
    )
    parsed_output["prediction"]["evidence"] = [
        {"title": "매출 및 처방량 예측 시뮬레이션", "basis": "9,999(ML·UBIST·매출·2031-03)"}
    ]

    result = validate_output(parsed_output, bundle, RunnerConfig.default_for_tests().validator)

    assert not result.valid
    assert any(item["pattern"] == "prediction_evidence_not_in_bundle" for item in result.unmatched_numbers)


def test_evidence_pool_policy_rejects_sparse_stage_evidence():
    parsed_output = {
        "evidence_pool": [],
        "phenomenon": {
            "title": "",
            "body": "",
            "bullets": [],
            "evidence": [{"title": "현상 근거", "basis": "bundle 수치"}],
        },
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    result = validate_output(parsed_output, {}, RunnerConfig.default_for_tests().validator)

    assert not result.valid
    assert any(item["pattern"] == "evidence_pool_too_sparse" for item in result.unmatched_numbers)


def test_evidence_pool_policy_passes_with_stage_evidence_and_bundle_supplement():
    parsed_output = {
        "phenomenon": {
            "title": "",
            "body": "",
            "bullets": [],
            "evidence": [{"title": "현상 근거", "basis": "bundle 수치"}],
        },
        "cause": {
            "title": "",
            "body": "",
            "bullets": [],
            "evidence": [{"title": "원인 근거", "source": "뉴스"}],
        },
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {
            "title": "",
            "body": "",
            "bullets": [],
            "evidence": [{"title": "권고 근거", "basis": "시장 근거"}],
        },
    }
    bundle = {
        "event_bundle": {
            "events_brand_centric": [
                {"title": f"뉴스 {idx}", "source": "뉴스", "summary": f"요약 {idx}"}
                for idx in range(1, 8)
            ],
        }
    }

    result = validate_output(parsed_output, bundle, RunnerConfig.default_for_tests().validator)

    assert result.valid
    assert not any(item["pattern"].startswith("evidence_pool_") for item in result.unmatched_numbers)
