from __future__ import annotations

from jw_chat_agent_poc.orchestrator.claim_policy import apply_claim_policy
from jw_chat_agent_poc.orchestrator.answer_contract import enforce_answer_contract
from jw_chat_agent_poc.orchestrator.unavailable_response import apply_common_unavailable_response, sanitize_internal_diagnostics


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


AXIS_FACT_MD = """## 확정 fact set

### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 상위 제형 추이 | 1위 Statin/EZE 2025-05 MS 55.63% → 2026-04 MS 59.05%, 점유율 변화 +3.42%p |

### 월별 MS fact
| 기간 | 리바로 MS | 시장 |
| --- | --- | --- |
| 2025-05 | 3.92% | ml_006 |
| 2026-04 | 3.76% | ml_006 |

### 출처 유형 fact
| 출처 | 상세 |
| --- | --- |
| 데이터 상세 | UBIST — 기간 2025-05~2026-04, 시장 ml_006, view market_landscape, denominator_basis market_landscape rows 470개 |
"""


NEWS_FACT_MD = TREND_FACT_MD + """

### 인사이트 근거 fact - 뉴스/이슈
| 날짜 | 제목 | 출처 | URL | 요약 | 매칭 발췌 |
| --- | --- | --- | --- | --- | --- |
| 2026-06-20 | 이상지질혈증 복합제 경쟁 심화 | 데일리팜 | https://example.test/news/1 | 로수바스타틴 복합제가 시장 경쟁을 키웠다는 내용 | 경쟁 심화 |
| 2026-06-25 | JW중외제약 리바로 영업 채널 확대 | 메디칼타임즈 | https://example.test/news/2 | 의원 채널 활동을 확대한다는 내용 | 영업 채널 확대 |
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


def test_sales_activity_contract_adds_missing_data_analysis_design() -> None:
    answer = "매출 추이는 확인됩니다. 최신 값은 저점 이후 회복 흐름을 보여줍니다.\n\n## 출처\n- 데이터: UBIST"

    revised = enforce_answer_contract(
        "[리바로] 영업활동의 Impact level에 변화가 있는가? 매출 추이와 영업 활동(상기 콜)의 연계성 파악",
        answer,
        {"fact_md": TREND_FACT_MD},
    )

    assert "## 영업-매출 연계 분석 설계" in revised
    assert "영업활동 데이터 보유 여부" in revised
    assert "1. 미보유 데이터" in revised
    assert "3. 해석 가능한 상한선" in revised
    assert "CSD 영업활동" in revised
    assert "콜 수" in revised
    assert "활동 전후 1~3개월" in revised
    assert "회복 흐름" not in revised
    assert "MS는 29.34%에서 25.36%로 낮아졌습니다" in revised
    assert revised.index("## 영업-매출 연계 분석 설계") < revised.index("## 출처")


def test_trend_support_contract_adds_axis_support_matrix() -> None:
    answer = "제형 축 변화는 확인됩니다.\n\n## 출처\n- 데이터: UBIST"

    revised = enforce_answer_contract(
        "[리바로] Weekly 및 Monthly 별로 추이 변화는 어떠하며, Class/Molecule/브랜드/용량 및 제형 단에서의 추이의 변화가 있는가?",
        answer,
        {"fact_md": AXIS_FACT_MD},
    )

    assert "## 추이 지원 범위" in revised
    assert "| Weekly | 미지원 |" in revised
    assert "| Monthly | 지원 |" in revised
    assert "| Form | 지원 |" in revised
    assert "| Dose | 미지원 |" in revised
    assert "### 지원 축 so-what" in revised
    assert "Statin/EZE(제형)" in revised
    assert "+3.42%p 상승" in revised
    assert "+3.42p 상승" not in revised


def test_change_drivers_contract_adds_external_internal_table() -> None:
    answer = "보유 proxy 기준으로만 설명합니다.\n\n## 출처\n- 데이터: UBIST"

    revised = enforce_answer_contract(
        "[리바로] 목표 시장에서의 향후 예상되는 시장 변화 요인이 있는가? - External: 타사 경쟁품 출시,  Market expansion, 보건 정책 변화(약가인하 등) - Internal: 자사 Line extension, 영업/채널 (타겟 Segment)",
        answer,
        {"fact_md": TREND_FACT_MD},
    )

    assert "## 변화 요인 결론" in revised
    assert "| External |" in revised
    assert "| Internal |" in revised
    assert "### 미보유·확인필요" in revised
    assert "해석 가능한 상한선" in revised
    assert "이벤트 전후 1~3개월" in revised


def test_change_drivers_contract_classifies_news_into_grounded_rows() -> None:
    answer = "채널 현황입니다.\n\n## 출처\n- 데이터: UBIST"

    revised = enforce_answer_contract(
        "[리바로] 목표 시장에서의 향후 예상되는 시장 변화 요인이 있는가? External/Internal로 정리",
        answer,
        {"fact_md": NEWS_FACT_MD},
    )

    assert "## 변화 요인 결론" in revised
    assert "뉴스 fact를 정성 근거로 분류해 연결합니다" not in revised
    assert "이상지질혈증 복합제 경쟁 심화" in revised
    assert "https://example.test/news/1" in revised
    assert "| External | 경쟁/시장 뉴스 |" in revised
    assert "JW중외제약 리바로 영업 채널 확대" in revised
    assert "https://example.test/news/2" in revised
    assert "| Internal | 자사 영업/채널 뉴스 |" in revised
    assert "| External | 정책/약가 변화 | 미보유 | 불확실 |" in revised


def test_change_drivers_contract_can_be_reapplied_after_channel_claim_policy() -> None:
    question = "[리바로] 목표 시장에서의 향후 예상되는 시장 변화 요인이 있는가? - External: 타사 경쟁품 출시,  Market expansion, 보건 정책 변화(약가인하 등) - Internal: 자사 Line extension, 영업/채널 (타겟 Segment)"
    channel_fact_md = """## 확정 fact set

### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| channel 상위 | 1위 의원 시장점유율 3.37% 매출 41.93억원 |

### 출처 유형 fact
| 출처 | 상세 |
| --- | --- |
| 데이터 상세 | UBIST — 기간 2026-04, market_id ml_006 |
"""
    answer = "채널별 현황입니다.\n\n| 채널 | 시장점유율 | 매출 |\n| --- | --- | --- |\n| 의원 | 3.37% | 41.93억원 |\n\n## 출처\n- 데이터: UBIST"

    with_contract = enforce_answer_contract(question, answer, {"fact_md": channel_fact_md})
    policy_rewritten = apply_claim_policy(question, with_contract, channel_fact_md)
    revised = enforce_answer_contract(question, policy_rewritten, {"fact_md": channel_fact_md})

    assert "## 변화 요인 결론" in revised
    assert "| External |" in revised
    assert "| Internal |" in revised
    assert "## 출처" in revised


def test_simple_lookup_contract_does_not_expand_answer() -> None:
    answer = "리바로는 2026-04 기준 매출 84.93억원, 순위 6/516입니다.\n\n## 출처\n- 데이터: UBIST"

    revised = enforce_answer_contract(
        "[리바로] 매출 알려줘",
        answer,
        {"fact_md": RANKING_FACT_MD},
    )

    assert revised == answer


def test_common_unavailable_layer_sanitizes_internal_cache_diagnostics_and_adds_5step() -> None:
    answer = (
        "요청한 출처 교차 지표는 확인 불가합니다.\n\n"
        "- cache_cause row is missing: CausePayloadKey(brand='리바로', view_type='market_landscape', "
        "source='UBIST', measure='sales', market_id='strategy_006')\n\n"
        "## 출처\n- 데이터: UBIST"
    )

    revised = apply_common_unavailable_response(
        "[리바로] UBIST와 IQVIA 출처 교차로 시장 규모를 비교해줘",
        answer,
        {"fact_md": "### 필수 답변 fact\n| 구분 | 반드시 반영할 내용 |\n| --- | --- |\n| 출처교차 | 데이터 미보유 |"},
    )

    assert "cache_cause" not in revised
    assert "CausePayloadKey" not in revised
    assert "market_id" not in revised
    assert "strategy_006" not in revised
    assert "요청한 일부 지표는 현재 운영 데이터에서 확정 경로를 찾지 못했습니다." in revised
    assert "### 미보유 데이터 처리" in revised
    assert "1. 미보유 데이터" in revised
    assert "2. 현재 가능한 proxy" in revised
    assert "3. 해석 가능한 상한선" in revised
    assert "4. 확인 필요 데이터" in revised
    assert "5. 확보 시 수행할 분석" in revised
    assert revised.index("### 미보유 데이터 처리") < revised.index("## 출처")


def test_common_unavailable_layer_does_not_fire_for_owned_metric_answer() -> None:
    answer = "리바로는 2026-04 기준 매출 84.93억원, 시장점유율 3.76%, 순위 6/470입니다."

    revised = apply_common_unavailable_response("[리바로] 매출 알려줘", answer, {"fact_md": RANKING_FACT_MD})

    assert revised == answer
    assert "미보유 데이터 처리" not in revised


def test_common_unavailable_layer_does_not_duplicate_existing_5step_block() -> None:
    answer = """### 미보유 데이터 처리
| 단계 | 내용 |
| --- | --- |
| 1. 미보유 데이터 | CSD 영업활동입니다. |
| 2. 현재 가능한 proxy | UBIST 매출입니다. |
| 3. 해석 가능한 상한선 | 인과를 증명하지 않습니다. |
| 4. 확인 필요 데이터 | 콜 수입니다. |
| 5. 확보 시 수행할 분석 | 전후 비교입니다. |
"""

    revised = apply_common_unavailable_response("[리바로] 영업활동 Impact를 봐줘", answer, {"fact_md": "데이터 미보유"})

    assert revised.count("### 미보유 데이터 처리") == 1


def test_common_unavailable_layer_fires_for_external_unavailable_source_question() -> None:
    answer = "리바로의 국내 매출 흐름은 UBIST 기준으로 확인됩니다.\n\n## 출처\n- 데이터: UBIST"

    revised = apply_common_unavailable_response(
        "[리바로] Datamonitor 기준 글로벌 시장 전망을 알려줘",
        answer,
        {"fact_md": RANKING_FACT_MD},
    )

    assert "### 미보유 데이터 처리" in revised
    assert "Datamonitor 등 글로벌 시장 전망" in revised
    assert revised.index("### 미보유 데이터 처리") < revised.index("## 출처")


def test_common_unavailable_layer_uses_forecast_specific_5step_for_prediction_questions() -> None:
    answer = "현재 데이터로 답변 불가합니다. forecast 데이터는 P1 POC 데이터 범위 밖입니다."

    revised = apply_common_unavailable_response(
        "[리바로] 향후 시장 규모와 매출을 예측해줘",
        answer,
        {"fact_md": "데이터 미보유"},
    )

    assert "### 미보유 데이터 처리" in revised
    assert "예측 모델" in revised
    assert "과거 실적 추세" in revised
    assert "참고용" in revised
    assert "추세는 예측이 아닙니다" in revised
    assert "forecast 시계열" in revised


def test_common_unavailable_layer_does_not_infer_forecast_from_answer_wording() -> None:
    answer = "출처 교차 추이 비교는 데이터 미보유입니다. 향후 데이터 확보가 필요합니다."

    revised = apply_common_unavailable_response(
        "리바로의 UBIST와 IQVIA 출처 교차로 추이를 비교해줘",
        answer,
        {"fact_md": "데이터 미보유"},
    )

    assert "출처 간 교차 검증" in revised
    assert "동일 기간의 UBIST/IQVIA" in revised
    assert "forecast 시계열" not in revised


def test_common_unavailable_layer_fires_for_no_data_wording_from_final_answer() -> None:
    answer = "리바로의 UBIST 및 IQVIA 출처 기반 시장 규모 비교는 현재 제공된 데이터가 없어 수행할 수 없습니다."

    revised = apply_common_unavailable_response(
        "리바로의 UBIST와 IQVIA 출처를 교차해서 시장 규모를 비교해줘",
        answer,
        {"fact_md": ""},
    )

    assert "### 미보유 데이터 처리" in revised
    assert "출처 간 교차 검증" in revised
    assert "forecast 시계열" not in revised


def test_common_unavailable_layer_fires_for_positioning_channel_segment_gap() -> None:
    answer = "리바로의 포지셔닝은 보유 시장 지표 중심으로 확인됩니다.\n\n## 출처\n- 데이터: UBIST"

    revised = apply_common_unavailable_response(
        "[리바로] 채널과 세그먼트 기준 포지셔닝을 분석해줘",
        answer,
        {"fact_md": RANKING_FACT_MD},
    )

    assert "### 미보유 데이터 처리" in revised
    assert "요청 축의 세그먼트 원천 행" in revised


def test_sanitize_internal_diagnostics_keeps_public_source_context() -> None:
    answer = "| 지표 | cache_cause response_json must be a JSON object |\n| 출처 | UBIST |"

    revised = sanitize_internal_diagnostics(answer)

    assert "response_json" not in revised
    assert "cache_cause" not in revised
    assert "UBIST" in revised
    assert "현재 운영 데이터에서 확정 경로를 찾지 못했습니다" in revised


def test_sanitize_internal_diagnostics_removes_bare_internal_market_ids() -> None:
    answer = "| 데이터 상세 | IQVIA / UBIST - 기간 2020-Q3~2025-Q4, 시장 strategy_006, view strategic |"

    revised = sanitize_internal_diagnostics(answer)

    assert "strategy_006" not in revised
    assert "확정 시장" in revised
    assert "IQVIA / UBIST" in revised


def test_sanitize_internal_diagnostics_preserves_denominator_note_market_ids() -> None:
    answer = (
        "- 데이터 상세: UBIST — 기간 2025-07~2026-04, 시장: ml_006 (market_landscape, 분모 470), "
        "참고: strategy_006 기준 순위는 6/516으로 표시될 수 있음"
    )

    revised = sanitize_internal_diagnostics(answer)

    assert "시장: ml_006" in revised
    assert "참고: strategy_006 기준 순위는 6/516으로 표시될 수 있음" in revised
    assert "확정 시장" not in revised


def test_sanitize_internal_diagnostics_still_blocks_market_ids_in_error_context() -> None:
    answer = (
        "cache_cause row is missing: CausePayloadKey(brand='리바로', view_type='market_landscape', "
        "source='UBIST', measure='sales', market_id='strategy_006')"
    )

    revised = sanitize_internal_diagnostics(answer)

    assert "cache_cause" not in revised
    assert "CausePayloadKey" not in revised
    assert "market_id" not in revised
    assert "strategy_006" not in revised
    assert "요청한 일부 지표는 현재 운영 데이터에서 확정 경로를 찾지 못했습니다." in revised
