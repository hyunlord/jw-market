from __future__ import annotations

from jw_chat_agent_poc.service.app import _public_numeric_copy_summary
from jw_chat_agent_poc.service.numeric_copy_contract import enforce_numeric_copy_contract


def _result() -> dict:
    return {
        "tool_calls": [
            {
                "tool": "get_market_landscape",
                "source": "UBIST",
                "status": "success",
                "render_data": {"brand": "리바로", "growth_pct": 12.3, "period": "2026-06"},
            }
        ]
    }


def test_surface_report_records_requested_rendered_and_dropped_metrics() -> None:
    answer, report = enforce_numeric_copy_contract(
        "리바로 성장률과 단가를 표로 보여줘",
        "| 브랜드 | 성장률(YoY) |\n| --- | ---: |\n| 리바로 | 12.3% |",
        _result(),
    )

    assert "12.3%" in answer
    assert report["requested_metrics"] == ["growth", "unit_price"]
    assert report["rendered_metrics"] == ["growth"]
    assert report["dropped_metrics"] == ["unit_price"]
    assert "calculation_unavailable" in report["reason_codes"]


def test_removed_numeric_line_marks_its_metric_dropped_and_forces_notice() -> None:
    answer, report = enforce_numeric_copy_contract(
        "리바로 성장률 알려줘",
        "리바로 성장률은 99.9%입니다.",
        _result(),
    )

    assert "99.9%" not in answer
    assert "요청 지표 미제공" in answer
    assert report["dropped_metrics"] == ["growth"]
    assert "numeric_copy_blocked" in report["reason_codes"]


def test_blocked_duplicate_metric_line_does_not_report_block_when_metric_is_rendered() -> None:
    answer, report = enforce_numeric_copy_contract(
        "리바로 성장률 알려줘",
        (
            "| 브랜드 | 성장률 |\n"
            "| --- | ---: |\n"
            "| 리바로 | 12.3% |\n\n"
            "추정 성장률은 99.9%입니다."
        ),
        _result(),
    )

    assert "99.9%" not in answer
    assert "12.3%" in answer
    assert report["rendered_metrics"] == ["growth"]
    assert report["dropped_metrics"] == []
    assert "numeric_copy_blocked" not in report["reason_codes"]


def test_public_numeric_copy_summary_omits_private_tokens() -> None:
    public = _public_numeric_copy_summary(
        {
            "requested_metrics": ["growth"],
            "rendered_metrics": [],
            "dropped_metrics": ["growth"],
            "reason_codes": ["numeric_copy_blocked"],
            "blocked_tokens": ["99.9"],
        }
    )

    assert public == {
        "requested_metrics": ["growth"],
        "rendered_metrics": [],
        "dropped_metrics": ["growth"],
        "reason_codes": ["numeric_copy_blocked"],
    }


def test_partial_metric_gap_is_explained_without_hiding_rendered_metric() -> None:
    answer, report = enforce_numeric_copy_contract(
        "리바로 매출과 단가 알려줘",
        "| 브랜드 | 매출 |\n| --- | ---: |\n| 리바로 | 85.87억원 |",
        {
            "tool_calls": [
                {
                    "source": "UBIST",
                    "render_data": {"sales_억원": 85.87},
                }
            ]
        },
    )

    assert "85.87억원" in answer
    assert "요청 지표 미제공" in answer
    assert "정의·산식" in answer
    assert report["rendered_metrics"] == ["sales"]
    assert report["dropped_metrics"] == ["unit_price"]
