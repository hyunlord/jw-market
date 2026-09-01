from __future__ import annotations

import re

from jw_chat_agent_poc.portfolio_scope import is_portfolio_decline_question
from jw_chat_agent_poc.agent_loop.population_specs import strict_query_plan
from jw_chat_agent_poc.orchestrator.answer_completeness import completeness_intent


_CSD_ACTIVITY_TOKENS = ("영업활동", "영업 활동", "상기 콜", "콜 수", "콜수", "활동량")
_DRUG_INFO_TOKENS = ("허가", "품목", "식약처", "MFDS", "의약품정보", "의약품 정보")
_SHARE_OR_RANK_RE = re.compile(r"(?:점유율|순위|(?<![A-Za-z])M\s*/?\s*S(?![A-Za-z]))", re.IGNORECASE)
_TOP_N_RE = re.compile(
    r"(?:상위\s*\d+(?!\d)(?!\.\d)|(?<![A-Za-z0-9_])top\s*\d+(?!\d)(?!\.\d))",
    re.IGNORECASE,
)
_PERCENT_SUFFIX_RE = re.compile(r"^\s*(?:%|퍼센트\b|percent\b)", re.IGNORECASE)
_DOSAGE_UNIT_SUFFIX_RE = re.compile(
    r"^\s*(?:[-–—]\s*)?(?:kg|mg|g|ng|pg|mcg|ug|μg|µg|ml|mL|l|L|iu|IU)\b",
)
_DOSAGE_CONTEXT_SUFFIX_RE = re.compile(
    r"^\s*(?:(?:/|per\s+)\s*(?:kg|g|ml|l|day|d|hour|hr|h|일|시간)\s*){0,2}"
    r"(?:용량|투여량?|복용량?|dose\b|dosage\b)",
    re.IGNORECASE,
)
_QUALITATIVE_RANK_RE = re.compile(r"(?:상위(?!\s*\d)|제일\s*(?:큰|높은)|가장\s*(?:큰|높은))", re.IGNORECASE)
_DERIVED_METRIC_RE = re.compile(
    r"(?:집중도?|분산|HHI|CR\s*\d+|momentum|모멘텀|(?<![A-Za-z])EI(?![A-Za-z])|CAGR)",
    re.IGNORECASE,
)
_ANALYTIC_CHANGE_RE = re.compile(r"(?:대비|변화|비교|차이|하락|떨어|감소|상승|증가|위협|오르(?:는|고|세))")
_COORDINATION_RE = re.compile(r"(?:같이|한번에|함께|동시에|동안)")
_SALES_RE = re.compile(r"(?:매출|판매|실적|시계열|추이)")
_MARKET_RE = re.compile(r"(?:시장|경쟁|구도)")
_NEWS_RE = re.compile(r"(?:뉴스|이슈|소식|정책|약가)")
_CLINICAL_RE = re.compile(r"(?:임상|clinical|연구)", re.IGNORECASE)
_PATENT_RE = re.compile(r"(?:특허|독점권|patent|Orange)", re.IGNORECASE)
_HIRA_RE = re.compile(r"(?:HIRA|환자|질병|질환|진료행위|행위코드|수가코드)", re.IGNORECASE)
_PHARMA_DOMAIN_RE = re.compile(r"(?:브랜드|제품|의약품|약물|성분|제형|ATC|처방)", re.IGNORECASE)
_TOP_N_DOMAIN_RE = re.compile(r"(?:의약품|약물|성분|제형|ATC|처방)", re.IGNORECASE)


def is_top_n_intent(question: str) -> bool:
    for match in _TOP_N_RE.finditer(question):
        suffix = question[match.end() :]
        if _PERCENT_SUFFIX_RE.match(suffix):
            continue
        unit = _DOSAGE_UNIT_SUFFIX_RE.match(suffix)
        if unit is not None and _DOSAGE_CONTEXT_SUFFIX_RE.match(suffix[unit.end() :]):
            continue
        return True
    return False


def should_use_agent_loop(question: str, *, has_brand_anchor: bool = False) -> bool:
    if is_portfolio_decline_question(question):
        return True
    if completeness_intent(question) == "brand_compare":
        return True
    if strict_query_plan(question, "") is not None:
        return True
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
    if _SHARE_OR_RANK_RE.search(question) and not _segment_metric_question(question):
        return True
    if is_top_n_intent(question) and (has_brand_anchor or _TOP_N_DOMAIN_RE.search(question)):
        return True
    if _DERIVED_METRIC_RE.search(question):
        return True
    if (_QUALITATIVE_RANK_RE.search(question) or _ANALYTIC_CHANGE_RE.search(question)) and _has_analytic_domain_signal(question):
        return True
    if _coordinated_multi_intent(question):
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
    return any(
        token in question
        for token in (
            "제형",
            "성분",
            "진료과",
            "채널",
            "회사",
            "오리지널",
            "제네릭",
            "상급종합병원",
            "상급종병",
            "종합병원",
            "종병",
            "병원",
            "의원",
            "약국",
            "원내",
            "원외",
            "보건소",
        )
    )


def _coordinated_multi_intent(question: str) -> bool:
    if not _COORDINATION_RE.search(question):
        return False
    groups = (
        bool(_SALES_RE.search(question)),
        bool(_SHARE_OR_RANK_RE.search(question)),
        bool(_MARKET_RE.search(question)),
        bool(_DERIVED_METRIC_RE.search(question)),
        bool(_NEWS_RE.search(question)),
        bool(_CLINICAL_RE.search(question)),
        bool(_PATENT_RE.search(question)),
        bool(_HIRA_RE.search(question)),
        any(token in question for token in _DRUG_INFO_TOKENS),
        any(token in question for token in _CSD_ACTIVITY_TOKENS),
    )
    return sum(groups) >= 2


def _has_analytic_domain_signal(question: str) -> bool:
    return any(
        pattern.search(question)
        for pattern in (
            _SALES_RE,
            _SHARE_OR_RANK_RE,
            _MARKET_RE,
            _DERIVED_METRIC_RE,
            _NEWS_RE,
            _CLINICAL_RE,
            _PATENT_RE,
            _HIRA_RE,
            _PHARMA_DOMAIN_RE,
        )
    ) or any(token in question for token in (*_DRUG_INFO_TOKENS, *_CSD_ACTIVITY_TOKENS))
