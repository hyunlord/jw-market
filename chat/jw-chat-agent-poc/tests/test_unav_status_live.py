from __future__ import annotations

from typing import Any

import pytest

from jw_chat_agent_poc.orchestrator.unavailable_response import apply_common_unavailable_response


QUESTION = "D693 상병 환자수 최근 5년 알려줘"
FACT_MD = """### D693 환자수 시계열 fact
| 연도 | 환자수 |
| --- | ---: |
| 2020 | 1,001명 |
| 2021 | 1,102명 |
| 2022 | 1,203명 |
| 2023 | 1,304명 |
| 2024 | 1,405명 |

일부 데이터 미보유"""
ANSWER = """D693 환자수는 다음과 같습니다.

| 연도 | 환자수 |
| --- | ---: |
| 2020 | 1,001명 |
| 2021 | 1,102명 |
| 2022 | 1,203명 |
| 2023 | 1,304명 |
| 2024 | 1,405명 |

## 출처
- HIRA"""
FIVE_STEP_HEADING = "### 미보유 데이터 처리"


def _call(status: str, *, evidence: bool) -> dict[str, Any]:
    return {
        "tool": "hira_disease",
        "status": status,
        "render_data": {
            "status": status,
            "ok": status in {"live", "ok"},
            "evidence": [{"metric": "환자수"}] if evidence else [],
        },
    }


def _two_pass(
    *,
    status: str,
    fact_md: str = FACT_MD,
    evidence: bool = False,
    connected_source_mode: bool = False,
) -> tuple[str, str]:
    inner = apply_common_unavailable_response(
        QUESTION,
        ANSWER,
        {"fact_md": fact_md},
    )
    outer = apply_common_unavailable_response(
        QUESTION,
        inner,
        {"fact_md": fact_md},
        tool_calls=[_call(status, evidence=evidence)],
        connected_source_mode=connected_source_mode,
    )
    return inner, outer


@pytest.mark.parametrize("connected_source_mode", (False, True))
def test_live_success_removes_early_unavailable_block_without_losing_values(
    connected_source_mode: bool,
) -> None:
    inner, outer = _two_pass(
        status="live",
        evidence=True,
        connected_source_mode=connected_source_mode,
    )

    assert FIVE_STEP_HEADING in inner
    assert FIVE_STEP_HEADING not in outer
    for year, value in (
        ("2020", "1,001명"),
        ("2021", "1,102명"),
        ("2022", "1,203명"),
        ("2023", "1,304명"),
        ("2024", "1,405명"),
    ):
        assert year in outer
        assert value in outer
    assert "## 출처" in outer
    if connected_source_mode:
        assert "- HIRA" in outer


def test_live_age_group_call_removes_block_without_changing_blocked_metadata() -> None:
    question = "D693 연령대별 환자수 알려줘"
    fact_md = "D693 0~9세 환자수 101명\n일부 데이터 미보유"
    metadata: dict[str, Any] = {
        "fact_md": fact_md,
        "blocked_claim_count": 10,
        "blocked_numbers": tuple(str(value) for value in range(10)),
    }
    inner = apply_common_unavailable_response(question, "0~9세 환자수는 101명입니다.", metadata)

    outer = apply_common_unavailable_response(
        question,
        inner,
        metadata,
        tool_calls=[_call("live", evidence=True)],
        connected_source_mode=True,
    )

    assert FIVE_STEP_HEADING in inner
    assert FIVE_STEP_HEADING not in outer
    assert metadata["blocked_claim_count"] == 10
    assert metadata["blocked_numbers"] == tuple(str(value) for value in range(10))


@pytest.mark.parametrize("status", ("error", "no_data"))
def test_failed_or_absent_call_keeps_early_unavailable_block(status: str) -> None:
    inner, outer = _two_pass(status=status)

    assert FIVE_STEP_HEADING in inner
    assert FIVE_STEP_HEADING in outer
    if status == "error":
        assert "현재 확인 불가" in outer
        assert "hira_disease" in outer
    else:
        assert "원천에 없음" in outer


def test_factless_ok_call_keeps_early_unavailable_block() -> None:
    inner, outer = _two_pass(status="ok", fact_md="데이터 미보유", evidence=True)

    assert FIVE_STEP_HEADING in inner
    assert FIVE_STEP_HEADING in outer


def test_non_hira_owned_metric_remains_unchanged() -> None:
    answer = "리바로는 2026-04 기준 매출 80.39억원입니다."

    revised = apply_common_unavailable_response(
        "리바로 매출 알려줘",
        answer,
        {"fact_md": "리바로 2026-04 매출 80.39억원"},
        tool_calls=[
            {
                "tool": "get_brand_metric",
                "status": "ok",
                "render_data": {"status": "ok", "sales_억원": 80.39},
            }
        ],
    )

    assert revised == answer
    assert FIVE_STEP_HEADING not in revised
