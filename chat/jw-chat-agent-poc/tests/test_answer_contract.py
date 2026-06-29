from __future__ import annotations

from jw_chat_agent_poc.orchestrator.answer_contract import enforce_answer_contract


TREND_FACT_MD = """## 확정 fact set

### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 매출 추이 | 페린젝트 매출 시계열 2023-Q3 41.53억원 → 2025-Q4 35.16억원, MS 29.34% → 25.36% |

### 페린젝트 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2023-Q3 | 41.53억원 | 29.34% |
| 2023-Q4 | 42.30억원 | 29.94% |
| 2024-Q1 | 36.69억원 | 26.88% |
| 2024-Q2 | 19.08억원 | 15.70% |
| 2024-Q3 | 24.32억원 | 17.33% |
| 2024-Q4 | 26.78억원 | 18.34% |
| 2025-Q1 | 25.91억원 | 20.56% |
| 2025-Q2 | 27.73억원 | 26.67% |
| 2025-Q3 | 31.84억원 | 24.75% |
| 2025-Q4 | 35.16억원 | 25.36% |

### 출처 유형 fact
| 출처 | 상세 |
| --- | --- |
| 데이터 상세 | IQVIA / UBIST — 기간 2020-Q3~2025-Q4, 시장 strategy_012, view strategic |
"""


RANKING_FACT_MD = """## 확정 fact set

### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 브랜드 핵심 지표 | 리바로 2026-04 매출 84.93억원 시장점유율 3.76% 순위 6/470 |

### 리바로 지표 fact
| 항목 | 값 |
| --- | --- |
| 브랜드/시장 | 리바로 |
| 지표 | market_share |
| 기간 | 2026-04 |
| 매출 | 84.93억원 |
| 시장점유율 | 3.76% |
| 순위 | 6/470 |

### 출처 유형 fact
| 출처 | 상세 |
| --- | --- |
| 데이터 상세 | UBIST — 기간 2025-07~2026-04, 시장 ml_006, view market_landscape |
"""


def test_trend_contract_reinserts_series_table_when_final_answer_is_empty_shell() -> None:
    # Given: verified trend facts exist, but final 514 returned a source-only shell.
    empty_shell = "확정 데이터 기준으로 정리하면 다음과 같습니다.\n\n## 출처\n- 데이터: UBIST / IQVIA NSA"

    # When: the post-generation answer contract is enforced.
    revised = enforce_answer_contract(
        "페린젝트 매출 추이 어때",
        empty_shell,
        {"fact_md": TREND_FACT_MD},
    )

    # Then: the deterministic answer includes a real trend summary and a min-row table.
    assert "페린젝트 매출은 2023-Q3 41.53억원에서 2025-Q4 35.16억원" in revised
    assert "| 기간 | 매출 | MS |" in revised
    assert revised.count("| 202") >= 4
    assert "## 출처" in revised


def test_ranking_contract_replaces_ubist_dash_with_verified_rank_answer() -> None:
    # Given: verified ranking facts exist, but final output is the invalid UBIST dash shell.
    empty_rank = "확정 데이터 기준으로 정리하면 다음과 같습니다.\n\n- UBIST: -\n\n## 출처\n- 데이터: UBIST"

    # When: the post-generation answer contract is enforced.
    revised = enforce_answer_contract(
        "리바로 점유율 몇 위야",
        empty_rank,
        {"fact_md": RANKING_FACT_MD},
    )

    # Then: rank, denominator, sales, share, period, and source are surfaced.
    assert "UBIST: -" not in revised
    assert "리바로는 2026-04 기준" in revised
    assert "매출 84.93억원" in revised
    assert "시장점유율 3.76%" in revised
    assert "순위 6/470" in revised
    assert "## 출처" in revised
