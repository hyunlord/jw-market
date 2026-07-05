from __future__ import annotations

from jw_chat_agent_poc.orchestrator.claim_policy import apply_claim_policy
from jw_chat_agent_poc.orchestrator.answer_contract import answer_contract_backfill_tool_calls, enforce_answer_contract, evaluate_answer_contract
from jw_chat_agent_poc.orchestrator.source_trap import apply_requested_source_trap_gate
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


SEGMENT_COMPARE_FACT_MD = """## 확정 fact set

### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| Molecule 지원 | UBIST 2026-04 기준 1위 RSV/EZE 매출 749.07억원, MS 33.19% |
| 브랜드 지원 | UBIST 2026-04 기준 1위 로수젯 매출 206.85억원, MS 9.17% |
| 제형 지원 | UBIST 2026-04 기준 1위 Statin/EZE 매출 1332.65억원, MS 59.05% |
| Class 미지원 | Class 축은 현재 catalog/query 경로에서 지원되지 않습니다. |
| 용량 미지원 | 용량 축은 현재 catalog/query 경로에서 지원되지 않습니다. |

### 출처 유형 fact
| 출처 | 상세 |
| --- | --- |
| 데이터 상세 | UBIST — 기간 2026-04, 시장 ml_006, view market_landscape |
"""


SOURCE_CROSSCHECK_FACT_MD = """## 확정 fact set

### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| UBIST 보유 | 리바로 UBIST 2025-04→2026-04 매출 70.00억원→90.00억원, MS 8.56%→11.00% |
| IQVIA 미보유 | IQVIA 출처는 현재 ml_006 기간 mart에서 조회되지 않습니다. |

### 출처 유형 fact
| 출처 | 상세 |
| --- | --- |
| 데이터 상세 | UBIST — 기간 2025-04~2026-04, 시장 ml_006, view market_landscape |
"""


POSITIONING_FACT_MD = """## 확정 fact set

### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 브랜드 핵심 지표 | 리바로 2026-04 매출 84.93억원 시장점유율 3.76% 순위 6/470 |
| 인사이트 계산 | 리바로젯 share-of-growth +0.53%p, cohort z-score 1.24, 백분위 82% |
| 인사이트 계산 | 시장 변화 top gainer 리바로젯 +0.53%p, top faller 리피토 -0.56%p |

### 출처 유형 fact
| 출처 | 상세 |
| --- | --- |
| 데이터 상세 | UBIST — 기간 2026-04, 시장 ml_006, view market_landscape |
"""


THREAT_FACT_MD = POSITIONING_FACT_MD + """

### 인사이트 근거 fact - 뉴스/이슈
| 날짜 | 제목 | 출처 | URL | 요약 | 매칭 발췌 |
| --- | --- | --- | --- | --- | --- |
| 2026-06-20 | 이상지질혈증 복합제 경쟁 심화 | 데일리팜 | https://example.test/threat/1 | 복합제 경쟁이 확대된다는 내용 | 경쟁 심화 |
"""


E1_NEWS_FACT_MD = """## 확정 fact set

### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| 브랜드 핵심 지표 | 리바로 2026-04 매출 84.93억원 시장점유율 3.76% 순위 6/470 |

### 인사이트 근거 fact - 뉴스/이슈
| 날짜 | 제목 | 출처 | URL | 요약 | 매칭 발췌 |
| --- | --- | --- | --- | --- | --- |
| 2026-06-25 | JW중외제약 리바로 영업 채널 확대 | 메디칼타임즈 | https://example.test/news/2 | 의원 채널 활동을 확대한다는 내용 | 영업 채널 확대 |
| 2026-06-20 | 이상지질혈증 복합제 경쟁 심화 | 데일리팜 | https://example.test/news/1 | 로수바스타틴 복합제가 시장 경쟁을 키웠다는 내용 | 경쟁 심화 |
| 2026-06-18 | 디지털 헬스케어 RAG 기술 소개 | IT뉴스 | https://example.test/news/3 | 검색 증강 생성 기술 기사 | RAG |
"""


SPECIALTY_FACT_MD = """## 확정 fact set

### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| specialty 상위 | 1위 분리되지 않은 내과 시장점유율 3.54% 매출 31.80억원 |
| specialty 상위 | 2위 내분비 시장점유율 1.20% 매출 10.83억원 |
| specialty 상위 | 3위 순환기 시장점유율 1.17% 매출 10.54억원 |

### 출처 유형 fact
| 출처 | 상세 |
| --- | --- |
| 데이터 상세 | UBIST — 기간 2026-04, 시장 ml_006, view market_landscape |
"""


SPECIALTY_DATA_TABLE_MD = """## 확정 fact set

### 분석 기준별 점유율
| 순위 | 구분 | MS | 매출 |
| --- | --- | --- | --- |
| 1 | 분리되지 않은 내과 | 3.54% | 31.80억원 |
| 2 | 내분비 | 1.20% | 10.83억원 |
| 3 | 순환기 | 1.17% | 10.54억원 |

### 출처 유형 fact
| 출처 | 상세 |
| --- | --- |
| 데이터 상세 | UBIST — 기간 2026-04, 시장 ml_006, view market_landscape |
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


def test_structural_contract_backfill_requests_metric_proxy() -> None:
    plans = answer_contract_backfill_tool_calls(
        "악템라 영업활동 Impact와 매출 연계성을 분석해줘",
        "악템라",
        [],
    )

    assert len(plans) == 1
    assert plans[0].name == "get_metric"
    assert plans[0].arguments == {"brand": "악템라", "measure": "sales", "period": "latest"}


def test_structural_contract_backfill_skips_existing_metric_proxy() -> None:
    plans = answer_contract_backfill_tool_calls(
        "악템라 시장 변화 요인과 매출 변화를 종합해서 인과 분석해줘",
        "악템라",
        [
            {
                "tool": "get_brand_metric",
                "render_data": {
                    "brand": "악템라",
                    "sales_krw": 4_819_000_000,
                    "source_status": "OK",
                },
            }
        ],
    )

    assert plans == ()


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


def test_segment_compare_contract_surfaces_supported_and_missing_axes() -> None:
    answer = "확보되지 않아 분석 불가능합니다.\n\n## 출처\n- 데이터: UBIST"

    revised = enforce_answer_contract(
        "리바로 처방을 Class/Molecule/브랜드/용량/제형 세그먼트별로 비교해줘",
        answer,
        {"fact_md": SEGMENT_COMPARE_FACT_MD},
    )

    assert "## 세그먼트 비교 지원 범위" in revised
    assert "| Molecule | 지원 |" in revised
    assert "| 브랜드 | 지원 |" in revised
    assert "| 제형 | 지원 |" in revised
    assert "| Class | 미지원 |" in revised
    assert "| 용량 | 미지원 |" in revised
    assert "1332.65억원" in revised
    assert "### 미지원 축 처리" in revised
    assert "확보되지 않아 분석 불가능" not in revised
    status = evaluate_answer_contract(
        "리바로 처방을 Class/Molecule/브랜드/용량/제형 세그먼트별로 비교해줘",
        revised,
        {"fact_md": SEGMENT_COMPARE_FACT_MD},
    )
    assert status["structural_contract"] == "segment_compare"
    assert status["status"] == "pass"


def test_segment_compare_contract_handles_company_manufacturer_axes() -> None:
    answer = (
        "회사 및 제조사 정보는 데이터 미보유입니다.\n"
        "| 순위 | 구분(성분 조합) | 매출 | 시장점유율 |\n"
        "| --- | --- | --- | --- |\n"
        "| 1 | Statin/EZE | 1,332.65억원 | 59.05% |\n"
        "| 2 | Statin | 924.13억원 | 40.95% |\n\n"
        "## 출처\n- 데이터: UBIST"
    )

    revised = enforce_answer_contract(
        "리바로 회사와 제조사, 제형별 처방 세그먼트를 비교해줘",
        answer,
        {"fact_md": RANKING_FACT_MD},
    )

    assert "## 세그먼트 비교 지원 범위" in revised
    assert "| 회사 | 미지원 |" in revised
    assert "| 제조사 | 미지원 |" in revised
    assert "| 제형 | 지원 |" in revised
    assert "### 미지원 축 처리" in revised
    assert "Statin/EZE vs Statin" in revised


def test_specialty_breakdown_contract_surfaces_existing_specialty_rows() -> None:
    answer = "진료과별 데이터는 현재 제공된 데이터가 없어 수행할 수 없습니다.\n\n## 출처\n- 데이터: UBIST"

    revised = enforce_answer_contract(
        "리바로 진료과별 매출 구성을 알려줘",
        answer,
        {"fact_md": SPECIALTY_FACT_MD},
    )

    assert "## 진료과별 매출 구성" in revised
    assert "| 진료과 | 순위/값 |" in revised
    assert "분리되지 않은 내과" in revised
    assert "31.80억원" in revised
    assert "순환기" in revised
    assert "수행할 수 없습니다" not in revised
    assert revised.index("## 진료과별 매출 구성") < revised.index("## 출처")
    status = evaluate_answer_contract(
        "리바로 진료과별 매출 구성을 알려줘",
        revised,
        {"fact_md": SPECIALTY_FACT_MD},
    )
    assert status["structural_contract"] == "specialty_breakdown"
    assert status["status"] == "pass"


def test_specialty_breakdown_contract_uses_data_md_when_fact_md_is_summary_only() -> None:
    answer = "진료과별 데이터는 현재 제공된 데이터가 없어 수행할 수 없습니다.\n\n## 출처\n- 데이터: UBIST"

    revised = enforce_answer_contract(
        "리바로 진료과별 매출 구성을 알려줘",
        answer,
        {"fact_md": "## 확정 fact set\n\n요약만 존재", "data_md": SPECIALTY_FACT_MD},
    )

    assert "## 진료과별 매출 구성" in revised
    assert "분리되지 않은 내과" in revised
    assert "31.80억원" in revised
    assert "수행할 수 없습니다" not in revised


def test_specialty_breakdown_contract_reads_specialty_fact_table() -> None:
    answer = "진료과별 데이터는 현재 제공된 데이터가 없어 수행할 수 없습니다.\n\n## 출처\n- 데이터: UBIST"

    revised = enforce_answer_contract(
        "리바로 진료과별 매출 구성을 알려줘",
        answer,
        {"fact_md": "## 확정 fact set\n\n요약만 존재", "data_md": SPECIALTY_DATA_TABLE_MD},
    )

    assert "## 진료과별 매출 구성" in revised
    assert "분리되지 않은 내과" in revised
    assert "31.80억원" in revised
    assert "수행할 수 없습니다" not in revised


def test_general_dosage_combination_note_fires_outside_segment_contract() -> None:
    answer = (
        "### 리바로 제형별(성분 조합) 시장 현황\n"
        "| 제형 | 매출 | MS |\n"
        "| --- | --- | --- |\n"
        "| Statin/EZE | 1332.65억원 | 59.05% |\n"
        "| Statin | 923.67억원 | 40.95% |\n\n"
        "## 출처\n- 데이터: UBIST"
    )

    revised = enforce_answer_contract(
        "리바로 제형별 회사 구성을 비교해줘",
        answer,
        {"fact_md": RANKING_FACT_MD},
    )

    assert "※ 본 시장의 제형 구분은 성분 조합 기준" in revised
    assert "Statin/EZE vs Statin" in revised
    assert revised.index("※ 본 시장의 제형 구분") < revised.index("## 출처")


def test_general_dosage_combination_note_ignores_rank_table_headers() -> None:
    answer = (
        "### 리바로 제형별(성분 조합) 시장 현황\n"
        "| 순위 | 구분 | 매출 | MS |\n"
        "| --- | --- | --- | --- |\n"
        "| 1 | Statin/EZE | 1332.65억원 | 59.05% |\n"
        "| 2 | Statin | 923.67억원 | 40.95% |\n\n"
        "## 출처\n- 데이터: UBIST"
    )

    revised = enforce_answer_contract(
        "리바로 제형별 회사 구성을 비교해줘",
        answer,
        {"fact_md": RANKING_FACT_MD},
    )

    assert "Statin/EZE vs Statin" in revised
    assert "Statin/EZE vs 순위" not in revised


def test_source_crosscheck_contract_keeps_single_source_values_without_cross_claim() -> None:
    answer = "UBIST와 IQVIA 교차 확인은 불가합니다.\n\n## 출처\n- 데이터: UBIST"

    revised = enforce_answer_contract(
        "리바로 UBIST와 IQVIA 데이터 출처별로 교차 확인해줘",
        answer,
        {"fact_md": SOURCE_CROSSCHECK_FACT_MD},
    )

    assert "## 출처별 교차 확인 범위" in revised
    assert "| UBIST | 보유 |" in revised
    assert "| IQVIA | 미보유 |" in revised
    assert "70.00억원→90.00억원" in revised
    assert "양 소스가 모두 확보될 때만 일치/불일치" in revised
    assert "일치합니다" not in revised
    assert "불일치합니다" not in revised
    status = evaluate_answer_contract(
        "리바로 UBIST와 IQVIA 데이터 출처별로 교차 확인해줘",
        revised,
        {"fact_md": SOURCE_CROSSCHECK_FACT_MD},
    )
    assert status["structural_contract"] == "source_crosscheck"
    assert status["status"] == "pass"


def test_source_crosscheck_contract_does_not_fire_without_source_tokens() -> None:
    answer = "포지셔닝 축은 보유 경쟁구도 fact 기준으로만 봅니다."

    revised = enforce_answer_contract(
        "리바로 시장 내 포지셔닝을 경쟁 제품과 비교해줘",
        answer,
        {"fact_md": SOURCE_CROSSCHECK_FACT_MD},
    )

    assert revised == answer
    assert evaluate_answer_contract("리바로 시장 내 포지셔닝을 경쟁 제품과 비교해줘", revised, {"fact_md": SOURCE_CROSSCHECK_FACT_MD})[
        "status"
    ] == "not_applicable"


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
    assert "관련성 등급" in revised
    assert "| External | 경쟁/시장 뉴스 | market |" in revised
    assert "JW중외제약 리바로 영업 채널 확대" in revised
    assert "https://example.test/news/2" in revised
    assert "| Internal | 자사 영업/채널 뉴스 | direct |" in revised
    assert "| External | 정책/약가 변화 | 미보유 | 미보유 | 불확실 |" in revised


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


def test_source_trap_gate_blocks_cortellis_label_and_separates_clinicaltrials_reference() -> None:
    answer = (
        "Cortellis 기준 파이프라인 현황입니다.\n"
        "Venetoclax 백혈병 병용 임상은 리바로 적응증 확장 가능성 및 상업 경쟁 압력입니다.\n"
        "### 임상시험\n"
        "| ID | 제목 | 상태 |\n"
        "| --- | --- | --- |\n"
        "| NCT01764178 | ClinicalTrials safety study | Completed |\n"
        "## 출처\n"
        "- 외부 API: ClinicalTrials/MFDS 임상 정보\n"
    )

    revised = apply_requested_source_trap_gate(
        "Cortellis 기준 이상지질혈증 파이프라인과 리바로 경쟁 임상 현황을 분석해줘",
        answer,
    )

    assert revised.startswith("Cortellis 데이터는 현재 운영 데이터에 미보유입니다.")
    assert "Cortellis 기준" not in revised
    assert "### 대체 참고" in revised
    assert "ClinicalTrials/MFDS 결과는 Cortellis 데이터가 아니므로" in revised
    assert "적응증 확장 가능성" not in revised
    assert "상업 경쟁 압력" not in revised
    assert "NCT01764178" in revised


def test_source_trap_gate_generalizes_to_requested_unavailable_sources() -> None:
    for question, expected in (
        ("리바로 KOL 자문 기준 처방 의견을 알려줘", "KOL 자문 데이터는 현재 운영 데이터에 미보유입니다."),
        ("리바로 NCCN 치료 지침 기준 시장 영향을 알려줘", "NCCN/가이드라인 데이터는 현재 운영 데이터에 미보유입니다."),
        ("리바로 Datamonitor 기준 글로벌 시장 전망을 알려줘", "Datamonitor 데이터는 현재 운영 데이터에 미보유입니다."),
    ):
        revised = apply_requested_source_trap_gate(question, "UBIST 매출 proxy만 확인됩니다.")

        assert revised.startswith(expected)


def test_source_trap_gate_compacts_final_answer_once_common_5step_exists() -> None:
    answer = (
        "Cortellis 데이터는 현재 운영 데이터에 미보유입니다.\n\n"
        "리바로는 Venetoclax 병용 임상으로 적응증 확장 가능성을 탐색했습니다.\n\n"
        "### 미보유 데이터 처리\n"
        "| 단계 | 내용 |\n"
        "| --- | --- |\n"
        "| 1. 미보유 데이터 | Cortellis/파이프라인 원천 데이터입니다. |\n"
        "| 2. 현재 가능한 proxy | ClinicalTrials 참고만 가능합니다. |\n"
        "| 3. 해석 가능한 상한선 | 출시 가능성을 추정하지 않습니다. |\n"
        "| 4. 확인 필요 데이터 | 임상 단계와 예상 출시일입니다. |\n"
        "| 5. 확보 시 수행할 분석 | 경쟁 위협도를 나눕니다. |\n\n"
        "### 대체 참고\n"
        "- ClinicalTrials 결과\n\n"
        "## 출처\n"
        "- 외부: ClinicalTrials/MFDS 임상 정보\n"
    )

    revised = apply_requested_source_trap_gate(
        "Cortellis 기준 이상지질혈증 파이프라인과 리바로 경쟁 임상 현황을 분석해줘",
        answer,
    )

    assert revised.startswith("Cortellis 데이터는 현재 운영 데이터에 미보유입니다.")
    assert "적응증 확장 가능성" not in revised
    assert "Venetoclax" not in revised
    assert "### 미보유 데이터 처리" in revised
    assert "### 대체 참고" in revised
    assert "요청 소스 결론으로 승격하지 않습니다" in revised
    assert "## 출처" in revised


def test_source_trap_gate_does_not_touch_general_external_question() -> None:
    answer = "리바로 임상은 ClinicalTrials 참고 결과를 기준으로 확인됩니다."

    assert apply_requested_source_trap_gate("리바로 임상 알려줘", answer) == answer


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
        {"fact_md": "IQVIA 미보유"},
    )

    assert "### 미보유 데이터 처리" in revised
    assert "출처 간 교차 검증" in revised
    assert "forecast 시계열" not in revised


def test_common_unavailable_layer_does_not_fire_for_positioning_without_unavailable_signal() -> None:
    answer = (
        "리바로의 포지셔닝은 보유 시장 지표 중심으로 확인됩니다.\n"
        "직접 처방 이동은 확인할 수 없습니다.\n"
        "단일제의 임상적 가치는 별도 근거로만 판단합니다.\n\n"
        "## 출처\n- 데이터: UBIST"
    )

    revised = apply_common_unavailable_response(
        "[리바로] 채널과 세그먼트 기준 포지셔닝을 분석해줘",
        answer,
        {"fact_md": RANKING_FACT_MD},
    )

    assert revised == answer
    assert "### 미보유 데이터 처리" not in revised
    assert "Cortellis/파이프라인" not in revised


def test_common_unavailable_layer_does_not_choose_cortellis_from_answer_wording() -> None:
    answer = "단일제의 임상적 가치는 별도 근거로 판단합니다."

    revised = apply_common_unavailable_response(
        "리바로 시장 내 포지셔닝을 경쟁 제품과 비교해줘",
        answer,
        {"fact_md": "리바로 경쟁 브랜드 지표 fact"},
    )

    assert revised == answer
    assert "Cortellis/파이프라인" not in revised


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


def test_sanitize_internal_diagnostics_preserves_intended_split_market_context() -> None:
    answer = (
        "- 데이터 상세: IQVIA NSA — 기간 2025-Q4, 시장: ml_011 (market_landscape, 분모 26), "
        "Class 구분 존재: 운영 노출은 Class 2 기준 분모 26; "
        "전체 market_landscape 분모와 Class 기준 분모는 직접 비교하지 않음"
    )

    revised = sanitize_internal_diagnostics(answer)

    assert "시장: ml_011 (market_landscape, 분모 26)" in revised
    assert "Class 구분 존재" in revised
    assert "Class 2 기준 분모 26" in revised
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


def test_trend_support_matrix_reflects_split_market_structure_on_query_failure() -> None:
    answer = (
        "악템라의 Class 축은 현재 지원하지 않습니다.\n\n"
        "## 추이 지원 범위\n"
        "| 축 | 지원 여부 | 처리 |\n"
        "| --- | --- | --- |\n"
        "| Class | 미지원 | Class 축 fact가 있을 때만 별도 해석합니다. |\n\n"
        "## 출처\n- 데이터: IQVIA NSA"
    )
    fact_md = (
        "### 필수 답변 fact\n"
        "| 구분 | 반드시 반영할 내용 |\n"
        "| --- | --- |\n"
        "| 조회 실패 | 요청한 지표 조회 실행이 실패했습니다. 데이터 미보유로 해석하지 않습니다. |\n"
        "| Class 구조 기준 | Class 구분 존재: 운영 노출은 Class 2 기준 분모 26; "
        "전체 market_landscape 분모와 Class 기준 분모는 직접 비교하지 않음 |\n"
    )

    revised = enforce_answer_contract(
        "[악템라] Weekly/Monthly와 Class/Molecule 추이를 보여줘",
        answer,
        {"fact_md": fact_md},
    )

    assert "| Class | 지원(구조) | Class 구분 존재: 운영 노출은 Class 2 기준 분모 26; 전체 market_landscape 분모와 Class 기준 분모는 직접 비교하지 않음 |" in revised
    assert "| Class | 미지원 |" not in revised


def test_positioning_contract_adds_axis_table_and_dedupes_substantive_lines() -> None:
    answer = (
        "인사이트: 리바로젯 share-of-growth +0.53%p, cohort z-score 1.24입니다.\n"
        "인사이트: 리바로젯 share-of-growth +0.53%p, cohort z-score 1.24입니다.\n\n"
        "## 출처\n- 데이터: UBIST"
    )

    revised = enforce_answer_contract(
        "리바로의 경쟁 대비 포지셔닝과 차별점은?",
        answer,
        {"fact_md": POSITIONING_FACT_MD},
    )

    assert revised.count("인사이트: 리바로젯 share-of-growth +0.53%p, cohort z-score 1.24입니다.") == 1
    assert "## 포지셔닝 축" in revised
    assert "| 시장 순위/MS |" in revised
    assert "| 성장성 |" in revised
    assert "| 경쟁 압력 |" in revised
    assert "자사 위치:" in revised
    assert "84.93억원" in revised
    assert "3.76%" in revised


def test_threat_detection_contract_adds_factor_direction_basis_table() -> None:
    revised = enforce_answer_contract(
        "리바로의 경쟁 위협 요인은 무엇인가?",
        "경쟁 브랜드를 모니터링해야 합니다.\n\n## 출처\n- 데이터: UBIST",
        {"fact_md": THREAT_FACT_MD},
    )

    assert "## 위협 요인" in revised
    assert "| 위협 요인 | 방향 | 근거 |" in revised
    assert "경쟁 브랜드 점유 확대" in revised
    assert "확대" in revised
    assert "데일리팜" in revised
    assert "https://example.test/threat/1" in revised


def test_news_ei_contract_adds_relevance_grade_without_stealing_change_drivers() -> None:
    revised = enforce_answer_contract(
        "리바로 관련 최근 뉴스와 이슈를 정리해줘",
        "뉴스는 시장 상황을 보여줍니다.\n\n## 출처\n- 데이터: 뉴스",
        {"fact_md": E1_NEWS_FACT_MD},
    )

    assert "## 뉴스 관련성 등급" in revised
    assert "| 관련성 등급 | 기사 | 방향 | 근거 | 처리 상한 |" in revised
    assert "direct" in revised
    assert "market" in revised
    assert "noise" in revised
    assert "입증/확인됨/달성으로 단정하지 않습니다" in revised

    id7_revised = enforce_answer_contract(
        "리바로 목표 시장의 변화 요인을 External/Internal로 분류해줘",
        "시장 변화를 요약합니다.\n\n## 출처\n- 데이터: 뉴스",
        {"fact_md": E1_NEWS_FACT_MD},
    )
    assert "## 변화 요인 결론" in id7_revised
    assert "## 뉴스 관련성 등급" not in id7_revised
