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
