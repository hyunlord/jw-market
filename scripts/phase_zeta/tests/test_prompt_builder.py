from __future__ import annotations

from phase_zeta_runner.config import RunnerConfig
from phase_zeta_runner.prompt_builder import build_unified_prompt


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
    prompt = build_unified_prompt(sample_bundle(), RunnerConfig.default_for_tests())

    assert "JW중외제약" in prompt.system_instruction
    assert "리바로" in prompt.user_message
    assert "forecast/simulation" in prompt.user_message
    assert set(prompt.response_schema["required"]) == {
        "phenomenon",
        "cause",
        "prediction",
        "recommendation",
    }


def test_prompt_determinism():
    bundle = sample_bundle()
    config = RunnerConfig.default_for_tests()

    p1 = build_unified_prompt(bundle, config)
    p2 = build_unified_prompt(bundle, config)

    assert p1.system_instruction == p2.system_instruction
    assert p1.user_message == p2.user_message
    assert p1.response_schema == p2.response_schema
