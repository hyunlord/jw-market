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
    assert '"bundle_meta"' in question
    assert '"market_views"' in question
    assert "[FULL_BUNDLE_JSON_FOR_FORMATTER]" in question
    assert "[/FULL_BUNDLE_JSON_FOR_FORMATTER]" in question
    assert "forecast/simulation" in question
    assert "phenomenon, cause, prediction, recommendation" in question
    assert "각 stage의 body는 6문장 이상 9문장 이하" in question
    assert "9문장 초과는 금지" in question
    assert "1년/3년/5년" in question
    assert "horizon_1y/horizon_3y/horizon_5y" in question
    assert "95% 신뢰구간" in question
    assert "단독 bullet" in question
    assert "bundle에 없는 퍼센트" in question
    assert "흐름/변동/증감/성장" in question
    assert "(ML·정책·약가인상)" in question
    assert "임의 compact tag" in question


def test_prompt_determinism():
    bundle = sample_bundle()
    config = RunnerConfig.default_for_tests()

    p1 = build_question_string(bundle, config)
    p2 = build_question_string(bundle, config)

    assert p1 == p2


def test_short_prompt_focuses_on_one_year_near_term_actions():
    question = build_question_string(
        sample_bundle(),
        RunnerConfig.default_for_tests().with_analysis_variant("short"),
    )

    assert "analysis_variant: short" in question
    assert "단기 인사이트" in question
    assert "horizon_1y" in question
    assert "1년 내 변화" in question
    assert "가까운 처방" in question
    assert "즉시 대응" in question
    assert "3년/5년 예측값" in question
    assert "5년 구조 변화" in question
    assert "주된 근거로 삼지 마세요" in question
    assert "recommendation도 bullet 요약으로 압축하지 말고" in question
    assert "100위권 밖" in question


def test_long_prompt_focuses_on_five_year_structural_strategy():
    question = build_question_string(
        sample_bundle(),
        RunnerConfig.default_for_tests().with_analysis_variant("long"),
    )

    assert "analysis_variant: long" in question
    assert "장기 인사이트" in question
    assert "horizon_5y" in question
    assert "5년 구조적 추세" in question
    assert "전략 포지셔닝" in question
    assert "horizon_3y" in question
    assert "중간 점검점" in question
    assert "단기 실행 체크리스트로 축소하지 마세요" in question
