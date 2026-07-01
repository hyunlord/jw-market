from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from jw_chat_agent_poc.portfolio_scope import portfolio_scope_for_question
from jw_chat_agent_poc.agentic import FilterEntry, extract_metric_filter_entries, extract_news_filter_entries


BQ_SYSTEM_PROMPT = """\
JW Market Chat Agent P1 routing map.

Use only this BQ map. Do not invent extra BQ classes before PL validation.

Q1 시장정의·규모:
  Sub-Q: 질환특성/시장정의/성장예측
  Structured tools: IQVIA·UBIST metrics
  Document RAG: Datamonitor·Guideline·Factsheet
  External API: HIRA disease statistics for patient-count/distribution questions
  Deep analysis cache: curated related news/events

Q2 경쟁 차별점:
  Sub-Q: MoA/제형/급여/처방요인/unmet
  Structured tools: UBIST·IQVIA metrics
  Document RAG: Cortellis·Biomeditracker·Guideline
  External API: Nedrug·CT

Q2.5 개발중 경쟁:
  Sub-Q: 발매/MoA/임상단계
  Structured tools: none
  Document RAG: Cortellis·Biomeditracker
  External API: Nedrug·CT

Q3 처방현황:
  Sub-Q: Segment 처방추이
  Structured tools: UBIST
  Document RAG: none
  External API: none

Q4 영업활동:
  Sub-Q: 영업 Impact
  Boundary: 현재 데이터로 답변 불가

Q5 개발타당성:
  Sub-Q: 타겟/허가/급여/임상/포트폴리오/사업성
  Document RAG: Guideline
  External API: MFDS·HIRA·CT
  Boundary: 포트폴리오·사업성 질문은 현재 데이터로 답변 불가
"""


@dataclass(frozen=True)
class BQSubQuestion:
    bq: str
    question: str
    sources: tuple[str, ...]
    reason: str
    filters: tuple[FilterEntry, ...] = ()
    brands: tuple[str, ...] = ()
    scope: str = "single_brand"


class BQRouter:
    system_prompt = BQ_SYSTEM_PROMPT

    def route(self, question: str, has_documents: bool = False) -> list[BQSubQuestion]:
        text = question.lower()
        routes: list[BQSubQuestion] = []

        if any(k in question for k in ("영업활동", "영업 활동", "영업 Impact", "영업 impact")):
            return [
                BQSubQuestion(
                    bq="Q4",
                    question="영업 Impact",
                    sources=("none",),
                    reason="BQ map marks Q4 영업활동 as 데이터 없음.",
                )
            ]

        scope = portfolio_scope_for_question(question)

        if any(k in question for k in ("포트폴리오", "사업성")) and scope != "portfolio":
            return [
                BQSubQuestion(
                    bq="Q5",
                    question="포트폴리오/사업성",
                    sources=("none",),
                    reason="BQ map marks Q5 포트폴리오·사업성 as 데이터 없음.",
                )
            ]

        if self._is_news_question(question):
            routes.append(
                BQSubQuestion(
                    bq="Q1",
                    question="관련 뉴스·소식·이슈",
                    sources=("deep_analysis_events",),
                    reason="Related news questions use curated cache_deep_analysis.data.events.",
                    filters=extract_news_filter_entries(question),
                )
            )

        lower = question.lower()
        metric_filters = extract_metric_filter_entries(question)
        if any(
            k in question
            for k in (
                "환자수",
                "환자 수",
                "환자통계",
                "환자 통계",
                "환자분포",
                "환자 분포",
                "관련 질병",
                "관련 질환",
                "질병통계",
                "질병 통계",
                "질환통계",
                "질환 통계",
                "질병 환자",
                "질환 환자",
            )
        ):
            routes.append(
                BQSubQuestion(
                    bq="Q1",
                    question="질환특성·환자수·질병통계",
                    sources=("external_api",),
                    reason="Q1 disease patient/distribution questions use HIRA disease statistics, not internal sales metrics.",
                )
            )

        if any(k in question for k in ("시장 규모", "시장규모", "성장", "성장 추이", "전망", "매출", "판매", "시계열", "월별", "모멘텀")) or any(
            k in lower for k in ("hhi", "momentum", "monthly", "ei")
        ):
            sources = ["metrics"]
            if has_documents or "업로드" in question:
                sources.append("document")
            routes.append(
                BQSubQuestion(
                    bq="Q1",
                    question="시장정의·규모·성장예측",
                    sources=tuple(sources),
                    reason="Q1 maps market size/growth to IQVIA·UBIST metrics and optional documents.",
                    filters=metric_filters,
                    scope=scope,
                )
            )

        if (
            scope == "portfolio"
            or any(k in question for k in ("경쟁", "차별", "점유율", "처방요인", "상위", "위협", "unmet"))
            or any(k in lower for k in ("ms", "m/s"))
        ):
            sources = ["metrics"]
            if has_documents:
                sources.append("document")
            routes.append(
                BQSubQuestion(
                    bq="Q2",
                    question="경쟁 차별점·점유율·처방요인",
                    sources=tuple(sources),
                    reason="Q2 maps competitive context to metrics plus optional RAG/external sources.",
                    filters=metric_filters,
                    scope=scope,
                )
            )

        if any(k in text for k in ("임상", "clinical", "ct", "개발중", "개발 중")):
            routes.append(
                BQSubQuestion(
                    bq="Q2.5",
                    question="개발중 경쟁·임상단계",
                    sources=("external_api",),
                    reason="Q2.5 maps pipeline/clinical questions to CT/Nedrug APIs.",
                )
            )

        if any(k in text for k in ("fda", "라벨", "label", "특허", "patent", "orange")):
            routes.append(
                BQSubQuestion(
                    bq="Q2",
                    question="급여·라벨·특허 기반 경쟁 차별점",
                    sources=("resolver", "external_api"),
                    reason="Q2 uses Nedrug/CT-class external APIs; P1 includes label and patent wrappers.",
                )
            )

        if any(k in question for k in ("처방", "segment", "세그먼트")):
            routes.append(
                BQSubQuestion(
                    bq="Q3",
                    question="Segment 처방추이",
                    sources=("metrics",),
                    reason="Q3 maps prescription trend to UBIST metrics.",
                    filters=metric_filters,
                )
            )

        if has_documents or "가이드라인" in question or "업로드" in question:
            routes.append(
                BQSubQuestion(
                    bq="Q1/Q5",
                    question="업로드 문서 근거 검색",
                    sources=("document",),
                    reason="BQ map allows Datamonitor/Guideline/Factsheet style documents.",
                )
            )

        return self._dedupe(routes) or [
            BQSubQuestion(
                bq="UNKNOWN",
                question=question,
                sources=("none",),
                reason="No provided BQ map pattern matched.",
            )
        ]

    @staticmethod
    def _dedupe(routes: Iterable[BQSubQuestion]) -> list[BQSubQuestion]:
        seen: set[tuple[str, str, tuple[str, ...], tuple[FilterEntry, ...], tuple[str, ...], str]] = set()
        out: list[BQSubQuestion] = []
        for route in routes:
            key = (route.bq, route.question, route.sources, route.filters, route.brands, route.scope)
            if key in seen:
                continue
            seen.add(key)
            out.append(route)
        return out

    @staticmethod
    def _is_news_question(question: str) -> bool:
        if any(token in question for token in ("뉴스", "소식", "이슈")):
            return True
        if any(token in question for token in ("최근 동향", "관련 동향")):
            metric_or_api_tokens = (
                "매출",
                "시장",
                "점유율",
                "환자",
                "임상",
                "특허",
                "라벨",
                "fda",
                "hhi",
            )
            return not any(token in question.lower() for token in metric_or_api_tokens)
        return False
