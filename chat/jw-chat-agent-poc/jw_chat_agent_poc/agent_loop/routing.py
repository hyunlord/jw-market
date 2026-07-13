from __future__ import annotations

from jw_chat_agent_poc.portfolio_scope import is_portfolio_decline_question
from jw_chat_agent_poc.agent_loop.population_specs import strict_query_plan


_CSD_ACTIVITY_TOKENS = ("영업활동", "영업 활동", "상기 콜", "콜 수", "콜수", "활동량")
_METRIC_TOKENS = (
    "매출",
    "점유율",
    "순위",
    "시장",
    "집중",
    "경쟁사",
    "경쟁",
    "상위",
    "위협",
    *_CSD_ACTIVITY_TOKENS,
)
_EXTERNAL_TOKENS = (
    "뉴스",
    "이슈",
    "HIRA",
    "환자",
    "질병",
    "질환",
    "임상",
    "특허",
    "라벨",
    "FDA",
    "진료행위",
    "행위코드",
    "수가코드",
    "입원외래",
    "기관종별",
    "요양기관종별",
    "디테일링",
    "상기되는",
    "KOL",
    "시장동향",
    "웹검색",
    "웹 검색",
    "검색해줘",
    "검색 결과",
    "최신 동향",
    "최근 동향",
)
_DRUG_INFO_TOKENS = ("허가", "품목", "식약처", "MFDS", "의약품정보", "의약품 정보")
_COMPLEX_TOKENS = (
    "같은 시장에서",
    "제일 큰",
    "가장 큰",
    "대비",
    "변화",
    "비교",
    "같이",
    "한번에",
    "함께",
    "하락",
    "떨어",
    "감소",
    "줄",
    "위협",
    "오르는",
    "동안",
    "상위",
    "집중",
    "분산",
)


def should_use_agent_loop(question: str) -> bool:
    if is_portfolio_decline_question(question):
        return True
    if strict_query_plan(question, "리바로") is not None:
        return True
    if not any(token in question for token in (*_METRIC_TOKENS, *_EXTERNAL_TOKENS, *_DRUG_INFO_TOKENS)):
        return False
    if any(token in question for token in _DRUG_INFO_TOKENS):
        return True
    if _external_question_needs_agent_loop(question):
        return True
    if _issue_question_needs_quant_context(question):
        return True
    if _news_sales_impact_question(question):
        return True
    if _patient_sales_question(question):
        return True
    if any(token in question for token in _CSD_ACTIVITY_TOKENS):
        return True
    if any(token in question for token in ("점유율", "순위")) and not _segment_metric_question(question):
        return True
    if any(token in question for token in _COMPLEX_TOKENS):
        return True
    return False


def _issue_question_needs_quant_context(question: str) -> bool:
    return any(token in question for token in ("최근 이슈", "관련 이슈", "이슈 뭐", "이슈 알려"))


def _news_sales_impact_question(question: str) -> bool:
    return (
        "매출" in question
        and any(token in question for token in ("뉴스", "이슈"))
        and any(token in question for token in ("영향", "원인", "왜"))
    )


def _patient_sales_question(question: str) -> bool:
    return "매출" in question and any(token in question for token in ("환자", "환자수", "환자 수", "질병", "질환", "HIRA"))


def _external_question_needs_agent_loop(question: str) -> bool:
    return any(
        token in question
        for token in (
            "진료행위",
            "행위코드",
            "수가코드",
            "입원외래",
            "기관종별",
            "요양기관종별",
            "디테일링",
            "상기되는",
            "KOL",
            "시장동향",
            "웹검색",
            "웹 검색",
            "검색해줘",
            "검색 결과",
            "최신 동향",
            "최근 동향",
        )
    )


def _segment_metric_question(question: str) -> bool:
    return any(token in question for token in ("제형", "성분", "진료과", "채널", "회사", "오리지널", "제네릭"))
