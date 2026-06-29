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
