from __future__ import annotations

from phase_zeta_runner.config import RunnerConfig
from phase_zeta_runner.metric_validator import validate_output
from phase_zeta_runner.post_llm_repair import repair_post_llm_output


def _config():
    return RunnerConfig.default_for_tests().validator


def _cd_metric_bundle():
    return {
        "market_views": [
            {
                "view_id": "CD.IQVIA.sales",
                "source": "IQVIA",
                "target_brand_metric": {"history": {"2025-Q4": {"ms_pct": 58.82}}},
            }
        ],
        "event_bundle": {
            "events_brand_centric": [{"news_id": "n1", "title": "페린젝트 급여 확대"}],
            "events_market_trend": [],
            "cross_match_events": [],
        },
        "competitor_events": {"by_source": {}, "by_view": {}},
    }


def _forecast_bundle():
    return {
        "forecast_simulation": {
            "available": True,
            "by_view": {
                "ML.IQVIA.unit": {
                    "source": "IQVIA",
                    "measure": "unit",
                    "horizon_1y": {
                        "period": "2026-Q3",
                        "base": 46012.98,
                        "ci_lower_95": 38134.79,
                        "ci_upper_95": 53282.74,
                    },
                    "horizon_3y": {
                        "period": "2028-Q3",
                        "base": 49346.94,
                        "ci_lower_95": 35387.82,
                        "ci_upper_95": 63012.55,
                    },
                    "horizon_5y": {
                        "period": "2030-Q3",
                        "base": 52549.86,
                        "ci_lower_95": 34579.14,
                        "ci_upper_95": 70724.93,
                    },
                },
                "ML.IQVIA.counting_unit": {
                    "source": "IQVIA",
                    "measure": "counting_unit",
                    "horizon_1y": {
                        "period": "2026-Q3",
                        "base": 338.49,
                        "ci_lower_95": 275.99,
                        "ci_upper_95": 499.68,
                    },
                    "horizon_3y": {
                        "period": "2028-Q3",
                        "base": 492.87,
                        "ci_lower_95": 265.81,
                        "ci_upper_95": 1177.49,
                    },
                    "horizon_5y": {
                        "period": "2030-Q3",
                        "base": 537.57,
                        "ci_lower_95": 190.68,
                        "ci_upper_95": 1696.47,
                    },
                },
            },
        }
    }


def _negative_trend_bundle():
    return {
        "market_views": [
            {
                "view_id": "ML.IQVIA.sales",
                "source": "IQVIA",
                "target_brand_metric": {"history": {"2025-Q4": {"qoq_pct": -19.27}}},
            }
        ]
    }


def test_repair_adds_existing_bundle_view_label_before_validation():
    parsed_output = {
        "phenomenon": {"title": "점유율", "body": "M/S는 58.82%입니다.", "bullets": []},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }
    before = validate_output(parsed_output, _cd_metric_bundle(), _config())

    repaired = repair_post_llm_output(parsed_output, _cd_metric_bundle(), _config())
    after = validate_output(repaired.parsed_output, _cd_metric_bundle(), _config())

    assert not before.valid
    assert after.valid
    assert "Competitive Dynamics · IQVIA 기준" in repaired.parsed_output["phenomenon"]["body"]
    assert repaired.changes == [
        {
            "type": "view_source_label",
            "path": "phenomenon.body",
            "labels": ["Competitive Dynamics · IQVIA 기준"],
        }
    ]


def test_repair_strips_only_zero_decimal_currency_and_quantity_units():
    parsed_output = {
        "phenomenon": {
            "title": "",
            "body": "매출은 1,234.0원이고 처방은 2,000.00개입니다. 비율은 12.30%입니다.",
            "bullets": [],
        },
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    repaired = repair_post_llm_output(parsed_output, {}, _config())

    assert repaired.parsed_output["phenomenon"]["body"] == "매출은 1,234원이고 처방은 2,000개입니다. 비율은 12.30%입니다."
    assert [change["type"] for change in repaired.changes] == ["zero_decimal_unit", "zero_decimal_unit"]


def test_repair_does_not_fabricate_prediction_evidence():
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
    repaired = repair_post_llm_output(parsed_output, _cd_metric_bundle(), _config())
    result = validate_output(repaired.parsed_output, _cd_metric_bundle(), _config())

    assert repaired.parsed_output == parsed_output
    assert repaired.changes == []
    assert not result.valid
    assert any(item["pattern"] == "prediction_evidence_not_in_bundle" for item in result.unmatched_numbers)


def test_repair_adds_prediction_evidence_only_from_existing_forecast_basis():
    parsed_output = {
        "phenomenon": {"title": "", "body": "", "bullets": []},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {
            "title": "향후 처방량",
            "body": (
                "1년 뒤 338.49(ML·IQVIA·counting_unit·2026-Q3), "
                "3년 뒤 492.87(ML·IQVIA·counting_unit·2028-Q3), "
                "5년 뒤 537.57(ML·IQVIA·counting_unit·2030-Q3)입니다. "
                "95% 신뢰구간은 275.99(ML·IQVIA·counting_unit·2026-Q3)에서 "
                "499.68(ML·IQVIA·counting_unit·2026-Q3)입니다. "
                "장기 구간으로 갈수록 처방량 성장 방향성이 이어질 가능성을 시사합니다. "
                "CI 폭 확대는 장기 전망의 불확실성 리스크를 함께 봐야 한다는 의미입니다."
            ),
            "bullets": [],
            "evidence": [],
        },
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    repaired = repair_post_llm_output(parsed_output, _forecast_bundle(), _config())
    result = validate_output(repaired.parsed_output, _forecast_bundle(), _config())

    assert repaired.parsed_output["prediction"]["evidence"] == [
        {
            "title": "forecast_simulation 수치 근거",
            "basis": "338.49(ML·IQVIA·counting_unit·2026-Q3)",
            "stage": "prediction",
        }
    ]
    assert any(change["type"] == "prediction_numeric_evidence" for change in repaired.changes)
    assert result.valid


def test_repair_restores_missing_negative_percent_sign_only_when_bundle_confirms_trend_metric():
    parsed_output = {
        "phenomenon": {
            "title": "",
            "body": "전 분기 대비 19.27%(ML·IQVIA·sales·2025-Q4)의 매출 변동이 관찰됩니다.",
            "bullets": ["전 분기 대비 매출은 19.27%(ML·IQVIA·sales·2025-Q4) 감소"],
        },
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    repaired = repair_post_llm_output(parsed_output, _negative_trend_bundle(), _config())
    result = validate_output(repaired.parsed_output, _negative_trend_bundle(), _config())

    assert "-19.27%(ML·IQVIA·sales·2025-Q4)" in repaired.parsed_output["phenomenon"]["body"]
    assert "-19.27%(ML·IQVIA·sales·2025-Q4)" in repaired.parsed_output["phenomenon"]["bullets"][0]
    assert [change["type"] for change in repaired.changes] == [
        "signed_percent_polarity",
        "signed_percent_polarity",
    ]
    assert result.valid


def test_repair_keeps_positive_percent_when_bundle_has_positive_value():
    bundle = {
        "market_views": [
            {
                "view_id": "ML.IQVIA.sales",
                "source": "IQVIA",
                "target_brand_metric": {"history": {"2025-Q4": {"ms_pct": 19.27}}},
            }
        ]
    }
    parsed_output = {
        "phenomenon": {
            "title": "",
            "body": "시장 점유율은 19.27%(ML·IQVIA·sales·2025-Q4)로 감소 압박을 받습니다.",
            "bullets": [],
        },
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    repaired = repair_post_llm_output(parsed_output, bundle, _config())

    assert repaired.parsed_output == parsed_output
    assert repaired.changes == []


def test_repair_normalizes_korean_large_unit_numbers_before_validation():
    parsed_output = {
        "phenomenon": {
            "title": "시장 성과",
            "body": "매출은 25억 8720만 873원이고 처방은 4만 3707 unit입니다.",
            "bullets": ["시장 규모는 525억 3099만 2,235원입니다."],
        },
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    repaired = repair_post_llm_output(parsed_output, {}, _config())

    assert repaired.parsed_output["phenomenon"]["body"] == "매출은 2,587,200,873원이고 처방은 43,707 unit입니다."
    assert repaired.parsed_output["phenomenon"]["bullets"][0] == "시장 규모는 52,530,992,235원입니다."
    assert [change["type"] for change in repaired.changes] == [
        "korean_large_unit_number",
        "korean_large_unit_number",
        "korean_large_unit_number",
    ]


def test_repair_does_not_treat_drug_class_or_news_title_as_large_unit_money():
    parsed_output = {
        "phenomenon": {
            "title": "",
            "body": "DPP-4 억제제와 SGLT-2 억제제는 유지하고 뉴스 '연처방 117억원 제품'도 제목 그대로 둔다.",
            "bullets": [],
        },
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    repaired = repair_post_llm_output(parsed_output, {}, _config())

    assert repaired.parsed_output == parsed_output
    assert repaired.changes == []


def test_repair_removes_decimal_krw_unit_when_compact_tag_preserves_measure():
    parsed_output = {
        "phenomenon": {"title": "", "body": "", "bullets": []},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {
            "title": "",
            "body": "95% 신뢰구간은 6,516,895,172.28원(ML·UBIST·sales·2027-03)입니다.",
            "bullets": [],
        },
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    repaired = repair_post_llm_output(parsed_output, {}, _config())

    assert repaired.parsed_output["prediction"]["body"] == "95% 신뢰구간은 6,516,895,172.28(ML·UBIST·sales·2027-03)입니다."
    assert repaired.changes == [
        {
            "type": "decimal_metric_unit_removed",
            "path": "prediction.body",
            "before": "6,516,895,172.28원",
            "after": "6,516,895,172.28",
        }
    ]


def test_repair_restores_forecast_tags_and_large_units_without_weakening_validator():
    parsed_output = {
        "phenomenon": {"title": "", "body": "", "bullets": []},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {
            "title": "향후 전망",
            "body": (
                "1년 뒤 4만 6012.98 unit(ML·IQVIA·counting_unit·2025-Q4), "
                "3년 뒤 4만 9346.94 unit(ML·IQVIA·counting_unit·2025-Q4), "
                "5년 뒤 5만 2549.86 unit(ML·IQVIA·counting_unit·2025-Q4)입니다. "
                "95% 신뢰구간은 3만 8134.79 unit에서 5만 3282.74 unit입니다. "
                "기준 예측값이 장기 구간으로 갈수록 높아져 처방량 성장 방향성이 이어질 가능성을 시사합니다. "
                "신뢰구간 폭 확대는 장기 전망에서 실제 수요 변동성 리스크도 함께 봐야 한다는 의미입니다."
            ),
            "bullets": [],
            "evidence": [{"title": "수치 근거", "basis": "4만 6012.98 unit(ML·IQVIA·counting_unit·2025-Q4)"}],
        },
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    repaired = repair_post_llm_output(parsed_output, _forecast_bundle(), _config())
    validation = validate_output(repaired.parsed_output, _forecast_bundle(), _config())

    body = repaired.parsed_output["prediction"]["body"]
    assert "46,012.98 unit(ML·IQVIA·unit·2026-Q3)" in body
    assert "49,346.94 unit(ML·IQVIA·unit·2028-Q3)" in body
    assert "52,549.86 unit(ML·IQVIA·unit·2030-Q3)" in body
    assert "38,134.79 unit(ML·IQVIA·unit·2026-Q3)" in body
    assert validation.valid


def test_repair_converts_dotted_forecast_tags_to_compact_tags():
    parsed_output = {
        "phenomenon": {"title": "", "body": "", "bullets": []},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {
            "title": "향후 처방량",
            "body": (
                "1년 뒤 338.49(ML.IQVIA.counting_unit), "
                "3년 뒤 492.87(ML.IQVIA.counting_unit), "
                "5년 뒤 537.57(ML.IQVIA.counting_unit)입니다. "
                "95% 신뢰구간은 275.99에서 499.68(ML.IQVIA.counting_unit)입니다. "
                "1년에서 5년으로 갈수록 처방량 기준 예측이 확대되어 시장 내 수요 회복 방향성을 시사합니다. "
                "CI 폭은 단기보다 장기 구간에서 불확실성이 커질 수 있어 추세 해석에 주의가 필요합니다."
            ),
            "bullets": [
                "1년 뒤 처방량 338.49(ML.IQVIA.counting_unit)",
                "3년 뒤 처방량 492.87(ML.IQVIA.counting_unit)",
                "5년 뒤 처방량 537.57(ML.IQVIA.counting_unit)",
            ],
        },
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    repaired = repair_post_llm_output(parsed_output, _forecast_bundle(), _config())
    validation = validate_output(repaired.parsed_output, _forecast_bundle(), _config())

    body = repaired.parsed_output["prediction"]["body"]
    assert "338.49(ML·IQVIA·counting_unit·2026-Q3)" in body
    assert "492.87(ML·IQVIA·counting_unit·2028-Q3)" in body
    assert "537.57(ML·IQVIA·counting_unit·2030-Q3)" in body
    assert "275.99(ML·IQVIA·counting_unit·2026-Q3)" in body
    assert validation.valid
