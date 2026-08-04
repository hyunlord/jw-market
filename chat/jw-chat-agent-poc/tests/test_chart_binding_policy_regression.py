from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jw_chat_agent_poc.service.charts import filter_charts_for_binding


_FIXTURE = Path(__file__).parent / "fixtures" / "chart_binding_live_shapes.v1.json"


def _fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case_name", ("market_only", "live_combined", "explicit_comovement"))
def test_chart_binding_policy_preserves_the_approved_three_shapes(case_name: str) -> None:
    fixture = _fixture()
    case = fixture["cases"][case_name]

    bound = filter_charts_for_binding(
        case["charts"],
        result=fixture["result"],
        question=case["question"],
    )

    assert [chart["title"] for chart in bound] == case["expected_titles"]


def test_live_combined_chart_keeps_both_brand_and_market_datasets() -> None:
    fixture = _fixture()
    case = fixture["cases"]["live_combined"]

    bound = filter_charts_for_binding(
        case["charts"],
        result=fixture["result"],
        question=case["question"],
    )

    assert [dataset["label"] for dataset in bound[0]["datasets"]] == [
        "리바로 매출",
        "시장 매출",
    ]
