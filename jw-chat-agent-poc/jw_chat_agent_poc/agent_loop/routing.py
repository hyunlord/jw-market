from __future__ import annotations

from jw_chat_agent_poc.agent_loop.population_specs import strict_query_plan


_METRIC_TOKENS = ("매출", "점유율", "순위", "시장", "경쟁사", "경쟁", "상위", "위협")
_EXTERNAL_TOKENS = ("뉴스", "이슈", "HIRA", "환자", "질병", "질환", "임상", "특허", "라벨", "FDA")
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
)


def should_use_agent_loop(question: str) -> bool:
    if strict_query_plan(question, "리바로") is not None:
        return True
    if not any(token in question for token in (*_METRIC_TOKENS, *_EXTERNAL_TOKENS)):
        return False
    if _issue_question_needs_quant_context(question):
        return True
    if _patient_sales_question(question):
        return True
    if any(token in question for token in ("점유율", "순위")) and not _segment_metric_question(question):
        return True
    if any(token in question for token in _COMPLEX_TOKENS):
        return True
    return False


def _issue_question_needs_quant_context(question: str) -> bool:
    return any(token in question for token in ("최근 이슈", "관련 이슈", "이슈 뭐", "이슈 알려"))


def _patient_sales_question(question: str) -> bool:
    return "매출" in question and any(token in question for token in ("환자", "환자수", "환자 수", "질병", "질환", "HIRA"))


def _segment_metric_question(question: str) -> bool:
    return any(token in question for token in ("제형", "성분", "진료과", "채널", "회사", "오리지널", "제네릭"))
