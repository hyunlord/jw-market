from __future__ import annotations

from jw_chat_agent_poc.service.answer_safety import ensure_judgment_insight, mandatory_retry_messages, missing_mandatory_lines
from jw_chat_agent_poc.service.genos_client import GenosClient


def test_final_answer_prompt_requires_evidence_based_causal_analysis() -> None:
    """Given judgment questions, When prompting Flash, Then causal analysis is required from facts."""

    # Given / When
    messages = GenosClient._markdown_messages(
        "리바로 2월 하락이 시장 영향인지 브랜드 고유인지 봐줘",
        {
            "fact_md": (
                "- 필수 답변 fact: 리바로 Jan-Feb sales pct change=-9.58%, "
                "market Jan-Feb sales pct change=-9.12%, gap=-0.46%p"
            )
        },
    )
    system_prompt = messages[0]["content"]
    user_prompt = messages[1]["content"]

    # Then
    assert "결론" in system_prompt
    assert "해석" in system_prompt
    assert "시사점" in system_prompt
    assert "근거 기반 인과 분석" in system_prompt
    assert "왜 이런가" in system_prompt
    assert "무엇을 시사하는가" in system_prompt
    assert "거짓 수치" in system_prompt
    assert "존재하지 않는 기사" in system_prompt
    assert "원인·배경·작용·때문 같은 인과 표현을 쓰지 않는다" not in system_prompt
    assert "숫자, 비율, 순위, 기간, 질병코드는 fact set에 있는 값만" in system_prompt
    assert "결론" in user_prompt
    assert "채점 근거" not in user_prompt
    assert "★" not in system_prompt


def test_final_answer_prompt_requires_news_context_synthesis() -> None:
    """Given news facts, When prompting Flash, Then news is treated as context evidence."""

    messages = GenosClient._markdown_messages(
        "리바로 경쟁 구도 변화는 어때",
        {
            "fact_md": (
                "### 인사이트 근거 fact - 뉴스/이슈\n"
                "| 날짜 | 제목 | 출처 | 요약 | 매칭 발췌 |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| 2026-04-12 | 리바로젯 경쟁 기사 | 약업신문 | 복합제 경쟁 맥락 | 리바로젯과 아토젯 |\n"
            )
        },
    )
    system_prompt = messages[0]["content"]

    assert "뉴스 fact는 사업 이슈 맥락" in system_prompt
    assert "실제 기사 제목·날짜·출처·URL·요약 내용을 드러낸다" in system_prompt
    assert "기사 제목·날짜·출처·URL·요약" in system_prompt
    assert "존재만 표시하는 빈 문장" in system_prompt


def test_final_answer_prompt_prioritizes_direct_metric_questions() -> None:
    """Given direct metric questions, When prompting Flash, Then requested values come first."""

    # Given / When
    messages = GenosClient._markdown_messages(
        "리바로 점유율이랑 순위 알려줘",
        {
            "fact_md": (
                "### 필수 답변 fact\n"
                "| 구분 | 반드시 반영할 내용 |\n"
                "| --- | --- |\n"
                "| 브랜드 핵심 지표 | 리바로 2026-04 매출 84.93억원 시장점유율 3.76% 순위 6위 |"
            )
        },
    )
    system_prompt = messages[0]["content"]
    user_prompt = messages[1]["content"]

    # Then
    assert "특정 지표를 직접 물으면" in system_prompt
    assert "이슈 맥락보다 먼저" in system_prompt
    assert "브랜드명·기간·값·순위" in system_prompt
    assert "브랜드명·기간·값" in user_prompt


def test_final_answer_prompt_requires_prose_first_conversation() -> None:
    """Given verified facts, When prompting Flash, Then prose leads and tables only support it."""

    messages = GenosClient._markdown_messages(
        "리바로 경쟁구도 어떻게 변하고 있어",
        {
            "fact_md": (
                "### 필수 답변 fact\n"
                "| 구분 | 반드시 반영할 내용 |\n"
                "| --- | --- |\n"
                "| 경쟁 현황 | 로수젯 시장점유율 9.13% |"
            )
        },
    )
    system_prompt = messages[0]["content"]
    user_prompt = messages[1]["content"]

    assert "사람에게 설명하듯 자연스러운 문단" in system_prompt
    assert "핵심을 한두 문장으로 먼저" in system_prompt
    assert "값이나 표를 먼저 나열하지 않는다" in system_prompt
    assert "기존 표·차트·뉴스·출처를 삭제하거나 축소하지 않는다" in system_prompt
    assert "표·차트·뉴스는 자연어 본문 뒤에 근거 자료로 그대로 유지" in system_prompt
    assert "확정 fact에 없는 수치·사실·원인은 덧붙이지 않는다" in system_prompt
    assert "자연어 문단을 먼저" in user_prompt
    assert "기존 표·차트·뉴스는 그대로 유지" in user_prompt
    assert "표는 꼭 필요한 경우에만" not in system_prompt


def test_final_answer_prompt_sends_each_verified_fact_section_once() -> None:
    """Given repeated fact sections, When prompting, Then redundant context is omitted."""

    # Given
    mandatory_row = "| 경쟁 현황 | 로수젯 시장점유율 9.13% |"
    repeated_section = (
        "### 상위 브랜드 월별 MS fact\n"
        "| 브랜드 | 월별 MS |\n"
        "| --- | --- |\n"
        "| 로수젯 | 2026-04 9.14% → 2026-05 9.13% |"
    )
    fact_md = (
        "### 필수 답변 fact\n"
        "| 구분 | 반드시 반영할 내용 |\n"
        "| --- | --- |\n"
        f"{mandatory_row}\n\n"
        f"{repeated_section}\n\n"
        f"{repeated_section}"
    )

    # When
    user_prompt = GenosClient._markdown_messages(
        "리바로 경쟁구도 어떻게 변하고 있어",
        {"fact_md": fact_md},
    )[1]["content"]

    # Then
    assert user_prompt.count("로수젯 시장점유율 9.13%") == 1
    assert "### 필수 답변 fact" not in user_prompt
    assert user_prompt.count("### 상위 브랜드 월별 MS fact") == 1


def test_clinical_only_prompt_omits_unrelated_market_instructions() -> None:
    """Given only clinical facts, When prompting, Then market-only rules are omitted."""

    messages = GenosClient._markdown_messages(
        "리바로 임상시험",
        {
            "fact_md": (
                "### 임상시험 fact\n"
                "| 출처 | 시험/식별자 | 제목/제품 | 상태 | 단계 |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| ClinicalTrials.gov | NCT07626840 | 리바로젯 비교 연구 | RECRUITING | Phase 4 |"
            )
        },
    )
    system_prompt = messages[0]["content"]
    user_prompt = messages[1]["content"]

    assert "임상시험 등록정보" in system_prompt
    assert "NCT 식별자" in system_prompt
    assert "사람에게 설명하듯" in system_prompt
    assert "새 수치·사실·원인" in system_prompt
    assert "share-of-growth" not in system_prompt
    assert "상위 브랜드 월별 MS" not in system_prompt
    assert "업로드 파일" not in system_prompt
    assert "NCT07626840" in user_prompt


def test_mandatory_retry_prompt_preserves_insight_and_fact_boundaries() -> None:
    """Given missing required facts, When retrying, Then the retry prompt still asks for causal insight."""

    # Given / When
    messages = mandatory_retry_messages(
        "아토젯이 리바로를 위협하는지 봐줘",
        "- 필수 답변 fact: 아토젯 share trend 상승, 리바로 share trend 보합",
        "아토젯 데이터는 확인되지 않습니다.",
        ("아토젯 share trend 상승",),
    )
    system_prompt = messages[0]["content"]

    # Then
    assert "결론" in system_prompt
    assert "해석" in system_prompt
    assert "시사점" in system_prompt
    assert "근거 기반 인과 분석" in system_prompt
    assert "거짓 수치" in system_prompt
    assert "원인·배경·작용·때문 같은 인과 표현을 쓰지 않는다" not in system_prompt
    assert "숫자는 fact set에 있는 값만" in system_prompt


def test_judgment_mandatory_facts_accept_integrated_prose() -> None:
    """Given judgment facts in prose, When checking mandatory coverage, Then raw fact lines are not appended."""

    # Given
    lines = (
        "- 시장/브랜드 변화율 대조: 리바로 2026-01→2026-02 브랜드 매출 83.03억원 → 75.08억원 브랜드 변화율 -9.58% 시장 매출 2,177.00억원 → 1,978.43억원 시장 변화율 -9.12% 변화율 차이 -0.46%p 근거 기반 인과 분석: 시장 동반 하락이 주요 배경으로 해석됨",
        "- 브랜드 추세 비교: 리바로 vs 아토젯 2025-07→2026-04 리바로 MS 3.92% → 3.76% 리바로 MS 변화 -0.16%p 아토젯 MS 5.17% → 5.16% 아토젯 MS 변화 -0.01%p 리바로 매출 변화율 0.20% 아토젯 매출 변화율 4.21% 근거 기반 인과 분석: 아토젯이 매출 성장 측면에서 리바로보다 강한 위협 신호",
    )
    answer = (
        "리바로 2026-01→2026-02 매출은 83.03억원에서 75.08억원으로 변했고, "
        "브랜드 변화율 -9.58%는 시장 변화율 -9.12%와 유사해 차이 -0.46%p 수준입니다. "
        "이는 시장 동반 하락이 리바로 매출 하락의 주요 배경이라는 해석을 뒷받침합니다.\n\n"
        "아토젯은 리바로와 비교할 때 2025-07→2026-04 점유율 변화가 -0.01%p로, "
        "리바로 -0.16%p보다 방어적입니다. 리바로 매출 변화율 0.20%, "
        "아토젯 매출 변화율 4.21%를 함께 보면 추세상 위협 신호가 있습니다."
    )

    # When / Then
    assert missing_mandatory_lines(answer, lines) == ()


def test_judgment_insight_replaces_raw_fact_echo_and_empty_table() -> None:
    """Given judgment answers that collapse to tables, When safety runs, Then insight is restored."""

    # Given
    fact_md = """### 필수 답변 fact
| 구분 | 값 |
| --- | --- |
| 시장/브랜드 변화율 대조 | 리바로 2026-01→2026-02 브랜드 매출 83.03억원 → 75.08억원 브랜드 변화율 -9.58% 시장 매출 2,177.00억원 → 1,978.43억원 시장 변화율 -9.12% 변화율 차이 -0.46%p 근거 기반 인과 분석: 시장 동반 하락이 주요 배경으로 해석됨 |
| 브랜드 추세 비교 | 리바로 vs 아토젯 2025-07→2026-04 리바로 MS 3.92% → 3.76% 리바로 MS 변화 -0.16%p 아토젯 MS 5.17% → 5.16% 아토젯 MS 변화 -0.01%p 리바로 매출 변화율 0.20% 아토젯 매출 변화율 4.21% 근거 기반 인과 분석: 아토젯이 매출 성장 측면에서 리바로보다 강한 위협 신호 |
"""
    table_only = """| 구분 | 2026-01 매출 | 2026-02 매출 | 변화율 |
| --- | --- | --- | --- |
| 리바로 | 83.03억원 | 75.08억원 | -9.58% |
| 시장 전체 | 2,177.00억원 | 1,978.43억원 | -9.12% |

출처: UBIST

- 시장/브랜드 변화율 대조: 리바로 2026-01→2026-02 브랜드 매출 83.03억원 → 75.08억원 브랜드 변화율 -9.58% 시장 매출 2,177.00억원 → 1,978.43억원 시장 변화율 -9.12% 변화율 차이 -0.46%p 근거 기반 인과 분석: 시장 동반 하락이 주요 배경으로 해석됨"""
    empty_table = """결론적으로 아토젯은 리바로보다 우위입니다.

| 브랜드 | 매출 | 시장점유율(MS) | 순위 |
| --- | --- | --- | --- |

출처: UBIST"""

    # When
    market_answer = ensure_judgment_insight("리바로 2월 하락이 시장 영향인지 봐줘", table_only, fact_md)
    threat_answer = ensure_judgment_insight("아토젯이 리바로를 위협하고 있어?", empty_table, fact_md)

    # Then
    assert "결론:" in market_answer
    assert "시장 전체와 동행" in market_answer
    assert "시장 동반 하락" in market_answer
    assert "시장/브랜드 변화율 대조" not in market_answer
    assert "결론:" in threat_answer
    assert "4.21%" in threat_answer
    assert "0.20%" in threat_answer
    assert "| 브랜드 | 매출 |" not in threat_answer
