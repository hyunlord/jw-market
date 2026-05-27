from __future__ import annotations

import pytest

from phase_zeta_runner.config import RunnerConfig
from phase_zeta_runner.metric_validator import (
    build_bundle_path_index,
    classify_number_context,
    extract_numbers,
    find_match_unit_aware,
    validate_output,
)


def test_volume_rx_with_integer_rounding_matches_bundle_raw():
    text = "처방량 26,184,954 Rx 기록"
    bundle = {"market_views": [{"target_brand_metric": {"history": {"2026-04": {"raw_value": 26184953.78}}}}]}
    bundle_index = build_bundle_path_index(bundle)

    match = next(item for item in extract_numbers(text) if item["raw_text"] == "26,184,954")
    number_type = classify_number_context(match["raw_text"], text, match["value"])

    assert number_type == "volume_rx"
    assert find_match_unit_aware(match["value"], bundle_index, number_type, RunnerConfig.default_for_tests().validator)


def test_signed_percent_rounding_matches_bundle_raw():
    text = "전년 대비 +34.87% 성장"
    bundle = {"yoy_growth": 34.872227235539356}
    bundle_index = build_bundle_path_index(bundle)

    match = next(item for item in extract_numbers(text) if item["pattern"] == "percent")
    number_type = classify_number_context(match["raw_text"], text, match["value"])

    assert number_type == "percent_signed"
    assert find_match_unit_aware(match["value"], bundle_index, number_type, RunnerConfig.default_for_tests().validator)


def test_currency_krw_precision_still_matches_exact_raw():
    text = "매출 14,450,706,270.69 KRW"
    bundle = {"raw_value": 14450706270.69}
    bundle_index = build_bundle_path_index(bundle)

    match = next(item for item in extract_numbers(text) if item["raw_text"] == "14,450,706,270.69")
    number_type = classify_number_context(match["raw_text"], text, match["value"])

    assert number_type == "currency_krw"
    assert find_match_unit_aware(match["value"], bundle_index, number_type, RunnerConfig.default_for_tests().validator)


def test_relative_tolerance_for_large_numbers():
    text = "매출 1,234,567,890,000 KRW"
    bundle = {"raw_value": 1234567889123.45}
    bundle_index = build_bundle_path_index(bundle)

    match = next(item for item in extract_numbers(text) if item["raw_text"] == "1,234,567,890,000")

    assert find_match_unit_aware(match["value"], bundle_index, "currency_krw", RunnerConfig.default_for_tests().validator)


def test_classify_rank_context():
    text = "시장 2위 달성"
    match = next(item for item in extract_numbers(text) if item["raw_text"] == "2위")

    assert classify_number_context(match["raw_text"], text, match["value"]) == "rank"


def test_classify_kpi_context():
    text = "EI 142.50 기록"
    match = next(item for item in extract_numbers(text) if item["value"] == pytest.approx(142.50))

    assert classify_number_context(match["raw_text"], text, match["value"]) == "kpi"


def test_classify_mixed_rx_and_krw_units_by_nearest_suffix():
    text = "처방량 26,184,954 Rx 및 매출 14,450,706,270.69 KRW"
    extracted = extract_numbers(text)
    rx = next(item for item in extracted if item["raw_text"] == "26,184,954")
    krw = next(item for item in extracted if item["raw_text"] == "14,450,706,270.69")

    assert classify_number_context(rx["raw_text"], text, rx["value"]) == "volume_rx"
    assert classify_number_context(krw["raw_text"], text, krw["value"]) == "currency_krw"


def test_validate_output_accepts_run6_integer_rounded_rx_values():
    bundle = {
        "market_views": [
            {
                "target_brand_metric": {
                    "history": {
                        "2026-04": {
                            "raw_value": 26184953.78,
                            "rank": 2,
                        }
                    }
                }
            }
        ]
    }
    parsed = {
        "phenomenon": {
            "title": "리바로, 2026년 4월 처방량 26,184,954 Rx로 시장 2위 달성",
            "body": "처방량 26,184,954 Rx 기준으로 2위입니다.",
            "bullets": ["처방량 26,184,954 Rx"],
        },
        "cause": {"title": "원인", "body": "bundle 기반", "bullets": []},
        "prediction": {"title": "예측", "body": "bundle 기반", "bullets": []},
        "recommendation": {"title": "권고", "body": "bundle 기반", "bullets": []},
    }

    result = validate_output(parsed, bundle, RunnerConfig.default_for_tests().validator)

    assert result.valid
    assert result.unmatched_numbers == []
    assert any(
        item["raw_text"] == "26,184,954" and item["number_type"] == "volume_rx" and item["matched_path"]
        for item in result.stage_results["phenomenon"].extracted
    )
