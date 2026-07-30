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


def test_display_percent_matches_ratio_bundle_value_without_relaxing_tolerance():
    text = "최근 성장률은 48.26%입니다."
    bundle = {
        "market_views": [
            {"target_brand_metric": {"mat_yoy_pct": 0.48263983937799626}}
        ]
    }
    bundle_index = build_bundle_path_index(bundle)

    match = next(item for item in extract_numbers(text) if item["pattern"] == "percent")

    assert find_match_unit_aware(
        match["value"],
        bundle_index,
        match["number_type"],
        RunnerConfig.default_for_tests().validator,
    ) == "market_views[0].target_brand_metric.mat_yoy_pct"


def test_display_percent_does_not_scale_non_percent_numbers():
    bundle_index = build_bundle_path_index({"market_views": [{"raw_value": 0.4826}]})

    assert find_match_unit_aware(
        48.26,
        bundle_index,
        "currency_krw",
        RunnerConfig.default_for_tests().validator,
    ) is None


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


def test_extract_numbers_treats_korean_composite_krw_as_single_amounts():
    extracted = extract_numbers(
        "게보린릴랙스 시장은 1,075억 8,303만 3,730원이며 "
        "최근 실적은 1,879만 4,572원이고 비교 시장은 1억 2,345만원입니다."
    )

    assert [
        (item["raw_text"], item["value"], item["number_type"])
        for item in extracted
    ] == [
        ("1,075억 8,303만 3,730원", 107_583_033_730.0, "currency_krw"),
        ("1,879만 4,572원", 18_794_572.0, "currency_krw"),
        ("1억 2,345만원", 123_450_000.0, "currency_krw"),
    ]


def test_korean_composite_krw_matches_raw_bundle_values():
    bundle_index = build_bundle_path_index(
        {
            "market_views": {
                "market_total_krw": 107_583_033_730,
                "brand_sales_krw": 18_794_572,
            }
        }
    )

    for extracted in extract_numbers(
        "시장 1,075억 8,303만 3,730원, 브랜드 1,879만 4,572원"
    ):
        assert find_match_unit_aware(
            extracted["value"],
            bundle_index,
            extracted["number_type"],
            RunnerConfig.default_for_tests().validator,
        )


def test_validate_output_accepts_gevorin_relax_composite_krw_fixture():
    bundle = {
        "market_views": {
            "market_total_krw": 107_583_033_730,
            "brand_sales_krw": 18_794_572,
        }
    }
    parsed = {
        "phenomenon": {
            "title": "게보린릴랙스 시장 현황",
            "body": (
                "시장 규모는 1,075억 8,303만 3,730원이며 "
                "브랜드 실적은 1,879만 4,572원입니다."
            ),
            "bullets": [],
        },
        "cause": {"title": "원인", "body": "bundle 기반", "bullets": []},
        "prediction": {"title": "예측", "body": "bundle 기반", "bullets": []},
        "recommendation": {"title": "권고", "body": "bundle 기반", "bullets": []},
    }

    result = validate_output(
        parsed,
        bundle,
        RunnerConfig.default_for_tests().validator,
    )

    assert result.valid
    assert result.total_numbers_extracted == 2
    assert result.total_numbers_matched == 2
    assert result.unmatched_numbers == []


def test_simple_percent_and_rank_extraction_is_unchanged():
    extracted = extract_numbers("점유율 1.44%, 시장 6위")

    assert any(
        item["value"] == 1.44 and item["number_type"] == "percent"
        for item in extracted
    )
    assert any(
        item["value"] == 6.0 and item["number_type"] == "rank"
        for item in extracted
    )


def test_materialized_derived_shares_match_without_whitelisting_calculations():
    bundle_index = build_bundle_path_index(
        {
            "derived_metrics": {
                "target_share_change_from_history_start": {
                    "delta_pct_points": -0.67,
                    "absolute_delta_pct_points": 0.67,
                },
                "top2_competitor_share_latest": {
                    "share_pct": 48.2584,
                },
            }
        }
    )
    config = RunnerConfig.default_for_tests().validator

    delta = next(item for item in extract_numbers("점유율 0.67%p 감소") if item["pattern"] == "percent")
    top_two = next(item for item in extract_numbers("상위 2개 점유율 48.26%") if item["pattern"] == "percent")
    invented = next(item for item in extract_numbers("상위 2개 점유율 50.00%") if item["pattern"] == "percent")

    assert find_match_unit_aware(delta["value"], bundle_index, delta["number_type"], config)
    assert find_match_unit_aware(top_two["value"], bundle_index, top_two["number_type"], config)
    assert find_match_unit_aware(invented["value"], bundle_index, invented["number_type"], config) is None
