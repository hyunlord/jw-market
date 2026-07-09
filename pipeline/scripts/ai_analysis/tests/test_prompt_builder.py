from __future__ import annotations

from phase_zeta_runner.config import RunnerConfig
from phase_zeta_runner.prompt_builder import build_question_string


def sample_bundle() -> dict:
    return {
        "bundle_meta": {
            "brand": "리바로",
            "snapshot_at": "2026-05-25T08:00:00+09:00",
            "bundle_hash": "sha256:test",
            "config_version": "phase_zeta_v1_1",
            "builder_version": "1.1.0",
            "stats": {"estimated_tokens": 1000},
        },
        "brand_context": {
            "name": "리바로",
            "mkt_team": "MKT 1팀",
            "molecule": "PITAVASTATIN",
            "available_sources": ["UBIST"],
        },
        "market_views": [
            {
                "view_id": "ML.UBIST.sales",
                "target_brand_metric": {
                    "history": {
                        "2026-04": {
                            "raw_value": 14450706270.69,
                            "ms_pct": 4.13,
                            "rank": 2,
                            "yoy_pct": 34.87,
                        }
                    }
                },
            }
        ],
        "event_bundle": {
            "events_brand_centric": [{"title": "리바로 성장", "score": 70}],
            "events_market_trend": [{"title": "스타틴 시장 변화", "score": 60}],
            "cross_match_events": [],
            "tag_distribution": {"정책/규제": 1},
        },
        "competitor_events": {"by_source": {}},
        "forecast_simulation": {"available": False, "by_view": {}},
    }


def test_prompt_structure():
    question = build_question_string(sample_bundle(), RunnerConfig.default_for_tests())

    assert "[분석 대상]" in question
    assert "리바로" in question
    assert "forecast/simulation" in question
    assert "phenomenon, cause, prediction, recommendation" in question


def test_prompt_determinism():
    bundle = sample_bundle()
    config = RunnerConfig.default_for_tests()

    p1 = build_question_string(bundle, config)
    p2 = build_question_string(bundle, config)

    assert p1 == p2


def test_prompt_compact_adds_mode_instruction_without_changing_full_default():
    bundle = sample_bundle()
    config = RunnerConfig.default_for_tests()

    full = build_question_string(bundle, config)
    compact = build_question_string(bundle, config, mode="compact")

    assert "[출력 밀도]" not in full
    assert "[출력 밀도]" in compact
    assert "bullets는 2-4개" in compact


def test_prompt_combines_density_mode_and_short_variant_instruction():
    bundle = sample_bundle()
    bundle["forecast_simulation"] = {
        "available": True,
        "by_view": {
            "ML.UBIST.sales": {
                "horizon_1y": {"base": 1000},
                "horizon_3y": {"base": 3000},
                "horizon_5y": {"base": 5000},
            }
        },
    }
    config = RunnerConfig.default_for_tests().with_analysis_variant("short")

    question = build_question_string(bundle, config, mode="compact")

    assert "[analysis_variant: short" in question
    assert "[출력 밀도]" in question
    assert "horizon_1y" in question
    assert "3년/5년 예측값" in question
    assert "1y/3y/5y 각 horizon의 실제 수치" not in question


def test_prompt_combines_density_mode_and_long_variant_instruction():
    bundle = sample_bundle()
    bundle["forecast_simulation"] = {
        "available": True,
        "by_view": {
            "ML.UBIST.sales": {
                "horizon_1y": {"base": 1000},
                "horizon_3y": {"base": 3000},
                "horizon_5y": {"base": 5000},
            }
        },
    }
    config = RunnerConfig.default_for_tests().with_analysis_variant("long")

    question = build_question_string(bundle, config, mode="recap")

    assert "[analysis_variant: long" in question
    assert "[출력 밀도]" in question
    assert "horizon_5y" in question
    assert "body는 1-2문장" in question
    assert "1y/3y/5y 각 horizon의 실제 수치" not in question
    assert "horizon_5y가 제공되지 않은 브랜드에서는 5년 수치를 만들지 말고" in question


def test_prompt_declares_view_label_and_evidence_contracts():
    question = build_question_string(sample_bundle(), RunnerConfig.default_for_tests())

    assert "Market Landscape · {SOURCE} 기준" in question
    assert "ML·UBIST·매출" in question
    assert "prediction evidence" in question
    assert "retained event 목록" in question
    assert "evidence 배열을 비워두거나 항목 수를 줄이세요" in question
    assert "source label만 있는 근거" in question


def test_prompt_declares_simulation_horizon_contract_when_all_horizons_exist():
    bundle = sample_bundle()
    bundle["forecast_simulation"] = {
        "available": True,
        "by_view": {
            "ML.UBIST.sales": {
                "horizon_1y": {"base": 1000},
                "horizon_3y": {"base": 3000},
                "horizon_5y": {"base": 5000},
            }
        },
    }

    question = build_question_string(bundle, RunnerConfig.default_for_tests())

    assert "1y/3y/5y" in question
    assert "각 horizon의 실제 수치" in question


def test_prompt_omits_simulation_horizon_contract_when_horizons_are_missing():
    bundle = sample_bundle()
    bundle["forecast_simulation"] = {
        "available": True,
        "by_view": {
            "ML.UBIST.sales": {
                "horizon_1y": {"base": 1000},
            }
        },
    }

    question = build_question_string(bundle, RunnerConfig.default_for_tests())

    assert "각 horizon의 실제 수치" not in question
