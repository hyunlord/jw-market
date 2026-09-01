from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from jw_chat_agent_poc.service.conversation import ConversationTurn
from jw_chat_agent_poc.service.v4.comparison_facts import build_comparison_facts
from jw_chat_agent_poc.service.v4.context_whitelist import project_recent_turns
from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.document_lane import (
    canonical_file_lane,
    document_record_lane,
)
from jw_chat_agent_poc.service.v4.fact_digest import (
    FactDigest,
    is_document_summary_request,
)
from jw_chat_agent_poc.service.v4.gates import (
    hira_row_axis_label,
    inspect_requested_hira_surface,
    render_mart_dimension_facts,
)
from jw_chat_agent_poc.service.v4.insight_claims import evidence_catalog_payload
from jw_chat_agent_poc.service.v4.insight_contract import (
    insight_expansion_metrics,
    replace_s17_insight_section,
    sanitize_s17_insight,
    unused_fact_digest_materials,
)
from jw_chat_agent_poc.service.v4.inspection import _raw_records, _surfaced_record_count
from jw_chat_agent_poc.service.v4.llm import (
    CompletionResult,
    CompletionTransportError,
    GenOSV4Client,
    normalize_reasoning_effort,
    thinking_observability,
)
from jw_chat_agent_poc.service.v4.reason_code_enforcement import typed_absence_record
from jw_chat_agent_poc.service.v4.session_state import SessionState
from jw_chat_agent_poc.service.v4.source_labels import (
    SOURCE_LABELS as _PUBLIC_SOURCE,
)
from jw_chat_agent_poc.service.v4.surface_notices import append_automatic_fact_notices
from jw_chat_agent_poc.service.v4.synthesis_policy import (
    SynthesisPolicy,
    bound_synthesis_messages,
)
from jw_chat_agent_poc.service.v4.time_context import (
    as_of_date_instruction,
)
from jw_chat_agent_poc.service.v4.time_context import (
    current_kst_date as _current_kst_date,
)

LOGGER = logging.getLogger(__name__)
_INSIGHT_LANE_TEMPERATURE = 0.0
_REASONING_EFFORT_UNSET = object()

_INTERNAL_SURFACE_RE = re.compile(
    r"(?i:MCP(?:[^가-힣\n]{0,80})?(?:에서|returned|결과)|\btotalCount\b|"
    r"\bslot[_ -]?id\b|\b(?:sickCd|ptntCnt|value)\b|"
    r"\b\d{7,}(?:\.\d+)?\s*KRW(?![A-Za-z])|"
    r"\b\d{7,}(?:\.\d+)?\s*(?:\(\s*원\s*\)|원)(?:은|는|이|가|을|를|으로|에서|의)?|"
    r"\b(?:hira|clinicaltrials|mfds|openfda|tavily)_[a-z0-9_]+\b|"
    r"(?:\bNCT\d{8}\b\s*[,/]\s*)+\bNCT\d{8}\b)|"
    r"\b[A-Z][A-Z0-9_]{2,}\s*[:=]\s*[^\s,;]+",
)
_RETRYABLE_INTERNAL_RE = re.compile(
    r"(?i:MCP(?:[^가-힣\n]{0,80})?(?:에서|returned|결과)|\btotalCount\b|"
    r"\bslot[_ -]?id\b|\b(?:sickCd|ptntCnt|value)\b|"
    r"\b(?:hira|clinicaltrials|mfds|openfda|tavily)_[a-z0-9_]+\b|"
    r"(?:\bNCT\d{8}\b\s*[,/]\s*)+\bNCT\d{8}\b)|"
    r"\b[A-Z][A-Z0-9_]{2,}\s*[:=]\s*[^\s,;]+",
)
_INTERNAL_ASSIGNMENT_RE = re.compile(
    r"(?i:\b(?:totalCount|slot[_ -]?id|sickCd|ptntCnt|value)\b|\baux:[a-z0-9_:-]+\b)"
    r"(?:\s*[:=]\s*[^\s,;.]+)?"
)
_SOURCE_SCOPE = {
    "mart": "KR",
    "nedrug": "KR",
    "hira": "KR",
    "openfda": "US",
    "clinicaltrials": "GLOBAL",
    "web": "GLOBAL",
    "patent": "GLOBAL",
    "document": "KR",
    "prior_turn": "CONVERSATION",
}
_HIRA_FIELD_LABELS = {
    "sickCd": "상병코드",
    "ptntCnt": "환자수(명)",
    "specCnt": "명세서건수(건)",
    "vstDdcnt": "방문일수(일)",
    "rvdInsupBrdnAmt": "보험자부담금(원)",
    "rvdRpeTamtAmt": "요양급여비용총액(원)",
}
_SYNTHESIS_SYSTEM_PROMPT = (
    "너는 JW MI팀의 CHAT-V4 답변 합성기다. 질문이 묻는 값이나 내용을 첫 문단에서 직접 답하고, "
    "직접 관련 없는 근거는 뒤로 보내거나 생략한다. 도구 로그를 나열하지 말고 근거를 연결한 자연스러운 "
    "한국어 줄글로 작성한다. 사실은 '~로 확인되었습니다' 또는 '~입니다'로 쓴다. 출처 표기는 코드가 "
    "근거 식별자에서 렌더하므로 [출처] 문자열을 직접 생성하지 않는다. 해석은 '~로 해석될 수 있습니다' 또는 '~할 것으로 추정됩니다'로 구분하며 근거에 없는 숫자를 "
    "만들지 않는다. 못 찾은 부분만 마지막 한 줄에 적는다. 내부 도구명, MCP 상태 문구, totalCount, slot id, "
    "식별자 목록과 대문자 레코드 필드명을 노출하지 않는다. <INTERNAL_DATAMART> 안의 숫자와 표기는 한 글자도 바꾸지 않는다. "
    "<INTERNAL_DEEP_ANALYSIS>는 사전 생성된 내부 분석이며 freshness_label을 출처명에 그대로 붙여 실시간 데이터마트와 시점을 구분한다. "
    "단위 환산, 반올림, 계산, 합산을 금지하며 UBIST와 IQVIA를 합산하지 않는다. "
    "MISMATCH 근거는 이미 제외됐고 PARTIAL, US, 기간 불일치는 한계를 본문에 명시한다. "
    "질문이 매출·점유율·순위·판매량·시장 규모·추이를 직접 물으면 내부 데이터마트를 핵심 답으로 쓴다. "
    "질문이 원인을 물으면 동적 cause_answer_contract의 세 층과 귀속 제한을 따르고, 첫 층은 "
    "`관측`으로 명확히 구분한다. "
    "허가·급여·임상·안전성·특허처럼 외부 주제를 물으면 해당 외부 근거가 핵심 답이며, 상시 제공된 내부 "
    "데이터마트는 종합 인사이트나 참고에만 둔다. 내부 데이터마트가 요청 주제를 대체하거나 첫 문단을 빼앗지 않는다. "
    "evidence.eligible_claims에 없는 주장에는 그 근거를 사용하지 않는다. 관찰연구, 기기 연구, 인접 질환, "
    "질문과 다른 모집상태의 연구는 핵심 답이 아니라 `참고: 인접 연구` 구획에만 둔다. causal=false 근거만으로 "
    "원인을 확인했다고 쓰지 말고 관찰 사실과 가설을 분리한다. HIRA 입원과 외래 환자수는 중복 가능하므로 "
    "LLM이 합산하거나 비율을 새로 계산하지 않는다. 다만 DerivedCoreCard에 코드가 사전 계산해 등록한 "
    "입원·외래 관계값은 그 카드의 수치 그대로 인용할 수 있다. study_classification에서 ADJACENT로 표시된 임상은 `참고: 인접 연구` "
    "구획에만 두고 인접 연구를 종합 인사이트에서 다시 요약하거나 해석하지 않는다. HIRA 환자수는 `환자수(명)` "
    "값만 사용한다. `명세서건수(건)`이나 `방문일수(일)`를 환자수로 바꾸어 쓰지 않고, 금액은 `(원)` 라벨이 "
    "붙은 값과 단위를 그대로 쓴다. 질문에 대한 답을 첫 문장에서 바로 제시한다. 출처별 소제목이나 "
    "고정된 섹션 수를 강제하지 말고, 질문의 논리에 맞는 소제목만 한 번씩 사용한다. 확인되지 않은 내용이 "
    "한계와 주의는 본문이 아니라 조회 제한 구역에만 둔다. 내용이 없는 소제목은 만들지 않는다. 같은 주어와 기간을 "
    "되풀이하는 선두 문장을 만들지 않는다. 근거 없는 수식어로 분량을 채우지 않는다. "
    "결정론적 사실면 표는 전건 보존용이므로 표의 행을 해설에 다시 나열하지 말고, 표가 뜻하는 맥락과 시사점을 "
    "충분한 길이의 자연스러운 문장으로 연결한다. 결정론적 사실면의 [직접 확인] 레코드 관계는 코드가 "
    "재계산한 주장만 포함하므로 서술에 반영하되, 그 목록에 없는 레코드 간 관계를 새로 만들지 않는다. "
    "핵심 답은 질문에 직접 답하고, 근거와 맥락은 사실 간 관계를, "
    "종합 인사이트는 의사결정상 함의를 설명한다. "
    "핵심 답은 DerivedCoreCard만 사용해 3~5문장으로 작성하고, "
    "표의 값을 전부 나열하지 않되 핵심 답을 표에 위임하지 않는다. 특허 만료 질문은 첫 문장에 "
    "특허번호·상태·만료일을 한 레코드로 결속해 쓴다. 종합 인사이트는 질문 축 심화, 둘 이상의 실제 "
    "자료원을 엮는 교차 융합 순서로 작성한다. FactDigest 재료가 충분하면 3개 문단과 전체 "
    "15~20문장을, 중간 수준이면 3개 문단과 최소 10문장을 작성하고 남은 적격 재료를 소진한다. "
    "L1은 FactDigest의 "
    "구체 값 2개 이상에 결속한 사실, L2는 비교·추세·구조의 의미를 설명하는 해석, L3는 여러 "
    "자료원을 엮은 시장 함의·경쟁 구도·전망·전략적 시사점으로 구성한다. L3는 반드시 2문장 "
    "이상이며 같은 인사이트의 FactDigest 숫자에 정박하고 '~로 해석될 수 있습니다', '추정됩니다', "
    "'가능성이 있습니다' 중 하나로 추론임을 밝힌다. L3 문장마다 구체 값 2개를 반복할 필요는 없지만 "
    "FactDigest에 없는 숫자·날짜·상태는 어느 문장에도 만들지 않는다. "
    "FactDigest의 derived_fields에 코드가 계산해 등록한 평균·비율·증감은 그대로 인용하되 LLM이 "
    "새로 계산하지 않는다. '가늠해 볼 수 있습니다', '추론할 수 있습니다', '의미합니다', "
    "'확인됩니다'처럼 판단을 독자에게 넘기거나 동어반복하는 문장을 만들지 말고 해석 결과를 직접 쓴다. "
    "당신의 가치는 나열이 아니라 통찰이다. L1 사실 위에 반드시 L2 해석과 L3 융합 추론을 얹어라. "
    "추론은 근거 수치를 지목하며 전개하고, 확정 어조 대신 추정 표지를 써라. MI 팀이 회의에서 바로 "
    "인용할 수 있는 수준의 시장 판단을 제시하라. L3가 없으면 '융합 추론 누락' 사유로 한 번만 재합성한다. "
    "면책·주의·확인 한계 문장과 '또한,'으로 시작하는 잘린 문단을 만들지 않는다. "
    "종합 인사이트는 사실면과 분리된 절에 두고, 해석마다 근거가 된 사실 구획을 이름으로 밝힌다. "
    "근거에 없는 새 수치를 만들지 않으며 단정 대신 가능성 또는 검토할 함의로 표현한다. "
    "검증 가능한 해석 재료가 없으면 종합 인사이트를 통째로 생략하고 대체 문장을 만들지 않는다. "
    "고시·허가사항은 투여대상·제외기준·투여방법·투여횟수처럼 의미 단위 불릿으로 요약한다. 근거 본문은 "
    "활용하되 다운로드 안내문이나 담당부서 연락 안내는 답변에 복사하지 않는다. gap_fill로 표시된 웹 근거는 "
    "공식 통계 표나 시계열에 섞지 말고 별도 문단에서 '공식 통계 아님'을 밝혀 서술한다. TIER1 또는 TIER2가 "
    "아닌 웹 정량값은 쓰지 않는다. 제네릭처럼 하위 제품 집합을 묻는 질문에서는 그 집합이 근거에 없을 때 "
    "본품이나 상위 제품의 수치를 대신 답하지 않고 요청 집합의 값을 확인하지 못했다고 먼저 밝힌다."
    " `required_hira_surface`가 있으면 모든 항목을 첫 합성에서 본문에 정확히 포함한다."
)
_INSIGHT_PROMPT_START = "종합 인사이트는 질문 축 심화"
_INSIGHT_PROMPT_END = (
    "검증 가능한 해석 재료가 없으면 종합 인사이트를 통째로 생략하고 대체 문장을 만들지 않는다. "
)
_CORE_INSIGHT_BRIDGE = (
    "결정론적 사실면 표는 전건 보존용이므로 표의 행을 해설에 다시 나열하지 말고, 표가 뜻하는 맥락과 시사점을 "
    "충분한 길이의 자연스러운 문장으로 연결한다. 결정론적 사실면의 [직접 확인] 레코드 관계는 코드가 "
    "재계산한 주장만 포함하므로 서술에 반영하되, 그 목록에 없는 레코드 간 관계를 새로 만들지 않는다. "
    "핵심 답은 질문에 직접 답하고, 근거와 맥락은 사실 간 관계를, "
    "종합 인사이트는 의사결정상 함의를 설명한다. "
)


def _split_synthesis_system_prompt() -> tuple[str, str]:
    start = _SYNTHESIS_SYSTEM_PROMPT.index(_INSIGHT_PROMPT_START)
    end = _SYNTHESIS_SYSTEM_PROMPT.index(_INSIGHT_PROMPT_END, start) + len(
        _INSIGHT_PROMPT_END
    )
    core_prompt = _SYNTHESIS_SYSTEM_PROMPT[:start] + _SYNTHESIS_SYSTEM_PROMPT[end:]
    core_prompt = core_prompt.replace(_CORE_INSIGHT_BRIDGE, "")
    core_prompt = core_prompt.replace(
        "인접 연구를 종합 인사이트에서 다시 요약하거나 해석하지 않는다.",
        "인접 연구를 핵심 답의 직접 근거로 쓰지 않는다.",
    )
    return (
        core_prompt.replace("종합 인사이트나 참고", "참고"),
        _SYNTHESIS_SYSTEM_PROMPT[start:end],
    )


_CORE_SYNTHESIS_SYSTEM_PROMPT, _MIGRATED_INSIGHT_SYSTEM_PROMPT = (
    _split_synthesis_system_prompt()
)
_CAUSE_MARKERS = ("원인", "왜 ", "이유")
_DEFAULT_SYNTHESIS_MAX_TOKENS = 16384
_MIN_SYNTHESIS_MAX_TOKENS = 8192
_MAX_SYNTHESIS_MAX_TOKENS = 32768
_DEFAULT_S17_REPAIR_MAX_TOKENS = 8192
_MIN_S17_REPAIR_MAX_TOKENS = 2048
_DEFAULT_INSIGHT_LANE_MAX_TOKENS = 28672


@dataclass(frozen=True)
class SynthesisOutcome:
    text: str
    trace: dict[str, Any]


class V4Synthesizer:
    def __init__(self, client: GenOSV4Client) -> None:
        self._client = client

    def synthesize(
        self,
        plan: PlannerOutput,
        results: Sequence[SourceResult],
        turns: Sequence[ConversationTurn],
        *,
        budget_s: float = 60.0,
        state: SessionState | None = None,
        deterministic_facts: str | None = None,
        defer_market_facts: bool = False,
        fact_digest: FactDigest | None = None,
        core_only: bool = False,
    ) -> str:
        return self.synthesize_with_trace(
            plan,
            results,
            turns,
            budget_s=budget_s,
            state=state,
            deterministic_facts=deterministic_facts,
            defer_market_facts=defer_market_facts,
            fact_digest=fact_digest,
            core_only=core_only,
        ).text

    def repair_insight_after_semantic(
        self,
        plan: PlannerOutput,
        answer: str,
        *,
        fact_digest: FactDigest,
        failure_trace: Mapping[str, Any],
        budget_s: float,
    ) -> SynthesisOutcome:
        """Spend the single S17 repair attempt on the observed semantic failure."""
        expansion_contract = insight_expansion_metrics(answer, fact_digest)
        required_sentence_count = max(
            1,
            int(expansion_contract.get("required_sentence_count") or 1),
        )
        maximum_sentence_count = int(
            expansion_contract.get("maximum_sentence_count") or 20
        )
        repair_shape = (
            f"정확히 3개 문단과 {required_sentence_count}~"
            f"{maximum_sentence_count}문장"
            if required_sentence_count >= 10
            else (
                f"가용 적격 근거를 소진한 {required_sentence_count}~"
                f"{maximum_sentence_count}문장"
            )
        )
        instruction = (
            f"{_MIGRATED_INSIGHT_SYSTEM_PROMPT} "
            "post_semantic 검사에서 탈락한 종합 인사이트만 다시 작성하라. "
            "첫 토큰은 `## 종합 인사이트`이고 기존 핵심답, 사고 과정, JSON, 목록은 출력하지 않는다. "
            f"목록 없이 {repair_shape}으로 쓴다. 첫 문단은 2~3문장만 사용해 FactDigest의 "
            "핵심 직접 사실을 결속한다. 둘째 문단은 각 문장마다 FactDigest의 구체 명칭이나 값을 지목해 "
            "비교·추세·구성을 해석하되 각 문장에 숫자·날짜·상태 비교 앵커를 2개 이상 넣고 "
            "해석 결과를 직접 서술한다. 셋째 문단은 앞 문단의 수치 앵커에 정박한 시장·경쟁·전략 "
            "추론 3문장으로 쓰되 각 문장에 의사결정 주체와 전략 선택지를 함께 밝히고 새 숫자는 만들지 않는다. "
            "각 문장은 FactDigest의 구체 값이나 명칭에 결속하고, 전 lane의 서로 다른 사실을 사용한다. "
            "숫자·날짜·상태는 FactDigest 표기를 그대로 복사한다. 새 관찰값이나 새 계산을 만들지 않는다. "
            "문서 청크는 근거로만 읽고 한국어 요약·해석으로 바꾸며 영어 원문이나 `문서 요약 chunks`를 "
            "통째로 붙여 넣지 않는다. 확장 상병코드는 관련 질환군 비교에만 쓰고 질문 질환의 직접 "
            "근거로 승격하지 않는다. unused_fact_digest_materials를 우선 소진하고 같은 숫자 토큰은 "
            "2회 이하, 같은 (대상·필드·값) 사실은 1회만 사용한다."
        )
        messages = _s17_repair_messages(
            plan=plan,
            answer=answer,
            instruction=instruction,
            fact_digest=fact_digest,
            fallback_messages=(),
            extra={
                "repair_stage": "post_semantic",
                "failure_trace": {
                    key: failure_trace.get(key)
                    for key in (
                        "reason_code",
                        "retry_reason",
                        "paragraph_count",
                        "retained_sentence_count",
                        "reject_reason_counts",
                    )
                    if failure_trace.get(key) is not None
                },
            },
            insight_only=True,
        )
        max_tokens = _s17_repair_max_tokens(_synthesis_max_tokens())
        prompt_chars = sum(len(message["content"]) for message in messages)
        try:
            completion = _complete_detailed(
                self._client,
                messages,
                budget_s=budget_s,
                max_tokens=max_tokens,
            )
        except CompletionTransportError as exc:
            recovered, partial_trace = _recover_grounded_partial_insight(
                answer,
                exc.partial.text,
                fact_digest,
            )
            candidate_available = bool(partial_trace.get("recovered"))
            return SynthesisOutcome(
                # A transport failure must not erase the grounded insight that
                # already survived validation. The partial candidate replaces
                # it only when it is strictly richer.
                text=recovered,
                trace={
                    "attempted": True,
                    "candidate_available": candidate_available,
                    "error_type": type(exc).__name__,
                    "prompt_chars": prompt_chars,
                    "max_tokens": max_tokens,
                    "partial_recovery": partial_trace,
                    "thinking": thinking_observability(
                        getattr(self._client, "thinking_level", None),
                        exc.partial.usage,
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001 - omission remains the fail-closed result
            return SynthesisOutcome(
                text="",
                trace={
                    "attempted": True,
                    "candidate_available": False,
                    "error_type": type(exc).__name__,
                    "prompt_chars": prompt_chars,
                    "max_tokens": max_tokens,
                },
            )
        candidate = completion.text.strip()
        candidate_available = bool(candidate)
        return SynthesisOutcome(
            text=candidate if candidate_available else "",
            trace={
                "attempted": True,
                "candidate_available": candidate_available,
                "error_type": None,
                "finish_reason": completion.finish_reason,
                "truncated": completion.finish_reason == "length",
                "elapsed_ms": completion.elapsed_ms,
                "prompt_chars": prompt_chars,
                "max_tokens": max_tokens,
                "candidate_chars": len(candidate),
                "thinking": thinking_observability(
                    getattr(self._client, "thinking_level", None),
                    completion.usage,
                ),
            },
        )

    def generate_structured_insight_claims(
        self,
        plan: PlannerOutput,
        core_answer: str,
        *,
        fact_digest: FactDigest,
        retry_error: str | None,
        budget_s: float,
        section: str | None = None,
    ) -> SynthesisOutcome:
        """Generate the IG-3 claim envelope without touching the core answer."""
        material_floor = _structured_material_floor(fact_digest)
        messages = _structured_insight_messages(
            plan=plan,
            core_answer=core_answer,
            fact_digest=fact_digest,
            retry_error=retry_error,
            target_section=section,
        )
        max_tokens = _insight_lane_max_tokens(section)
        prompt_chars = sum(len(message["content"]) for message in messages)
        reasoning_effort = _section_reasoning_effort(self._client, section)
        reasoning_effort_label = reasoning_effort or "not_requested"
        try:
            completion = _complete_detailed(
                self._client,
                messages,
                budget_s=budget_s,
                max_tokens=max_tokens,
                temperature=_INSIGHT_LANE_TEMPERATURE,
                reasoning_effort=reasoning_effort,
            )
        except CompletionTransportError as exc:
            return SynthesisOutcome(
                text=exc.partial.text.strip(),
                trace={
                    "attempted": True,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "finish_reason": exc.partial.finish_reason,
                    "elapsed_ms": exc.partial.elapsed_ms,
                    "prompt_chars": prompt_chars,
                    "max_tokens": max_tokens,
                    "temperature": _INSIGHT_LANE_TEMPERATURE,
                    "candidate_chars": len(exc.partial.text.strip()),
                    "material_floor": material_floor,
                    "reasoning_effort": reasoning_effort_label,
                    "thinking": thinking_observability(
                        getattr(self._client, "thinking_level", None),
                        exc.partial.usage,
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001 - caller records and isolates the lane
            return SynthesisOutcome(
                text="",
                trace={
                    "attempted": True,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "finish_reason": None,
                    "prompt_chars": prompt_chars,
                    "max_tokens": max_tokens,
                    "temperature": _INSIGHT_LANE_TEMPERATURE,
                    "candidate_chars": 0,
                    "material_floor": material_floor,
                    "reasoning_effort": reasoning_effort_label,
                },
            )
        candidate = completion.text.strip()
        return SynthesisOutcome(
            text=candidate,
            trace={
                "attempted": True,
                "error_type": None,
                "finish_reason": completion.finish_reason,
                "truncated": completion.finish_reason == "length",
                "elapsed_ms": completion.elapsed_ms,
                "prompt_chars": prompt_chars,
                "max_tokens": max_tokens,
                "temperature": _INSIGHT_LANE_TEMPERATURE,
                "candidate_chars": len(candidate),
                "material_floor": material_floor,
                "reasoning_effort": reasoning_effort_label,
                "thinking": thinking_observability(
                    getattr(self._client, "thinking_level", None),
                    completion.usage,
                ),
            },
        )

    def generate_structured_section_claims(
        self,
        plan: PlannerOutput,
        core_answer: str,
        *,
        fact_digest: FactDigest,
        retry_error: str | None,
        budget_s: float,
        section: str,
    ) -> SynthesisOutcome:
        """Generate one answer section so malformed output cannot kill its peer."""

        return self.generate_structured_insight_claims(
            plan,
            core_answer,
            fact_digest=fact_digest,
            retry_error=retry_error,
            budget_s=budget_s,
            section=section,
        )

    def synthesize_with_trace(
        self,
        plan: PlannerOutput,
        results: Sequence[SourceResult],
        turns: Sequence[ConversationTurn],
        *,
        budget_s: float = 60.0,
        state: SessionState | None = None,
        deterministic_facts: str | None = None,
        defer_market_facts: bool = False,
        fact_digest: FactDigest | None = None,
        core_only: bool = False,
    ) -> SynthesisOutcome:
        observed_on = _current_kst_date()
        synthesis_max_tokens = _synthesis_max_tokens()
        usable = _select_usable_results(plan, tuple(
            result
            for result in results
            if result.status == "ok"
            and _entity_match(result) != "MISMATCH"
            and (result.source != "web" or _web_has_citable_body(result.payload))
        ))
        if not usable:
            fallback = "이번 조회에서 확인된 근거가 없어 구체적인 답을 구성하지 못했습니다."
            absence_answer = _append_absence_context_surface("", results)
            has_typed_absence = bool(absence_answer)
            empty_answer = absence_answer if has_typed_absence else fallback
            empty_answer = _apply_active_kr_clinical_empty_surface(
                empty_answer,
                question=plan.resolved_question,
                results=results,
            )
            return SynthesisOutcome(
                text=empty_answer,
                trace={
                    "status": (
                        "typed_absence"
                        if has_typed_absence
                        else "no_usable_evidence"
                    ),
                    "fallback_reason": (
                        "confirmed_absence"
                        if has_typed_absence
                        else "no_evidence"
                    ),
                    "serving_id": "not_applicable",
                    "model": "not_applicable",
                },
            )

        messages = _synthesis_messages(
            plan,
            usable,
            turns,
            state=state,
            observed_on=observed_on,
            deterministic_facts=deterministic_facts,
            fact_digest=fact_digest,
            core_only=core_only,
        )
        try:
            messages, prompt_bound_trace = bound_synthesis_messages(
                messages,
                char_limit=SynthesisPolicy.from_env().prompt_char_limit,
            )
        except Exception as exc:
            # Bounding runs before the completion guard below. Letting it raise
            # would cost the whole grounded surface, not just the commentary.
            LOGGER.exception("v4 synthesis prompt bounding failed; sending unbounded prompt")
            prompt_bound_trace = {
                "applied": False,
                "before_chars": sum(len(message.get("content", "")) for message in messages),
                "after_chars": sum(len(message.get("content", "")) for message in messages),
                "strategy": "unbounded_after_error",
                "records_discarded": 0,
                "inspection_retains_full_payload": True,
                "error_type": type(exc).__name__,
            }
        completion: CompletionResult | None = None
        error_type: str | None = None
        error_category: str | None = None
        partial_generated = False
        try:
            completion = _complete_detailed(
                self._client,
                messages,
                budget_s=budget_s,
                max_tokens=synthesis_max_tokens,
            )
            answer = completion.text.strip()
        except CompletionTransportError as exc:
            completion = exc.partial
            answer = _complete_sentence_prefix(completion.text)
            partial_generated = bool(answer)
            error_category = exc.kind
            error_type = "transport"
            if partial_generated:
                answer = (
                    f"{answer.rstrip()}\n\n"
                    "해설 생성이 시간 내 완료되지 않아 일부만 표시합니다."
                )
        except Exception as exc:  # noqa: BLE001 - a grounded fallback is preferable to a 500
            answer = ""
            error_type = type(exc).__name__
            error_category = _completion_error_category(exc)

        fallback_reason: str | None = None
        if completion is not None and completion.finish_reason == "length":
            answer = _complete_sentence_prefix(completion.text)
            partial_generated = bool(answer)
            if partial_generated:
                answer = (
                    f"{answer.rstrip()}\n\n"
                    "해설 생성이 시간 내 완료되지 않아 일부만 표시합니다."
                )
            fallback_reason = "length"
        elif partial_generated:
            fallback_reason = "partial_transport"
        elif not answer:
            fallback_reason = "empty_or_transport_error"

        if answer and _RETRYABLE_INTERNAL_RE.search(answer):
            original_answer = answer
            repair_messages = [
                *messages,
                {"role": "assistant", "content": answer},
                {
                    "role": "user",
                    "content": (
                        "내부 도구명, MCP 상태 문구, totalCount, slot id, 쉼표로 나열한 NCT 식별자를 "
                        "노출하지 말고 원 단위 큰 수는 payload의 억원 display 값으로 바꿔 같은 근거로 "
                        "자연스러운 답변을 다시 작성하라. 개별 임상 ID는 "
                        "시험명·단계·설명에 녹여 쓸 때만 허용한다."
                    ),
                },
            ]
            try:
                repaired = _complete_detailed(
                    self._client,
                    repair_messages,
                    budget_s=min(6.0, budget_s),
                    max_tokens=4096,
                )
                answer = repaired.text.strip()
                if repaired.finish_reason == "length":
                    answer = original_answer
                else:
                    completion = repaired
            except Exception:  # noqa: BLE001 - deterministic surface replacement follows
                answer = original_answer

        if not answer:
            answer = "해설은 생성하지 못했고 조회 결과만 표시합니다."
            answer = _append_comparison_observations(answer, usable)
        elif _RETRYABLE_INTERNAL_RE.search(answer):
            answer = _replace_internal_blocks(answer, usable)

        hira_surface = inspect_requested_hira_surface(
            plan.resolved_question,
            answer,
            tuple(usable),
        )
        insight_richness_before = (
            {} if core_only else _s9_insight_metrics(answer, fact_digest)
        )
        insight_retry_attempted = not core_only and _should_retry_s17_insight(
            completion_available=completion is not None,
            metrics=insight_richness_before,
            axis_repair_pending=bool(hira_surface["missing"]),
            s17_contract_active=fact_digest is not None,
        )
        repair_max_tokens = _s17_repair_max_tokens(synthesis_max_tokens)
        insight_repair_prompt_chars = 0
        insight_retry_error_type: str | None = None
        insight_retry_candidate_accepted = False
        insight_partial_recovery: dict[str, Any] = {
            "attempted": False,
            "recovered": False,
            "reason": "no_transport_partial",
        }
        if insight_retry_attempted:
            instruction = (
                "답변 작문 계약을 충족하도록 한 번만 다시 작성하라. 핵심 답은 "
                "DerivedCoreCard의 값만 사용해 3~5문장으로 직접 답하고 표에 위임하거나 "
                "행을 나열하지 않는다. 종합 인사이트는 질문 축 심화, 실제 근거 자료원 "
                "교차 융합 순서로 쓴다. FactDigest 재료가 충분하면 3개 문단과 15~20문장, "
                "중간 수준이면 3개 문단과 최소 10문장으로 쓰며 비교·추세·구성·"
                "코드 파생값·교차 원천 융합 중 가능한 재료를 모두 사용하고 최소 2개 확장 축을 "
                "전개한다. 첫 문단은 2~3문장만 사용하고 L1·L2는 FactDigest의 "
                "구체 값 2개 이상을 문장마다 포함하고, L3는 같은 인사이트의 숫자 앵커에 정박한 "
                "추론 표지 문장을 2개 이상 쓴다. L3가 없으면 '융합 추론 누락' 사유로 재작성한다. "
                "FactDigest 밖 숫자·날짜·상태, 면책·주의·[확인 한계]·[출처] 문자열을 만들지 않는다. "
                "문서 청크 원문은 붙여 넣지 말고 한국어 요약·해석으로 변환하며 같은 "
                "(대상·필드·값) 사실을 두 문장에 재사용하지 않는다."
            )
            richness_messages = _s17_repair_messages(
                plan=plan,
                answer=answer,
                instruction=instruction,
                fact_digest=fact_digest,
                fallback_messages=messages,
            )
            insight_repair_prompt_chars = sum(
                len(message["content"]) for message in richness_messages
            )
            try:
                retried = _complete_detailed(
                    self._client,
                    richness_messages,
                    budget_s=min(_s17_repair_timeout_s(), budget_s),
                    max_tokens=repair_max_tokens,
                )
                retried_answer = retried.text.strip()
                if retried_answer and retried.finish_reason != "length":
                    answer = (
                        _replace_internal_blocks(retried_answer, usable)
                        if _RETRYABLE_INTERNAL_RE.search(retried_answer)
                        else retried_answer
                    )
                    completion = retried
                    insight_retry_candidate_accepted = True
            except CompletionTransportError as exc:
                insight_retry_error_type = type(exc).__name__
                if fact_digest is not None:
                    answer, insight_partial_recovery = _recover_grounded_partial_insight(
                        answer,
                        exc.partial.text,
                        fact_digest,
                    )
                    insight_retry_candidate_accepted = bool(
                        insight_partial_recovery.get("recovered")
                    )
                else:
                    insight_partial_recovery = {
                        "attempted": False,
                        "recovered": False,
                        "reason": "fact_digest_unavailable",
                    }
            except Exception as exc:  # noqa: BLE001 - deterministic richness fallback follows
                insight_retry_error_type = type(exc).__name__
        if fact_digest is not None and not core_only:
            answer, insight_richness_after = sanitize_s17_insight(answer, fact_digest)
            insight_richness_after = {
                **insight_richness_after,
                "retry_reason": (
                    insight_richness_after.get("expansion_retry_reason")
                    or insight_richness_after.get("reason_code")
                ),
            }
        else:
            insight_richness_after = _s9_insight_metrics(answer)

        hira_retry_attempted = _should_retry_legacy_hira_surface(
            completion_available=completion is not None,
            missing=hira_surface["missing"],
            fact_digest=fact_digest,
        )
        hira_retry_skipped_reason = (
            "s17_fact_digest_contract"
            if hira_surface["missing"]
            and completion is not None
            and fact_digest is not None
            else None
        )
        hira_repair_prompt_chars = 0
        hira_retry_error_type: str | None = None
        if hira_retry_attempted:
            hira_core_instruction = (
                "HIRA 요청 지표 결속 검사에서 누락이 발견됐다. 원형 detail을 다시 읽고 "
                "요청 연도와 입원/외래 구분마다 아래 값을 정확히 본문에 써라. "
                "환자수는 환자수(명) 값만 쓰고 명세서건수(건)를 환자수로 쓰지 마라. "
                "금액과 방문일수도 표시된 단위를 유지하라. 핵심 답은 DerivedCoreCard의 "
                "값만 사용해 3~5문장으로 직접 답한다."
            )
            hira_insight_instruction = (
                " 종합 인사이트는 L1 사실, L2 해석, "
                "L3 융합 추론 순서로 작성한다. FactDigest 재료가 충분하면 3개 문단과 "
                "15~20문장, 중간 수준이면 3개 문단과 최소 10문장으로 쓰며 비교·추세·구성·"
                "코드 파생값·교차 원천 융합 중 가능한 재료를 모두 사용한다. L1·L2는 FactDigest의 "
                "구체 값 2개 이상에 결속하고 L3는 공유 숫자 앵커와 추론 표지를 갖춘 2문장 이상으로 "
                "작성한다. FactDigest 밖 숫자·날짜·상태는 만들지 않는다."
            )
            hira_instruction = hira_core_instruction
            if not core_only:
                hira_instruction += hira_insight_instruction
            retry_messages = _s17_repair_messages(
                plan=plan,
                answer=answer,
                instruction=hira_instruction,
                fact_digest=fact_digest,
                fallback_messages=messages,
                extra={
                    "missing": [
                        {
                            "year": fact.year,
                            "care_type": fact.care_type,
                            "metric": fact.label,
                            "value": fact.display,
                        }
                        for fact in hira_surface["missing"]
                    ]
                },
            )
            hira_repair_prompt_chars = sum(
                len(message["content"]) for message in retry_messages
            )
            try:
                retried = _complete_detailed(
                    self._client,
                    retry_messages,
                    budget_s=min(30.0, budget_s),
                    max_tokens=repair_max_tokens,
                )
                retried_answer = retried.text.strip()
                if retried_answer and retried.finish_reason != "length":
                    answer = (
                        _replace_internal_blocks(retried_answer, usable)
                        if _RETRYABLE_INTERNAL_RE.search(retried_answer)
                        else retried_answer
                    )
                    completion = retried
            except Exception as exc:  # noqa: BLE001 - deterministic gate repairs remaining omission
                hira_retry_error_type = type(exc).__name__
        if fact_digest is not None and hira_retry_attempted and not core_only:
            answer, insight_richness_after = sanitize_s17_insight(answer, fact_digest)
            insight_richness_after = {
                **insight_richness_after,
                "retry_reason": (
                    insight_richness_after.get("expansion_retry_reason")
                    or insight_richness_after.get("reason_code")
                ),
            }
        hira_after_retry = inspect_requested_hira_surface(
            plan.resolved_question,
            answer,
            tuple(usable),
        )
        answer = _append_required_adverse_signal(answer, usable)
        answer = _append_absence_context_surface(answer, results)
        answer = _apply_reexamination_surface(
            answer,
            question=plan.resolved_question,
            results=usable,
            state=state,
            observed_on=observed_on,
        )
        answer = _apply_active_kr_clinical_empty_surface(
            answer,
            question=plan.resolved_question,
            results=results,
        )
        answer = _finalize_answer(answer, usable)
        if core_only:
            answer = _without_insight_section(answer)
        # After _finalize_answer on purpose: that step truncates at a model-owned
        # "## 출처" heading, which would swallow anything appended before it.
        answer, market_surface_trace = _inject_deterministic_market_surface(
            answer,
            usable,
            question=plan.resolved_question,
            enabled=not defer_market_facts,
        )
        answer = _append_comparison_observations(answer, usable)
        return SynthesisOutcome(
            text=answer,
            trace={
                "status": (
                    "partial"
                    if partial_generated
                    else "fallback" if fallback_reason else "synthesized"
                ),
                "finish_reason": completion.finish_reason if completion else None,
                "usage": completion.usage if completion else {},
                "elapsed_ms": completion.elapsed_ms if completion else None,
                "prompt_chars": sum(len(message["content"]) for message in messages),
                "prompt_bound": prompt_bound_trace,
                "deterministic_market_surface": market_surface_trace,
                "prompt_layout": {
                    "static_prefix_sha256": hashlib.sha256(
                        messages[0]["content"].encode()
                    ).hexdigest(),
                    "static_prefix_chars": len(messages[0]["content"]),
                    "dynamic_suffix_chars": len(messages[1]["content"]),
                    "explicit_cache": "unavailable_via_genos_serving",
                    "implicit_cache_eligible": True,
                },
                "raw_payload_chars": sum(
                    len(json.dumps(result.payload, ensure_ascii=False, default=str))
                    for result in usable
                ),
                "max_tokens": synthesis_max_tokens,
                "coverage_notices": list(_coverage_notices(usable)),
                "requested_hira_surface_retry": {
                    "attempted": hira_retry_attempted,
                    "skipped_reason": hira_retry_skipped_reason,
                    "error_type": hira_retry_error_type,
                    "prompt_chars": hira_repair_prompt_chars,
                    "max_tokens": repair_max_tokens,
                    "missing_before": [
                        {
                            "year": fact.year,
                            "care_type": fact.care_type,
                            "metric": fact.label,
                            "value": fact.display,
                        }
                        for fact in hira_surface["missing"]
                    ],
                    "missing_after": [
                        {
                            "year": fact.year,
                            "care_type": fact.care_type,
                            "metric": fact.label,
                            "value": fact.display,
                        }
                        for fact in hira_after_retry["missing"]
                    ],
                },
                "insight_richness_retry": {
                    "attempted": insight_retry_attempted,
                    "error_type": insight_retry_error_type,
                    "partial_recovery": insight_partial_recovery,
                    "prompt_chars": insight_repair_prompt_chars,
                    "max_tokens": repair_max_tokens,
                    "generated_sentence_count": int(
                        insight_richness_before.get("sentence_count", 0) or 0
                    ),
                    "retry_sentence_count": (
                        int(insight_richness_after.get("sentence_count", 0) or 0)
                        if insight_retry_candidate_accepted
                        else 0
                    ),
                    "retry_candidate_accepted": insight_retry_candidate_accepted,
                    "before": insight_richness_before,
                    "after": insight_richness_after,
                },
                "fallback_reason": fallback_reason,
                "error_type": error_type,
                "error_category": error_category,
                "partial_generated": partial_generated,
                "serving_id": completion.serving_id if completion else "not_applicable",
                "model": completion.model if completion else "not_applicable",
                "thinking": thinking_observability(
                    getattr(self._client, "thinking_level", None),
                    completion.usage if completion else {},
                ),
            },
        )


def _synthesis_max_tokens() -> int:
    raw = os.environ.get(
        "V4_SYNTHESIZER_MAX_TOKENS",
        str(_DEFAULT_SYNTHESIS_MAX_TOKENS),
    )
    try:
        configured = int(raw)
    except ValueError:
        configured = _DEFAULT_SYNTHESIS_MAX_TOKENS
    return min(
        _MAX_SYNTHESIS_MAX_TOKENS,
        max(_MIN_SYNTHESIS_MAX_TOKENS, configured),
    )


def _s17_repair_max_tokens(synthesis_max_tokens: int) -> int:
    raw = os.environ.get(
        "S17_REPAIR_MAX_TOKENS",
        str(_DEFAULT_S17_REPAIR_MAX_TOKENS),
    )
    try:
        configured = int(raw)
    except ValueError:
        configured = _DEFAULT_S17_REPAIR_MAX_TOKENS
    return min(
        synthesis_max_tokens,
        max(_MIN_S17_REPAIR_MAX_TOKENS, configured),
    )


def _insight_lane_max_tokens(section: str | None = None) -> int:
    default = (
        32_768
        if section == "insight"
        else 24_576
        if section == "facts"
        else _DEFAULT_INSIGHT_LANE_MAX_TOKENS
    )
    env_name = (
        f"CHAT_V4_{section.upper()}_LANE_MAX_TOKENS"
        if section in {"facts", "insight"}
        else "CHAT_V4_INSIGHT_LANE_MAX_TOKENS"
    )
    raw = os.environ.get(
        env_name,
        str(default),
    )
    try:
        configured = int(raw)
    except ValueError:
        configured = default
    return min(_MAX_SYNTHESIS_MAX_TOKENS, max(_MIN_SYNTHESIS_MAX_TOKENS, configured))


def _s17_repair_timeout_s() -> float:
    raw = os.environ.get(
        "S17_INSIGHT_REPAIR_TIMEOUT_S",
        os.environ.get("GENOS_FINAL_TIMEOUT_S", "50"),
    )
    try:
        configured = float(raw)
    except ValueError:
        configured = 50.0
    return max(1.0, configured)


def _s9_insight_metrics(
    answer: str,
    fact_digest: FactDigest | None = None,
) -> dict[str, Any]:
    if fact_digest is not None:
        _sanitized, trace = sanitize_s17_insight(answer, fact_digest)
        return {
            **trace,
            "retry_reason": (
                trace.get("expansion_retry_reason") or trace.get("reason_code")
            ),
        }
    matched = re.search(
        r"(?ms)^##\s+종합 인사이트\s*\n(?P<body>.*?)(?=^##\s+|\Z)",
        answer,
    )
    body = matched.group("body").strip() if matched else ""
    paragraphs = tuple(
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", body)
        if paragraph.strip()
    )
    sentences = tuple(
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", body)
        if sentence.strip()
    )
    blacklist = (
        "본문 표에 표시된 항목과 범위를 기준으로 읽어야 합니다",
        "대표 항목만으로 전체를 일반화하기보다",
        "표에 남은 구분과 기간을 함께 확인하면",
        "질문 축의 구성과 차이를 더 분명하게 파악할 수 있습니다",
        "같은 질문을 서로 다른 자료 범위에서 보완하므로",
        "한쪽 결과만으로 결론을 닫지 않는 것이 적절합니다",
        "요청 축과 인접 축을 함께 보면",
        "실제 검토 우선순위를 정하는 데 어떤 맥락을 주는지",
        "이 구조는 세부 표의 차이를",
        "시장 검토, 경쟁 구도 확인 또는 후속 자료 탐색의 출발점",
        "본문에 표시되지 않은 원인이나 효과를 단정하기보다",
        "관찰된 구성과 범위가 추가 확인의 차례를 보여주는",
        "이번 응답에서 근거가 확인된 자료원은",
        "다른 자료원과의 교차 비교는 근거가 추가될 때 별도로",
        "현재는 한 자료원의 항목들을 같은 정의와 범위 안에서 비교",
        "본문에 표시된 사실은 항목별 식별자와 기간, 자료원 범위 안에서 해석",
        "살펴보면 다음과 같습니다",
        "이어서 다른 주요 항목을 확인해 볼 수 있습니다",
        "판단하지 않습니다",
        "판단하지 않았습니다",
        "단정하기보다",
        "단정하지 않습니다",
        "읽어야 합니다",
        "일반화하기보다",
        "결론을 닫지 않는",
        "해석하는 편이 타당",
        "해석해야 합니다",
        "유의해야 합니다",
        "주의가 필요합니다",
        "[확인 한계]",
    )
    blacklist_hits = sum(body.count(term) for term in blacklist)
    citation_count = body.count("[출처")
    retry_reason = (
        "MISSING_REQUIRED_ROLE"
        if len(paragraphs) < 3 or len(sentences) < 4
        else None
    )
    return {
        "paragraph_count": len(paragraphs),
        "sentence_count": len(sentences),
        "character_count": len(body),
        "limitation_count": body.count("[확인 한계]"),
        "blacklist_hits": blacklist_hits,
        "llm_citation_count": citation_count,
        "retry_reason": retry_reason,
        "contract_met": (
            len(paragraphs) == 3
            and 4 <= len(sentences) <= 6
            and blacklist_hits == 0
            and citation_count == 0
            and not any(paragraph.startswith("또한,") for paragraph in paragraphs)
        ),
    }


def _should_retry_s17_insight(
    *,
    completion_available: bool,
    metrics: Mapping[str, Any],
    axis_repair_pending: bool = False,
    s17_contract_active: bool = True,
) -> bool:
    """Leave the single FactDigest repair for final semantic validation."""
    if not completion_available or axis_repair_pending:
        return False
    if s17_contract_active:
        return False
    return bool(
        not metrics.get("contract_met")
        and metrics.get("retry_reason")
        in {"MISSING_REQUIRED_ROLE", "MISSING_EVIDENCE", "AXIS_UNCLOSED", "융합 추론 누락"}
    )


def _should_retry_legacy_hira_surface(
    *,
    completion_available: bool,
    missing: Sequence[Any],
    fact_digest: FactDigest | None,
) -> bool:
    """Keep the legacy all-values prose repair outside the S17 card contract."""
    return bool(completion_available and missing and fact_digest is None)


def _s17_repair_messages(
    *,
    plan: PlannerOutput,
    answer: str,
    instruction: str,
    fact_digest: FactDigest | None,
    fallback_messages: Sequence[dict[str, str]],
    extra: Mapping[str, Any] | None = None,
    insight_only: bool = False,
) -> list[dict[str, str]]:
    """Build a bounded repair prompt from immutable cards, not raw result prose."""
    if fact_digest is None:
        fallback_instruction = (
            json.dumps(
                {"instruction": instruction, **dict(extra)},
                ensure_ascii=False,
            )
            if extra
            else instruction
        )
        return [
            *fallback_messages,
            {"role": "assistant", "content": answer},
            {"role": "user", "content": fallback_instruction},
        ]
    expansion_contract = insight_expansion_metrics(answer, fact_digest)
    required_sentence_count = max(
        1,
        int(expansion_contract.get("required_sentence_count") or 1),
    )
    maximum_sentence_count = int(
        expansion_contract.get("maximum_sentence_count") or 20
    )
    repair_shape = (
        f"정확히 3개 문단과 {required_sentence_count}~"
        f"{maximum_sentence_count}문장"
        if required_sentence_count >= 10
        else (
            f"가용 적격 근거를 소진한 {required_sentence_count}~"
            f"{maximum_sentence_count}문장"
        )
    )
    payload: dict[str, Any] = {
        "question": plan.resolved_question,
        "instruction": instruction,
        "fact_digest": fact_digest.repair_prompt_payload(),
        "retry_reason": expansion_contract.get("retry_reason"),
        "unused_fact_digest_materials": _repair_unused_fact_digest_materials(
            answer, fact_digest
        ),
        "contract": {
            "immutable_code_owned_facts": True,
            "llm_math_forbidden": True,
            "code_derived_values_allowed": True,
            "new_facts_forbidden": True,
            "required_sentence_count": required_sentence_count,
            "maximum_sentence_count": maximum_sentence_count,
            "unused_material_failure_threshold": 5,
            "l2_comparison_anchor_minimum": 2,
            "l3_actor_choice_required": True,
        },
    }
    if extra:
        payload.update(extra)
    section_contract = (
        "출력은 `## 종합 인사이트`로 시작하고 그 섹션만 작성한다. 기존 핵심답 재작성 금지. "
        if insight_only
        else (
            "출력에는 `## 핵심 답`과 `## 종합 인사이트`를 정확히 한 번씩 사용하고, "
            "자료원 요약 등 다른 소제목은 만들지 않는다. "
        )
    )
    return [
        {
            "role": "system",
            "content": (
                "FactDigest에 결속된 사실만 사용해 한국어 답변을 재작성한다. "
                "derived_fields의 코드 계산값은 그대로 인용할 수 있지만 새 계산은 금지한다. "
                "retry_reason이 '확장 부족'이면 unused_fact_digest_materials를 우선 사용해 "
                "비교·추세·구성·교차 원천 융합 중 아직 쓰지 않은 축을 확장한다. "
                f"확장 재작성은 목록 없이 {repair_shape}으로 쓴다. "
                "eligibility gate에서 탈락한 문장은 "
                "삭제해 분량을 줄이지 말고 supporting fact가 있는 문장으로 교체한다. "
                "둘째 문단의 각 문장은 FactDigest에 있는 숫자·날짜·상태 비교 앵커를 2개 이상 "
                "직접 인용하고 비교·격차·추세·구성 중 하나의 해석 결과를 명시한다. "
                "셋째 문단의 각 문장은 FactDigest 앵커와 함께 의사결정 주체와 선택지를 모두 명시한다. "
                "첫 문단은 직접 사실 2~3문장으로 제한하고, 미사용 재료가 5개 이상 남지 않게 "
                "전 lane의 서로 다른 적격 재료를 소진한다. 같은 (대상·필드·값) 사실을 두 문장에 "
                "재사용하지 않는다. 문서 청크는 근거로만 사용해 한국어로 요약·해석하고 영어 원문, "
                "청크 전문, `문서 요약 chunks` 접두문을 출력하지 않는다. "
                "같은 숫자 토큰은 2회 이하로 인용한다. "
                "'가늠해 볼 수 있습니다', '추론할 수 있습니다', '의미합니다', '확인됩니다'는 쓰지 않고 "
                "근거가 지지하는 해석을 직접 제시한다. "
                f"{section_contract}"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _structured_insight_messages(
    *,
    plan: PlannerOutput,
    core_answer: str,
    fact_digest: FactDigest,
    retry_error: str | None,
    target_section: str | None = None,
) -> list[dict[str, str]]:
    material_floor = _structured_material_floor(fact_digest)
    sparse_material = bool(material_floor["relaxed"])
    answer_contract = getattr(plan, "answer_contract", None) or fact_digest.answer_contract
    schema = {
        "s": target_section or "insight | facts",
        "e": ["FactDigest 또는 dm 카드의 실재 id를 중복 없이 한 번씩 선언"],
        "c": [[
            "한국어 완결 문장",
            "C | K | O | I | H (각각 CITE/CALC/OBS/INTERP/HYPO)",
            ["e 배열의 0 기반 인덱스"],
            ["answer_contract.required_items의 실재 id (없으면 이 위치 생략)"],
        ]],
    }
    payload = {
        "question": plan.resolved_question,
        "confirmed_core_answer": core_answer,
        "fact_digest": fact_digest.repair_prompt_payload(),
        "evidence_catalog": evidence_catalog_payload(fact_digest),
        "answer_contract": (
            answer_contract.model_dump(mode="json") if answer_contract is not None else None
        ),
        "material_supply": {
            "sources": list(
                dict.fromkeys(
                    card.source
                    for card in fact_digest.cards
                    if card.received_count > 0 and card.evidence_ids
                )
            ),
            "cards": [
                {
                    "source": card.source,
                    "entity": card.entity,
                    "received_count": card.received_count,
                    "evidence_ids": list(card.evidence_ids[:3]),
                }
                for card in fact_digest.cards
                if card.received_count > 0 and card.evidence_ids
            ],
        },
        "material_floor": material_floor,
        "output_schema": schema,
        "previous_parse_error": retry_error,
    }
    retry_instruction = (
        "이전 응답이 다음 이유로 파싱되지 않았다. 오류를 고쳐 JSON 객체 하나만 다시 출력한다: "
        f"{retry_error} "
        if retry_error
        else ""
    )
    section_instruction = (
        "이번 호출은 facts 섹션 전용이다. 모든 claim의 section은 facts이며 CITE·CALC·OBS만 사용한다. 재료량에 맞춰 3~8개 claim으로 정상 종료한다. "
        if target_section == "facts" and sparse_material
        else "이번 호출은 insight 섹션 전용이다. 모든 claim의 section은 insight이며 재료량에 맞춰 4~8개 claim으로 정상 종료한다. 확보되지 않은 관계나 전망을 만들지 않는다. "
        if target_section == "insight" and sparse_material
        else
        f"이번 호출은 facts 섹션 전용이다. 모든 claim의 section은 facts이며 CITE·CALC·OBS만 사용하고 가용 재료가 뒷받침하면 {material_floor['facts_claim_target']}개 이상 생성한다. "
        if target_section == "facts"
        else "이번 호출은 insight 섹션 전용이다. 모든 claim의 section은 insight이며 정확히 18개, INTERP와 HYPO 합계 8개 이상 생성한다. "
        if target_section == "insight"
        else "모든 claim은 section을 facts 또는 insight로 신고한다. "
    )
    volume_instruction = (
        f"{material_floor['reason']}입니다. facts는 확보된 사실을 공백 제외 300자 이상으로 서술하고 반복이나 억지 채움 없이 종료한다. "
        if target_section == "facts" and sparse_material
        else f"{material_floor['reason']}입니다. insight는 확보된 사실과 관계만으로 공백 제외 350자 이상을 목표로 하며 추측 확장 없이 종료한다. "
        if target_section == "insight" and sparse_material
        else
        f"facts는 가용 재료가 뒷받침하면 {material_floor['facts_claim_target']}개 이상으로 구성하고 공백 제외 {material_floor['facts_target_chars']:,}자를 목표로 한다. 수신 원천별 관련 사실을 관련도 순으로 소진하고, 각 수치에 기간·모수·원천을 함께 쓴다. 재료가 빈약하면 확보된 사실을 모두 서술한 뒤 반복이나 억지 채움 없이 종료한다. "
        if target_section == "facts"
        else "insight는 정확히 18개로 구성하고 각 claim의 text는 공백 제외 110자 이상으로 작성한다. INTERP와 HYPO를 합해 8개 이상 두며 공백 제외 최소 1,800자를 반드시 채운다. "
        if target_section == "insight"
        else "가용 재료가 충분하면 facts는 10개 이상, insight는 20개 이상으로 구성해 전체 claims는 30개 이상으로 만든다. insight의 INTERP와 HYPO를 합해 12개 이상 두고 분량 상한은 두지 않는다. insight는 공백 제외 최소 2,100자, facts는 공백 제외 최소 1,500자를 반드시 채운다. "
    )
    supply_instruction = (
        "material_supply에 있는 공급 원천마다 최소 1개 claim을 현재 섹션에 포함해 질문 실체와 연결된 비주원천 근거도 누락하지 않는다. "
        if target_section
        else "material_supply에 있는 공급 원천마다 최소 1개 claim을 facts와 insight에 각각 포함해 질문 실체와 연결된 비주원천 근거도 누락하지 않는다. "
    )
    completion_instruction = (
        "가용 사실과 관계를 모두 사용하면 JSON 객체를 닫고 정상 종료한다. "
        if sparse_material
        else
        "서로 다른 핵심 관계를 사용해 18개를 작성하고, 18개에 도달하면 JSON 객체를 닫고 종료한다. "
        if target_section == "insight"
        else "질문 직답을 먼저 쓴 뒤 수신 원천별 관련 사실이 남아 있는 동안 계속 작성하고, 재료를 소진하면 JSON 객체를 닫고 종료한다. "
        if target_section == "facts"
        else "사용하지 않은 관계·경쟁·시간 근거가 남아 있는 동안 길이 미달 응답을 반환하지 않는다. "
    )
    return [
        {
            "role": "system",
            "content": (
                "FactDigest와 derived_metrics만 사용해 요청된 답변 섹션을 압축 JSON 객체 하나로 출력한다. "
                "마크다운, 코드펜스, 설명문은 금지하고 JSON 객체 하나만 출력한다. "
                "최상위 s는 요청 섹션 id, c는 claim 행 배열이다. 각 행 위치는 "
                "[text, claim_type, evidence_ids, answers] 순서이며 answers가 없으면 네 번째 값을 생략한다. "
                "evidence_ids 안의 같은 id는 한 번만 쓴다. hedge는 claim_type에서 결정되므로 출력하지 않는다: "
                "CITE·CALC·OBS=none, INTERP=softened, HYPO=hypothesis. 긴 필드명 claims, section, text, "
                "claim_type, evidence_ids, hedge, answers는 출력하지 않는다. "
                + section_instruction
                + "facts는 질문에 대한 직접 답을 "
                "담고 첫 1~2개 claim에서 질문이 요구한 수치·기간·대상을 바로 제시한다. 이어서 원천, "
                "기간, 비교 대상을 명시한 관련 사실을 충분히 서술하되 CITE·CALC·OBS만 사용하고 "
                "해석·전망을 넣지 않는다. insight는 같은 직접 답의 요지를 1개 claim으로 시작한 뒤 "
                "사실 요지, 관계 해석, 시사점, 가설의 순서로 폭넓게 전개한다. derived_metrics의 "
                "서로 다른 종류를 가능한 한 모두 활용해 경쟁 구도·시장 구조·전망을 구체적으로 설명한다. "
                "claim_type은 CITE(원천 수치·날짜 인용), CALC(코드 파생 수치), "
                "OBS(방향·비교 관찰), INTERP(해석·시사점), HYPO(가설·전망) 중 하나다. "
                "CITE·CALC·OBS는 evidence_ids가 필수이고, INTERP·HYPO도 전제가 있으면 반드시 "
                "실재 id를 참조한다. evidence_catalog에 없는 id는 만들지 않는다. "
                "임상·특허·브랜드·상병·기간·파일을 지칭할 때는 근거에 실재하는 식별자(NCT 번호, 특허번호, 정확 브랜드명, 상병코드, YYYY-MM, 파일·시트명)를 함께 쓴다. "
                + volume_instruction
                + "해석과 가설은 적극 제시하되 없는 수치·날짜·관측 사실은 만들지 않는다. "
                "해석·가설은 derived_metrics의 관계 수치에서 도출한다. 데이터에 없는 시장 행위자의 "
                "행동·심리를 관측 사실처럼 서술하지 않는다. 대신 수치 변화 자체를 근거로 서술한다. "
                "비율과 퍼센트포인트는 소수 2자리, 금액은 억원 소수 2자리로 반올림한다. "
                "한국어 완결 문장만 쓰고 조사·단어 반복, 이중 마침표, 문두 잘림을 금지한다. "
                "동일 술어 문형은 답변당 1회 이하로 쓰며 MI팀 우선순위 조정 같은 범용 문구를 "
                "반복하지 않는다. hedge는 직접 사실·계산·관찰이면 none, 완화된 해석이면 softened, "
                "가설이면 hypothesis로 신고한다. answer_contract.required_items의 각 항목을 facts에서 "
                "직접 답하고 대응 claim의 answers에 그 id를 신고한다. 데이터가 없으면 해당 ask가 "
                "보유 원천에서 확인되지 않았다고 명시하며 무언으로 누락하지 않는다. insight 첫 claim은 "
                "answer_contract.question_core에 대한 직답 요지여야 한다. 관계·비교 derived metric마다 "
                "수치를 제시한 뒤 맥락과 의미를 1~2문장 더 전개하고, 경쟁 구도·시장 구조·시간 축을 "
                "각각 문단 수준으로 발전시킨다. "
                + supply_instruction
                + "각 섹션은 요구된 최소 분량을 반드시 채우며, "
                + completion_instruction
                + "문장마다 줄바꿈하지 말고 같은 의미 축의 3~5문장을 한 문단으로 이어 서술한다. "
                "'참조된 수치는 근거입니다', '관찰 수치는 검토 근거입니다', '확인된 기간 변화의 규모', "
                "'경쟁 구도 변화 가능성을 검토하는 근거'와 그 변형 같은 자기 참조·방법 서술형 군더더기를 "
                "만들지 말고 수치 관계의 맥락과 의미를 직접 서술한다. 특허·임상 건수를 인용할 때는 "
                "'직접 관련 N건 기준'처럼 "
                "그 수치의 모수를 같은 문장에 붙인다. "
                + retry_instruction
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _structured_material_floor(fact_digest: FactDigest) -> dict[str, Any]:
    supplied = tuple(
        card
        for card in fact_digest.cards
        if card.received_count > 0 and card.evidence_ids
    )
    evidence_count = len(
        {evidence_id for card in supplied for evidence_id in card.evidence_ids}
    )
    sources = tuple(dict.fromkeys(card.source for card in supplied))
    relaxed = (
        fact_digest.answer_type == "disease"
        and bool(supplied)
        and "hira" in sources
        and len(supplied) <= 2
        and evidence_count <= 18
        and len(fact_digest.derived_metrics) <= 12
    )
    material_count = max(evidence_count, len(supplied))
    file_material = fact_digest.answer_type == "file_aggregate" or any(
        card.card_type == "file_aggregate"
        or card.source in {"document", "document_sql"}
        for card in supplied
    )
    return {
        "relaxed": relaxed,
        "material_count": material_count,
        "source_count": len(sources),
        "dm_card_count": len(fact_digest.derived_metrics),
        "facts_minimum_chars": 300 if relaxed else 900 if file_material else 1200,
        "facts_target_chars": 300 if relaxed else 1200 if file_material else 1500,
        "facts_claim_target": 3 if relaxed else 10 if file_material else 14,
        "insight_minimum_chars": 350 if relaxed else 1800,
        "reason": (
            f"재료 {material_count}건 기준 축약 응답"
            if relaxed
            else "파일 재료량 기준 응답"
            if file_material
            else "표준 재료량 기준 응답"
        ),
    }


def _repair_unused_fact_digest_materials(
    answer: str,
    fact_digest: FactDigest,
) -> list[dict[str, Any]]:
    duplicate_paths = (
        "representative.content",
        "file_facts.chunks",
        "file_facts.targeted_facts",
    )
    return [
        entry
        for entry in unused_fact_digest_materials(answer, fact_digest)
        if not str(entry.get("path") or "").startswith(duplicate_paths)
    ]


def _complete_detailed(
    client: Any,
    messages: Sequence[dict[str, str]],
    *,
    budget_s: float,
    max_tokens: int,
    temperature: float | None = None,
    reasoning_effort: str | None | object = _REASONING_EFFORT_UNSET,
) -> CompletionResult:
    detailed = getattr(client, "complete_detailed", None)
    if callable(detailed):
        kwargs: dict[str, object] = {"budget_s": budget_s, "max_tokens": max_tokens}
        parameters = inspect.signature(detailed).parameters
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if temperature is not None and (
            "temperature" in parameters or accepts_kwargs
        ):
            kwargs["temperature"] = temperature
        if reasoning_effort is not _REASONING_EFFORT_UNSET and (
            "reasoning_effort" in parameters or accepts_kwargs
        ):
            kwargs["reasoning_effort"] = reasoning_effort
        return detailed(messages, **kwargs)
    text = client.complete(messages, budget_s=budget_s, max_tokens=max_tokens)
    return CompletionResult(
        text=text,
        finish_reason="stop",
        usage={},
        elapsed_ms=0.0,
    )


def _section_reasoning_effort(client: Any, section: str | None) -> str | None:
    env_name = (
        f"V4_{section.upper()}_REASONING_EFFORT"
        if section in {"facts", "insight"}
        else None
    )
    if env_name is not None and env_name in os.environ:
        raw = os.environ[env_name].strip()
        if not raw or raw.lower() == "default":
            return None
        return normalize_reasoning_effort(raw)
    return normalize_reasoning_effort(getattr(client, "reasoning_effort", None))


def _complete_sentence_prefix(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return ""
    boundaries = tuple(
        match.end()
        for match in re.finditer(r"(?:[.!?。](?=\s|$)|(?:다|요|음|임)\.(?=\s|$))", cleaned)
    )
    return cleaned[: boundaries[-1]].strip() if boundaries else ""


def _recover_grounded_partial_insight(
    base_answer: str,
    partial_text: str,
    digest: FactDigest,
) -> tuple[str, dict[str, Any]]:
    complete_partial = _complete_sentence_prefix(partial_text)
    base_sanitized, base_trace = sanitize_s17_insight(base_answer, digest)
    before_count = int(base_trace.get("retained_sentence_count") or 0)
    if not complete_partial:
        return base_sanitized, {
            "attempted": True,
            "recovered": False,
            "reason": "no_complete_sentence",
            "partial_chars": len(partial_text),
            "complete_prefix_chars": 0,
            "before_sentence_count": before_count,
            "after_sentence_count": before_count,
        }

    merged, candidate_trace = replace_s17_insight_section(
        base_sanitized,
        complete_partial,
        digest,
        require_expansion_target=False,
    )
    after_count = int(candidate_trace.get("retained_sentence_count") or 0)
    recovered = bool(
        candidate_trace.get("replacement_applied") and after_count > before_count
    )
    return (merged if recovered else base_sanitized), {
        "attempted": True,
        "recovered": recovered,
        "reason": "richer_grounded_partial" if recovered else "partial_not_richer",
        "partial_chars": len(partial_text),
        "complete_prefix_chars": len(complete_partial),
        "before_sentence_count": before_count,
        "after_sentence_count": after_count if recovered else before_count,
        "candidate": candidate_trace,
    }


def _completion_error_category(exc: Exception) -> str:
    name = type(exc).__name__.casefold()
    if "readtimeout" in name:
        return "read_timeout"
    if "timeout" in name:
        return "transport_timeout"
    if "connection" in name:
        return "connection_error"
    if "http" in name:
        return "upstream_http_error"
    return "upstream_error"


def _synthesis_messages(
    plan: PlannerOutput,
    results: Sequence[SourceResult],
    turns: Sequence[ConversationTurn],
    *,
    state: SessionState | None = None,
    observed_on: date | None = None,
    deterministic_facts: str | None = None,
    fact_digest: FactDigest | None = None,
    core_only: bool = False,
) -> list[dict[str, str]]:
    mart = tuple(result for result in results if result.source == "mart")
    external = tuple(result for result in results if result.source != "mart")
    fact_backed = bool(deterministic_facts)
    history = project_recent_turns(turns, limit=3)
    asks_cause = any(marker in plan.resolved_question.casefold() for marker in _CAUSE_MARKERS)
    comparison_facts = _comparison_facts(mart)
    deep_analysis_blocks = [
        block
        for result in mart
        for block in _deep_analysis_blocks(result)
    ]
    source_mapping, source_mapping_capped = _bounded_source_mapping(results)
    prompt = {
        "internal_datamart": [
            _fact_backed_source_packet(result) if fact_backed else _mart_block(result)
            for result in mart
        ],
        "external_evidence": _external_evidence_packets(external),
        "source_mapping": source_mapping,
        "recent_turns": history,
        "resolved_intents": list(plan.expanded_intents),
        "user_question": plan.resolved_question,
        **(
            {
                "source_mapping_contract": {
                    "capped_per_source": SynthesisPolicy.from_env().source_render_limit,
                    "omitted_counts": source_mapping_capped,
                    "do_not_enumerate_every_url_in_the_answer": True,
                    "instruction": (
                        "출처 목록은 코드가 별도로 렌더한다. 본문에서 인용한 항목만 "
                        "언급하고, 전체 URL 목록을 답변에 나열하지 않는다"
                    ),
                }
            }
            if source_mapping_capped
            else {}
        ),
        "as_of_date_context": as_of_date_instruction(
            observed_on or _current_kst_date()
        ),
        "output_guide": [
            "핵심 답은 DerivedCoreCard의 값만 사용해 3~5문장으로 직접 제시",
            "표의 세부 값을 전부 나열하지 않되 핵심 답을 표에 위임하거나 raw 청크를 복사하지 않음",
            "근거와 맥락",
            "종합 인사이트는 L1 사실, L2 해석, L3 고차 융합 추론 순서이며 재료 충분 시 3개 문단 15~20문장, 중간 재료 시 최소 10문장",
            "정상 수신 데이터는 비교·추세·구성·코드 파생값·교차 원천 융합 중 가능한 재료를 소진하고 확장 축 2개 이상 사용",
            "L1·L2 문장은 FactDigest 구체 값 2개 이상을 결속하고 L3는 공유 숫자 앵커와 추론 표지 문장 2개 이상",
            "FactDigest에 없는 숫자·날짜·상태 생성 금지; L3 부재는 융합 추론 누락 사유로 한 번만 재합성",
            "면책·주의·확인 한계 문장은 본문에 쓰지 않음",
            "출처는 코드가 렌더하므로 [출처] 문자열을 생성하지 않음",
        ],
    }
    if fact_digest is not None:
        prompt["fact_digest"] = fact_digest.compact_prompt_payload()
        prompt["fact_digest_contract"] = {
            "immutable_code_owned_facts": True,
            "llm_math_forbidden": True,
            "code_derived_values_allowed": True,
            "use_full_received_statistics": True,
            "visible_rows_are_examples_not_denominators": True,
            "core_must_bind_to_derived_core_cards": True,
            "file_facts_have_equal_evidence_priority": True,
        }
        if fact_digest.answer_type == "document_summary":
            if is_document_summary_request(fact_digest.question):
                prompt["output_guide"][0] = (
                    "문서 요약 핵심 답은 문서 정체 1문장과 선별 본문 청크의 핵심 수치·사실 "
                    "4~5문장을 합성해 전체 5~6문장으로 직접 제시"
                )
                prompt["document_summary_contract"] = {
                    "mode": "whole_document_summary",
                    "minimum_sentences": 5,
                    "maximum_sentences": 6,
                    "document_identity_sentences": 1,
                    "body_fact_sentences": {"minimum": 4, "maximum": 5},
                    "use_only_fact_digest_file_facts_chunks": True,
                    "verbatim_chunk_copy_forbidden": True,
                    "navigation_greeting_cover_chunks_forbidden": True,
                    "retrieval_metadata_narration_forbidden": True,
                    "insight_uses_the_generated_summary_facts": True,
                }
            else:
                prompt["output_guide"][0] = (
                    "문서 내 특정 사실 질문은 첫 문장에서 질문한 수치·명칭·기준을 직접 답하고 "
                    "관련 본문 청크만 사용해 전체 3~5문장으로 제시"
                )
                prompt["document_summary_contract"] = {
                    "mode": "targeted_document_answer",
                    "minimum_sentences": 3,
                    "maximum_sentences": 5,
                    "question_matching_facts_required": True,
                    "first_sentence_direct_answer_required": True,
                    "absence_forbidden_when_chunks_present": True,
                    "use_only_fact_digest_file_facts_chunks": True,
                    "verbatim_chunk_copy_forbidden": True,
                    "retrieval_metadata_narration_forbidden": True,
                }
    if core_only:
        prompt["output_guide"] = [
            item
            for item in prompt["output_guide"]
            if "종합 인사이트" not in item and "L1" not in item and "L3" not in item
        ]
        prompt["output_guide"].append(
            "핵심 답과 질문에 직접 필요한 근거 사실만 작성"
        )
        document_contract = prompt.get("document_summary_contract")
        if isinstance(document_contract, dict):
            document_contract.pop("insight_uses_the_generated_summary_facts", None)
    if deterministic_facts:
        prompt["deterministic_facts"] = deterministic_facts
        prompt["deterministic_commentary_contract"] = {
            "facts_are_precomputed_and_rendered_before_commentary": True,
            "do_not_recalculate_or_rewrite_facts": True,
            "do_not_repeat_full_tables_or_source_documents": True,
            "commentary_scope": (
                "질문에 직접 답하는 사실만 작성하며 근거에 없는 사실을 추가하지 않는다"
                if core_only
                else "해석과 맥락만 작성하며 근거에 없는 사실을 추가하지 않는다"
            ),
        }
    if deep_analysis_blocks:
        prompt["internal_deep_analysis"] = deep_analysis_blocks
        prompt["deep_analysis_contract"] = {
            "canonical_source_only": "cache_deep_analysis_ai_analysis",
            "separate_from_realtime_mart": True,
            "cite_with_freshness_label": True,
            "absence_rule": "구획이 없으면 심층분석을 언급하지 않는다",
        }
    if comparison_facts:
        prompt["COMPARISON_FACTS"] = comparison_facts
    if (fact_backed or comparison_facts) and not core_only:
        prompt["advisory_contract"] = {
            "section": "종합 인사이트",
            "required_when_facts_exist": True,
            "separate_from_fact_surface": True,
            "cite_fact_section": True,
            "new_numbers_forbidden": True,
            "assertive_recommendations_forbidden": True,
            "instruction": (
                (
                    "COMPARISON_FACTS와 내부 데이터마트 사실을 해석하되 "
                    if comparison_facts
                    else "deterministic_facts의 사실을 해석하되 "
                )
                + "사실면의 수치를 재작성하지 말고, "
                + "근거가 된 사실 구획을 밝혀 의사결정상 함의를 한 문단 이상 작성한다"
            ),
        }
    if any("entity_bundle" in _mart_block(result) for result in mart):
        prompt["entity_bundle_contract"] = {
            "same_period_and_denominator_only": True,
            "use_precomputed_comparison_facts": bool(comparison_facts),
            "comparison_instruction": (
                "COMPARISON_FACTS.observation_sentences의 완성된 사실 문장을 서술에 "
                "그대로 반영한다. supplied delta를 다시 계산하지 않는다. 점유율 방향 "
                "사실이 있으면 반드시 명시한다"
            ),
            "free_form_cross_brand_causality_or_movement_forbidden": True,
            "adverse_signal_must_be_explicit": True,
            "explicit_share_direction_sentence": True,
        }
    if any(_is_absence_context_result(result) for result in external):
        prompt["absence_context_contract"] = {
            "official_document_lookup_is_typed": True,
            "non_reimbursed_requires_confirmed_non_reimbursed": True,
            "web_context_uses_reported_language": True,
            "separate_official_and_reported_claims": True,
            "do_not_replace_absent_document_with_other_document": True,
        }
    if any(result.source == "document" for result in external):
        prompt["uploaded_document_contract"] = {
            "same_evidence_fusion": True,
            "compare_with_other_sources_in_the_same_paragraph": True,
            "per_source_paragraph_dump_forbidden": True,
            "raw_chunk_dump_forbidden": True,
            "cite_document_name_section_and_page": True,
            "official_source_wins_on_conflict": True,
        }
    document_has_records = any(
        result.source == "document" and bool(_raw_records(result.payload))
        for result in external
    )
    other_source_has_records = any(
        result.source != "document"
        and result.status == "ok"
        and bool(_raw_records(result.payload) or result.payload)
        for result in results
    )
    if document_has_records and other_source_has_records:
        prompt["file_fusion_contract"] = {
            "minimum_file_linked_narratives": 1,
            "link_file_fact_with_relevant_non_file_fact": True,
            "interpretation_language_required": True,
            "cite_every_source_used_by_the_sentence": True,
            "new_numbers_forbidden": True,
            "do_not_force_when_only_file_evidence_exists": True,
        }
    reexamination_contract = _reexamination_prompt_contract(
        plan.resolved_question,
        external,
        state=state,
        observed_on=observed_on or _current_kst_date(),
    )
    if reexamination_contract:
        prompt["reexamination_contract"] = reexamination_contract
    required_hira_surface = _required_hira_surface(plan.resolved_question, results)
    if required_hira_surface:
        prompt["required_hira_surface"] = required_hira_surface
    prompt["session_state"] = state.public_dict() if state else None
    if asks_cause:
        prompt["cause_answer_contract"] = {
            "layers": ["관측", "날짜가 확인된 외부 사건", "가설"],
            "missing_event_rule": (
                "날짜가 확인된 외부 사건이 없으면 그 층을 생략하지 말고 "
                "'날짜가 확인된 외부 사건: 확인되지 않았습니다'라고 쓴다"
            ),
            "period_rule": (
                "cause_period_anchor의 공통 시작·종료 기간 안에서만 모든 분해 수치를 비교한다"
            ),
            "attribution_rule": (
                "시장 전체 채널·성분 수치를 특정 브랜드 원인으로 귀속하지 않는다. "
                "브랜드×채널 교차 근거가 있을 때만 브랜드 수준으로 쓴다"
            ),
            "language_ladder": [
                "관측: ~로 확인되었습니다",
                "시점 병치: ~와 시기가 일치합니다",
                "해석: ~로 해석될 수 있습니다",
                "가설: ~일 가능성이 있으나 확인되지 않았습니다",
            ],
            "forbidden_without_movement_evidence": ["전환", "잠식", "대체"],
            "forbidden_causal_phrases": [
                "기인한 것으로 관측됩니다",
                "원인으로 확인되었습니다",
            ],
            "few_shot_shape": (
                "관측 수치를 먼저 시장 기여와 점유율 기여로 나누고, 날짜가 확인된 외부 사건은 "
                "시점만 병치하며, 남는 설명은 확인되지 않은 가설로 분리한다. 예시 문장의 수치는 "
                "사용하지 말고 현재 payload 값만 그대로 사용한다"
            ),
        }
    system_prompt = (
        _CORE_SYNTHESIS_SYSTEM_PROMPT if core_only else _SYNTHESIS_SYSTEM_PROMPT
    )
    if fact_digest is not None and fact_digest.answer_type == "document_summary":
        if is_document_summary_request(fact_digest.question):
            system_prompt += (
                " 문서 요약은 file_facts.chunks를 서로 연결해 새 문장으로 합성한다. "
                "청크 문장을 그대로 이어 붙이거나 목차·인사말·표지·검색 메타데이터를 쓰지 않는다."
            )
        else:
            system_prompt += (
                " 문서 내 특정 사실 질문은 file_facts.chunks에서 질문과 일치하는 수치·명칭·기준을 "
                "첫 문장에 직접 답한다. 청크가 있으면 부재 답을 쓰지 않고, 원문을 그대로 복사하지 않는다."
            )
    return [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": json.dumps(prompt, ensure_ascii=False, default=str),
        },
    ]


def _comparison_facts(results: Sequence[SourceResult]) -> dict[str, Any]:
    return build_comparison_facts(results)


def _append_comparison_observations(
    answer: str,
    results: Sequence[SourceResult],
) -> str:
    raw_sentences = _comparison_facts(results).get("observation_sentences")
    if not isinstance(raw_sentences, list):
        return answer
    missing = [
        sentence.strip()
        for sentence in raw_sentences
        if isinstance(sentence, str)
        and sentence.strip()
        and sentence.strip() not in answer
    ]
    if not missing:
        return answer
    observations = "\n".join(
        f"{sentence} [출처: 내부 데이터마트]" for sentence in missing
    )
    return f"{answer.rstrip()}\n\n## 비교 관측\n{observations}"


def _legacy_comparison_facts(results: Sequence[SourceResult]) -> dict[str, Any]:
    calls: list[Mapping[str, Any]] = []
    for result in results:
        if result.source != "mart" or not isinstance(result.payload, Mapping):
            continue
        raw_calls = result.payload.get("calls")
        if isinstance(raw_calls, list):
            calls.extend(call for call in raw_calls if isinstance(call, Mapping))
        else:
            calls.append(result.payload)
    call_index = 0
    while call_index < len(calls):
        nested_calls = calls[call_index].get("tool_calls")
        if isinstance(nested_calls, list):
            calls.extend(call for call in nested_calls if isinstance(call, Mapping))
        call_index += 1
    bundle = next(
        (
            call.get("entity_bundle")
            for call in calls
            if isinstance(call.get("entity_bundle"), Mapping)
        ),
        None,
    )
    if not isinstance(bundle, Mapping):
        for call in calls:
            render_data = call.get("render_data")
            if not isinstance(render_data, Mapping):
                continue
            anchor_brand = str(render_data.get("anchor_brand") or "").strip()
            rows = render_data.get("competitor_rows")
            if not anchor_brand or not isinstance(rows, list):
                continue
            points: list[tuple[str, Decimal, Decimal]] = []
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                if str(row.get("brand") or "").strip() != anchor_brand:
                    continue
                period = str(row.get("period") or "").strip()
                sales_krw = _decimal_value(row.get("sales_krw"))
                share_pct = _decimal_value(row.get("share_pct"))
                if period and sales_krw is not None and share_pct is not None and share_pct > 0:
                    points.append((period, sales_krw, share_pct))
            points.sort(key=lambda point: point[0])
            if len(points) < 2 or points[0][0] == points[-1][0]:
                continue
            period_start, sales_start, share_start = points[0]
            period_end, sales_end, share_end = points[-1]
            krw_per_eok = Decimal(100000000)
            bundle = {
                "anchor": anchor_brand,
                "period_start": period_start,
                "period_end": period_end,
                "same_period_and_denominator": True,
                "members": [
                    {
                        "brand": anchor_brand,
                        "role": "target",
                        "share_delta_pctp": share_end - share_start,
                        "render_data": {
                            "brand_value_series_10pt": [
                                {"period": period_start, "value_억원": sales_start / krw_per_eok},
                                {"period": period_end, "value_억원": sales_end / krw_per_eok},
                            ]
                        },
                    }
                ],
            }
            calls.append(
                {
                    "render_data": {
                        "market_size_series": [
                            {
                                "period": period_start,
                                "value_억원": sales_start * 100 / share_start / krw_per_eok,
                            },
                            {
                                "period": period_end,
                                "value_억원": sales_end * 100 / share_end / krw_per_eok,
                            },
                        ]
                    }
                }
            )
            break
    if not isinstance(bundle, Mapping):
        return {}
    period_start = str(bundle.get("period_start") or "").strip()
    period_end = str(bundle.get("period_end") or "").strip()
    members = bundle.get("members")
    if not period_start or not period_end or not isinstance(members, list):
        return {}

    deltas: list[dict[str, Any]] = []
    numeric_deltas: list[tuple[str, Decimal]] = []
    target_values: tuple[str, Decimal, Decimal] | None = None
    competitor_share_changes: list[dict[str, str]] = []
    for member in members:
        if not isinstance(member, Mapping):
            continue
        brand = str(member.get("brand") or "").strip()
        role = str(member.get("role") or "").strip()
        render_data = member.get("render_data")
        if not brand or not isinstance(render_data, Mapping):
            continue
        series = render_data.get("brand_value_series_10pt") or render_data.get("series")
        values = _period_values(series)
        start = values.get(period_start)
        end = values.get(period_end)
        if start is not None and end is not None:
            delta = end - start
            deltas.append(
                {
                    "brand": brand,
                    "role": role,
                    "start": _fixed_display(start, "억원"),
                    "end": _fixed_display(end, "억원"),
                    "delta": _fixed_display(delta, "억원", signed=True),
                }
            )
            numeric_deltas.append((brand, delta))
            if role == "target":
                target_values = (brand, start, end)
        share_delta = _decimal_value(member.get("share_delta_pctp"))
        if role == "competitor" and share_delta is not None:
            competitor_share_changes.append(
                {
                    "brand": brand,
                    "change": _fixed_display(share_delta, "%p", signed=True),
                }
            )

    positives = [(brand, value) for brand, value in numeric_deltas if value > 0]
    negatives = [(brand, value) for brand, value in numeric_deltas if value < 0]
    symmetric_pairs: list[dict[str, str]] = []
    remaining = list(negatives)
    for increase_brand, increase in positives:
        if not remaining:
            break
        decrease_brand, decrease = min(
            remaining,
            key=lambda item: abs(abs(increase) - abs(item[1])),
        )
        remaining.remove((decrease_brand, decrease))
        symmetric_pairs.append(
            {
                "increase_brand": increase_brand,
                "increase_delta": _fixed_display(increase, "억원", signed=True),
                "decrease_brand": decrease_brand,
                "decrease_delta": _fixed_display(decrease, "억원", signed=True),
            }
        )

    market_values: dict[str, Decimal] = {}
    for call in calls:
        render_data = call.get("render_data")
        if not isinstance(render_data, Mapping):
            continue
        market_values = _period_values(render_data.get("market_size_series"))
        if period_start in market_values and period_end in market_values:
            break
    share_direction: dict[str, str] = {}
    if target_values is not None:
        brand, brand_start, brand_end = target_values
        market_start = market_values.get(period_start)
        market_end = market_values.get(period_end)
        brand_growth = _growth_pct(brand_start, brand_end)
        market_growth = (
            _growth_pct(market_start, market_end)
            if market_start is not None and market_end is not None
            else None
        )
        if brand_growth is not None and market_growth is not None:
            direction = (
                "상승"
                if brand_growth > market_growth
                else "하락"
                if brand_growth < market_growth
                else "유지"
            )
            brand_growth_display = _fixed_display(brand_growth, "%", signed=True)
            market_growth_display = _fixed_display(market_growth, "%", signed=True)
            growth_comparison = (
                f"시장 성장률 {market_growth_display}과 같아"
                if direction == "유지"
                else (
                    f"시장 성장률 {market_growth_display}보다 "
                    f"{'높아' if direction == '상승' else '낮아'}"
                )
            )
            share_direction = {
                "brand": brand,
                "brand_growth": brand_growth_display,
                "market_growth": market_growth_display,
                "direction": direction,
                "statement": (
                    f"{brand} 성장률 {brand_growth_display}가 {growth_comparison} "
                    f"점유율 방향은 {direction}입니다."
                ),
            }

    return {
        "period_start": period_start,
        "period_end": period_end,
        "brand_deltas": deltas,
        "symmetric_pairs": symmetric_pairs,
        "share_direction": share_direction,
        "competitor_share_changes": competitor_share_changes,
    }


def _period_values(series: Any) -> dict[str, Decimal]:
    if not isinstance(series, list):
        return {}
    values: dict[str, Decimal] = {}
    for point in series:
        if not isinstance(point, Mapping):
            continue
        period = str(point.get("period") or "").strip()
        value = _decimal_value(point.get("value_억원"))
        if period and value is not None:
            values[period] = value
    return values


def _growth_pct(start: Decimal, end: Decimal) -> Decimal | None:
    if start == 0:
        return None
    return ((end - start) / start * Decimal(100)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def _fixed_display(value: Decimal, suffix: str, *, signed: bool = False) -> str:
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    prefix = "+" if signed and quantized > 0 else ""
    return f"{prefix}{format(quantized, '.2f')}{suffix}"


def _is_absence_context_result(result: SourceResult) -> bool:
    if result.source != "web" or not isinstance(result.payload, Mapping):
        return False
    context = result.payload.get("absence_context")
    return bool(
        isinstance(context, Mapping)
        and (
            context.get("official_document_not_found") is True
            or context.get("official_absence") is True
        )
        and context.get("reported_context_only") is True
    )


def _append_absence_context_surface(
    answer: str,
    results: Sequence[SourceResult],
) -> str:
    context_result = next(
        (
            result
            for result in results
            if _is_absence_context_result(result)
        ),
        None,
    )
    context: Mapping[str, Any] | None = None
    if context_result is not None and isinstance(context_result.payload, Mapping):
        candidate = context_result.payload.get("absence_context")
        if isinstance(candidate, Mapping):
            context = candidate
    if context is None:
        confirmation = _typed_absence_context(results)
        if confirmation is None:
            return answer
        context = confirmation
    subject = str(context.get("subject") or "").strip()
    source = str(context.get("source") or "")
    document = str(context.get("document") or "")
    if not subject or not source or not document:
        return answer
    official_label = "HIRA" if source == "hira" else "식품의약품안전처"
    absence_status = str(context.get("absence_status") or "doc_not_found")
    if document == "reimbursement":
        if absence_status == "confirmed_non_reimbursed":
            official_sentence = (
                f"{subject}{_topic_particle(subject)} 현재 급여기준이 없습니다(비급여). "
                f"[출처: {official_label}]"
            )
        else:
            official_sentence = (
                "현재 조회한 HIRA 세부 급여기준에서는 별도 기준을 찾지 못했습니다. "
                "이 결과만으로 비급여 여부를 확정할 수는 없습니다. "
                f"[출처: {official_label}]"
            )
    else:
        official_sentence = (
            f"{subject}{_topic_particle(subject)} 현재 허가 문서를 확인할 수 없습니다. "
            f"[출처: {official_label}]"
        )
    answer = _insert_core_first_paragraph(answer, official_sentence)
    reported_result = next(
        (
            result
            for result in results
            if result.status == "ok" and _is_absence_context_result(result)
        ),
        None,
    )
    items = (
        _absence_web_items(reported_result.payload)
        if reported_result is not None and isinstance(reported_result.payload, Mapping)
        else []
    )
    event = next(
        (
            item
            for item in items
            if isinstance(item, Mapping)
            and any(
                marker in str(item.get("title") or "")
                for marker in ("협상", "결렬", "재신청", "약가")
            )
        ),
        None,
    )
    if event is None:
        return answer
    title = " ".join(str(event.get("title") or "").split())
    if not title:
        return answer
    published_date = _observed_publication_date(event)
    if title in answer and (not published_date or published_date in answer):
        return answer
    date_prefix = f"{published_date} 게시된 " if published_date else ""
    event_sentence = (
        f"{_PUBLIC_SOURCE['web']}에서는 {date_prefix}\"{title}\"로 보도되고 있습니다 "
        f"[출처: {_PUBLIC_SOURCE['web']}]."
    )
    first_break = answer.find("\n\n")
    if first_break < 0:
        return f"{answer.rstrip()}\n\n{event_sentence}"
    return f"{answer[:first_break]}\n\n{event_sentence}\n\n{answer[first_break + 2:]}"


def _observed_publication_date(item: Mapping[str, Any]) -> str:
    for key in ("published_at", "published_date", "date"):
        value = str(item.get(key) or "").strip()
        match = re.match(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})(?:\D|$)", value)
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return ""


def _typed_absence_context(
    results: Sequence[SourceResult],
) -> dict[str, str] | None:
    for result in results:
        record = typed_absence_record(result)
        if record is None or result.evidence is None:
            continue
        claims = set(result.evidence.eligible_claims)
        required = {record.doc_type}
        if record.status == "confirmed_non_reimbursed":
            required.update(
                {
                    "absence_confirmation",
                    f"absence_confirmation:{record.doc_type}",
                }
            )
        if required.issubset(claims):
            return {
                "source": result.source,
                "document": record.doc_type,
                "subject": record.subject,
                "absence_status": record.status,
            }
    return None


def _absence_web_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    direct = payload.get("items")
    if isinstance(direct, list):
        return [item for item in direct if isinstance(item, Mapping)]
    calls = payload.get("calls")
    if not isinstance(calls, list):
        return []
    return [
        item
        for call in calls
        if isinstance(call, Mapping)
        for render_data in (call.get("render_data"),)
        if isinstance(render_data, Mapping)
        for items in (render_data.get("items"),)
        if isinstance(items, list)
        for item in items
        if isinstance(item, Mapping)
    ]


def _insert_core_first_paragraph(answer: str, sentence: str) -> str:
    cleaned = answer.replace(sentence, "").strip()
    heading = "## 핵심 답"
    if cleaned.startswith(heading):
        remainder = cleaned[len(heading):].lstrip("\n")
        return f"{heading}\n{sentence}" + (f"\n\n{remainder}" if remainder else "")
    return f"{heading}\n{sentence}" + (f"\n\n{cleaned}" if cleaned else "")


def _topic_particle(subject: str) -> str:
    last = subject[-1]
    code = ord(last)
    if 0xAC00 <= code <= 0xD7A3:
        return "은" if (code - 0xAC00) % 28 else "는"
    return "는"


def _fact_backed_source_packet(result: SourceResult) -> dict[str, Any]:
    return {
        "source": _PUBLIC_SOURCE[result.source],
        "query": result.query,
        "status": result.status,
        "detail": {
            "omitted": "raw source payload is retained in inspection detail",
        },
    }


def _bounded_source_mapping(
    results: Sequence[SourceResult],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Hand synthesis the same number of urls per source that the surface shows.

    source_mapping used to carry every citation of every result. That is a third
    rule for the same cap: the display notice said "clinicaltrials: 40/1004 표시",
    the deterministic source block rendered its own set, and this list gave the
    model all 1,004 - which it then dutifully wrote out, producing an answer that
    was 93% link list. Capping here at the same per-source limit means the three
    surfaces finally agree, and the omitted counts are reported rather than
    silently dropped.
    """
    limit = SynthesisPolicy.from_env().source_render_limit
    mapping: list[dict[str, Any]] = []
    kept: dict[str, int] = {}
    omitted: dict[str, int] = {}
    for result in results:
        label = _PUBLIC_SOURCE[result.source]
        for citation in result.citations:
            if kept.get(label, 0) >= limit:
                omitted[label] = omitted.get(label, 0) + 1
                continue
            kept[label] = kept.get(label, 0) + 1
            mapping.append(
                {
                    "source": label,
                    "url": citation.url,
                    "retrieved_at": citation.retrieved_at.isoformat(),
                }
            )
    return mapping, omitted


def _evidence_packet(
    result: SourceResult,
    *,
    include_detail: bool = True,
) -> dict[str, Any]:
    evidence = result.evidence
    packet = {
        "source": _PUBLIC_SOURCE[result.source],
        "query": result.query,
        "evidence": evidence.model_dump(mode="json") if evidence else {
            "entity_match": _entity_match(result),
            "source_scope": _SOURCE_SCOPE[result.source],
            "time_match": _time_match(result),
        },
        "detail": (
            result.payload
            if include_detail
            else {"omitted": "raw source payload is retained in inspection detail"}
        ),
    }
    if result.source == "hira":
        packet["field_labels"] = dict(_HIRA_FIELD_LABELS)
    if result.source == "clinicaltrials":
        packet["study_classification"] = _clinical_study_classification(result.payload)
    return packet


def _external_evidence_packets(
    results: Sequence[SourceResult],
) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    for result in results:
        if result.source != "document":
            packets.append(
                _evidence_packet(
                    result,
                    include_detail=result.source == "prior_turn",
                )
            )
            continue
        records = _raw_records(result.payload)
        accounting = (
            result.payload.get("route_accounting", {})
            if isinstance(result.payload, Mapping)
            else {}
        )
        for legacy_lane in ("document_rag", "document_sql"):
            lane_records = [
                record
                for record in records
                if document_record_lane(record) == legacy_lane
            ]
            route = accounting.get(legacy_lane, {}) if isinstance(accounting, Mapping) else {}
            if not lane_records and not (
                isinstance(route, Mapping) and route.get("planned") is True
            ):
                continue
            packets.append(
                {
                    "source": _PUBLIC_SOURCE[legacy_lane],
                    "lane_id": canonical_file_lane(legacy_lane),
                    "legacy_tool": legacy_lane,
                    "query": result.query,
                    "evidence": {
                        "entity_match": _entity_match(result),
                        "source_scope": _SOURCE_SCOPE[result.source],
                        "time_match": _time_match(result),
                    },
                    "record_count": len(lane_records),
                    "document_names": list(
                        dict.fromkeys(
                            str(record.get("document_name") or record.get("file_name") or "")
                            for record in lane_records
                        )
                    ),
                    "detail": {
                        "omitted": "raw source payload is retained in inspection detail"
                    },
                }
            )
    return packets


def _clinical_study_classification(payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            protocol = value.get("protocolSection")
            if isinstance(protocol, Mapping):
                identification = protocol.get("identificationModule")
                design = protocol.get("designModule")
                arms = protocol.get("armsInterventionsModule")
                study_id = str(value.get("NCTId") or value.get("nctId") or "").strip()
                if not study_id and isinstance(identification, Mapping):
                    study_id = str(
                        identification.get("nctId") or identification.get("NCTId") or ""
                    ).strip()
                study_type = ""
                if isinstance(design, Mapping):
                    study_type = str(
                        design.get("studyType") or design.get("study_type") or ""
                    ).strip().upper()
                interventions = arms.get("interventions") if isinstance(arms, Mapping) else ()
                intervention_types = tuple(
                    dict.fromkeys(
                        str(item.get("type") or "").strip().upper()
                        for item in interventions or ()
                        if isinstance(item, Mapping) and str(item.get("type") or "").strip()
                    )
                )
                if study_id and study_id not in seen:
                    seen.add(study_id)
                    therapeutic = bool(
                        set(intervention_types)
                        & {"DRUG", "BIOLOGICAL", "GENETIC", "DIETARY_SUPPLEMENT", "RADIATION"}
                    )
                    if study_type == "OBSERVATIONAL":
                        answer_section = "ADJACENT_OBSERVATIONAL"
                    elif study_type == "INTERVENTIONAL" and therapeutic:
                        answer_section = "PRIMARY_DRUG_INTERVENTIONAL"
                    else:
                        answer_section = "ADJACENT_NON_DRUG_INTERVENTIONAL"
                    rows.append(
                        {
                            "study_id": study_id,
                            "study_type": study_type or "UNKNOWN",
                            "intervention_types": list(intervention_types),
                            "answer_section": answer_section,
                        }
                    )
            for item in value.values():
                visit(item)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                visit(item)

    visit(payload)
    return rows


def _mart_block(result: SourceResult) -> str:
    payload = _public_mart_payload(_without_deep_analysis_calls(result.payload))
    cause_tables = _cause_table_packets(result)
    if cause_tables and isinstance(payload, Mapping):
        payload = {**payload, "cause_tables": cause_tables}
    tables = _markdown_tables(payload)
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    body = "\n\n".join((*tables, f"원형 JSON: {raw}"))
    return f"<INTERNAL_DATAMART source=\"{_PUBLIC_SOURCE[result.source]}\">\n{body}\n</INTERNAL_DATAMART>"


def _cause_table_packets(result: SourceResult) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    payload = result.payload
    calls = payload.get("calls") if isinstance(payload, Mapping) else None
    if not isinstance(calls, list):
        return packets
    for call in calls:
        if not isinstance(call, Mapping) or call.get("tool") != "cause_card_data":
            continue
        render_data = call.get("render_data")
        if not isinstance(render_data, Mapping):
            continue
        anchor = call.get("cause_period_anchor")
        if not isinstance(anchor, Mapping):
            anchor = render_data.get("cause_period_anchor")
        period = {
            "start": str(anchor.get("period_start") or "") if isinstance(anchor, Mapping) else "",
            "end": str(anchor.get("period_end") or "") if isinstance(anchor, Mapping) else "",
        }
        analysis_level = str(render_data.get("analysis_level") or "market")
        for table, values in render_data.items():
            if table == "cause_period_anchor":
                continue
            packets.append(
                {
                    "table": str(table),
                    "subject_grain": _cause_subject_grain(str(table), analysis_level),
                    "period": period,
                    "values": _public_mart_payload(values),
                }
            )
    return packets


def _cause_subject_grain(table: str, analysis_level: str) -> str:
    normalized = table.casefold()
    if "company" in normalized:
        return "company"
    if "customer" in normalized or "channel" in normalized:
        return "channel"
    if normalized in {"analysis_level", "analysis_level_trend"}:
        return analysis_level
    if normalized in {"ei_ms", "growth_contribution"}:
        return "brand"
    return "market"


def _without_deep_analysis_calls(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    copied = dict(value)
    calls = copied.get("calls")
    if isinstance(calls, list):
        copied["calls"] = [
            call
            for call in calls
            if not isinstance(call, Mapping) or call.get("tool") != "agent2_deep_analysis"
        ]
    return copied


def _deep_analysis_blocks(result: SourceResult) -> list[str]:
    payload = result.payload
    calls = payload.get("calls") if isinstance(payload, Mapping) else None
    if not isinstance(calls, list):
        return []
    blocks: list[str] = []
    for call in calls:
        if not isinstance(call, Mapping) or call.get("tool") != "agent2_deep_analysis":
            continue
        freshness = str(call.get("freshness_label") or "내부 심층분석 · 생성시점 미확인")
        public = {
            "brand": call.get("brand"),
            "market_id": call.get("market_id"),
            "subject_grain": call.get("subject_grain"),
            "analysis_variant": call.get("analysis_variant"),
            "analysis": call.get("analysis"),
            "generated_at": call.get("generated_at"),
            "freshness_label": freshness,
        }
        blocks.append(
            f'<INTERNAL_DEEP_ANALYSIS source="{freshness}">\n'
            f"{json.dumps(public, ensure_ascii=False, default=str)}\n"
            "</INTERNAL_DEEP_ANALYSIS>"
        )
    return blocks


def _public_mart_payload(value: Any) -> Any:
    """Remove renderer/progress metadata while preserving every evidence value."""

    if isinstance(value, Mapping):
        return {
            str(key): _public_mart_payload(item)
            for key, item in value.items()
            if str(key) not in {"summary_text", "tool"}
        }
    if isinstance(value, list):
        return [_public_mart_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_public_mart_payload(item) for item in value)
    return value


def _required_hira_surface(
    question: str,
    results: Sequence[SourceResult],
) -> list[dict[str, str]]:
    expected = inspect_requested_hira_surface(question, "", tuple(results))["expected"]
    return [
        {
            "year": fact.year,
            "care_type": fact.care_type,
            "metric": fact.label,
            "value": fact.display,
        }
        for fact in expected
    ]

def _select_usable_results(
    plan: PlannerOutput,
    results: tuple[SourceResult, ...],
) -> tuple[SourceResult, ...]:
    return tuple(
        sorted(
            results,
            key=lambda item: (
                item.source not in plan.answer_sources,
                SOURCE_ORDER[item.source],
            ),
        )
    )


SOURCE_ORDER = {
    "mart": 0,
    "nedrug": 1,
    "hira": 2,
    "openfda": 3,
    "clinicaltrials": 4,
    "web": 5,
    "patent": 6,
    "document": 7,
    "prior_turn": 8,
}


def _markdown_tables(value: Any) -> tuple[str, ...]:
    tables: list[str] = []
    for rows in _dict_lists(value):
        columns = tuple(
            key
            for key in dict.fromkeys(str(key) for row in rows for key in row)
            if any(
                not isinstance(row.get(key), (Mapping, list, tuple))
                for row in rows
                if key in row
            )
        )[:12]
        if not columns:
            continue
        header = "| " + " | ".join(columns) + " |"
        separator = "| " + " | ".join("---" for _ in columns) + " |"
        body = [
            "| " + " | ".join(_cell(row.get(column)) for column in columns) + " |"
            for row in rows[:20]
        ]
        tables.append("\n".join((header, separator, *body)))
        if len(tables) >= 4:
            break
    return tuple(tables)


def _dict_lists(value: Any):
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _dict_lists(item)
    elif isinstance(value, list):
        rows = [item for item in value if isinstance(item, Mapping)]
        if rows:
            yield rows
        for item in value:
            yield from _dict_lists(item)


def _cell(value: Any) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ")


def _entity_match(result: SourceResult) -> str:
    if result.evidence is not None:
        return result.evidence.entity_match
    values = [
        str(value).casefold()
        for path, value in _walk_scalars(result.payload)
        if path.casefold().endswith(("match_scope", "entity_match"))
    ]
    if any("mismatch" in value for value in values):
        return "MISMATCH"
    if any("partial" in value or "component" in value for value in values):
        return "PARTIAL"
    return "EXACT"


def _time_match(result: SourceResult) -> str:
    if result.evidence is not None:
        return result.evidence.time_match
    requested = set(re.findall(r"(?:19|20)\d{2}", result.query))
    if not requested:
        return "NOT_REQUESTED"
    payload_years = {
        str(value)
        for path, value in _walk_scalars(result.payload)
        if path.casefold().endswith(("year", "period", "yyyymm"))
    }
    return "MATCH" if any(any(year in value for value in payload_years) for year in requested) else "MISMATCH"


def _web_has_citable_body(payload: Any) -> bool:
    candidates = [
        str(value).strip()
        for path, value in _walk_scalars(payload)
        if path.casefold().endswith(("content", "body", "snippet", "raw_content", "text"))
    ]
    return any(len(text) >= 200 and not re.search(r"로그인|paywall|구독", text, re.IGNORECASE) for text in candidates)


def _walk_scalars(value: Any, prefix: str = ""):
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_scalars(item, path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_scalars(item, f"{prefix}[{index}]")
    else:
        yield prefix, value


def _replace_internal_blocks(answer: str, results: Sequence[SourceResult]) -> str:
    del results
    blocks: list[str] = []
    for block in re.split(r"\n\s*\n", answer):
        cleaned = _INTERNAL_ASSIGNMENT_RE.sub("", block)
        cleaned = _INTERNAL_SURFACE_RE.sub("", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned).strip()
        if cleaned:
            blocks.append(cleaned)
    return "\n\n".join(dict.fromkeys(blocks))


def _append_automatic_footnotes(answer: str, results: Sequence[SourceResult]) -> str:
    surfaced_sources = tuple(
        result.source
        for result in results
        if _surfaced_record_count(_raw_records(result.payload), answer) > 0
    )
    return append_automatic_fact_notices(answer, surfaced_sources)


def _without_insight_section(answer: str) -> str:
    return re.sub(
        r"(?ms)^##\s+종합 인사이트[^\n]*(?:\n.*?)?(?=^##\s+|\Z)",
        "",
        answer,
    ).strip()


def _finalize_answer(answer: str, results: Sequence[SourceResult]) -> str:
    # The final gate renders citations from typed results. Remove the model-owned
    # source section first so deterministic footnotes remain. Preserve any
    # following typed section, including the repaired insight surface.
    answer = re.sub(
        r"(?ms)^##\s+출처[^\n]*(?:\n.*?)?(?=^##\s+|\Z)",
        "",
        answer,
    ).strip()
    freshness_labels = _deep_analysis_freshness_labels(results)
    if freshness_labels:
        answer = (
            f"{answer.rstrip()}\n\n"
            + "\n".join(
                f"- {label}: 사전 생성 분석이며 실시간 데이터마트와 생성 시점이 다릅니다."
                for label in freshness_labels
                if label not in answer
            )
        ).rstrip()
    for result in results:
        notice = str(result.notice or "").strip()
        if (
            result.source == "mart"
            and notice.startswith("요청한 종료 기간 ")
            and notice not in answer
        ):
            answer = f"{answer.rstrip()}\n\n{notice}"
    answer = _append_automatic_footnotes(answer, results)
    return answer


_REEXAM_NAME_KEYS = {
    "item_name",
    "itemname",
    "product_name",
    "prdlst_nm",
    "품목명",
}
_REEXAM_DATE_KEYS = {
    "reexam_date",
    "reexamination_date",
    "reexam_period",
    "재심사기간",
}
_REEXAM_TARGET_KEYS = {
    "reexam_target",
    "reexamination_target",
    "재심사대상",
}
_REEXAM_ENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "리바로": ("리바로", "livalo"),
    "리바로젯": ("리바로젯", "livalozet"),
    "리피토": ("리피토", "lipitor"),
    "아토젯": ("아토젯", "atozet"),
}
_DATE_TOKEN_RE = re.compile(r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})")


def _apply_reexamination_surface(
    answer: str,
    *,
    question: str,
    results: Sequence[SourceResult],
    state: SessionState | None,
    observed_on: date,
) -> str:
    if "재심사" not in question:
        return answer
    primary = _reexamination_primary(question, state)
    if not primary:
        return answer
    records = _reexamination_records(results)
    if not records:
        return answer
    primary_records = [
        record for record in records if _same_product(record["name"], primary)
    ]
    primary_record = primary_records[0] if primary_records else {
        "name": primary,
        "date": "",
        "target": "",
    }
    primary_statement = _reexamination_statement(
        primary,
        primary_record,
        observed_on=observed_on,
    )
    related_records = [
        record for record in records if not _same_product(record["name"], primary)
    ]
    related_statements = [
        _reexamination_statement(record["name"], record, observed_on=observed_on)
        for record in related_records
        if record.get("date") or _is_explicit_not_subject(str(record.get("target") or ""))
    ]

    known_names = tuple(
        dict.fromkeys((primary, *(str(record["name"]) for record in records)))
    )
    remaining = _without_model_reexamination_claims(answer, known_names)
    blocks = ["## 핵심 답", primary_statement]
    if related_statements:
        blocks.extend(("## 관련 제품", "\n".join(f"- {item}" for item in related_statements)))
    if remaining:
        if remaining.startswith("## "):
            blocks.append(remaining)
        else:
            blocks.extend(("## 근거와 맥락", remaining))
    return "\n\n".join(blocks)


def _reexamination_prompt_contract(
    question: str,
    results: Sequence[SourceResult],
    *,
    state: SessionState | None,
    observed_on: date,
) -> dict[str, Any]:
    if "재심사" not in question:
        return {}
    primary = _reexamination_primary(question, state)
    if not primary:
        return {}
    records = _reexamination_records(results)
    if not records:
        return {}
    return {
        "primary_entity": primary,
        "as_of_date": observed_on.isoformat(),
        "primary_must_be_answered_first": True,
        "related_products_use_separate_section": True,
        "missing_date_does_not_mean_elapsed": True,
        "records": records,
    }


def _reexamination_primary(
    question: str,
    state: SessionState | None,
) -> str | None:
    return _reexamination_subject(question) or (
        state.primary_entity if state else None
    )


def _reexamination_subject(question: str) -> str | None:
    normalized = " ".join(question.split())
    for canonical, aliases in _REEXAM_ENTITY_ALIASES.items():
        if any(alias.casefold() in normalized.casefold() for alias in aliases):
            return canonical
    match = re.search(r"([가-힣A-Za-z0-9+._-]{2,40})(?:의)?\s*재심사", normalized)
    return match.group(1) if match else None


def _reexamination_records(results: Sequence[SourceResult]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for result in results:
        if result.source != "nedrug" or result.status != "ok":
            continue
        for item in _nested_mappings(result.payload):
            lowered = {str(key).casefold(): value for key, value in item.items()}
            name = _first_mapping_value(lowered, _REEXAM_NAME_KEYS)
            if not name:
                continue
            date_value = _first_mapping_value(lowered, _REEXAM_DATE_KEYS)
            target = _first_mapping_value(lowered, _REEXAM_TARGET_KEYS)
            records.append(
                {
                    "name": name,
                    "date": date_value or "",
                    "target": target or "",
                }
            )
    deduplicated: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        identity = (record["name"], record["date"], record["target"])
        if identity not in seen:
            seen.add(identity)
            deduplicated.append(record)
    return deduplicated


def _nested_mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, Mapping):
        children = tuple(
            nested
            for item in value.values()
            for nested in _nested_mappings(item)
        )
        return (value, *children)
    if isinstance(value, (list, tuple)):
        return tuple(
            nested
            for item in value
            for nested in _nested_mappings(item)
        )
    return ()


def _first_mapping_value(
    mapping: Mapping[str, Any],
    keys: set[str],
) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _same_product(item_name: str, requested: str) -> bool:
    item = re.sub(r"\s+", "", item_name).casefold()
    requested_folded = re.sub(r"\s+", "", requested).casefold()
    aliases = next(
        (
            values
            for canonical, values in _REEXAM_ENTITY_ALIASES.items()
            if requested_folded == canonical.casefold()
            or requested_folded in {value.casefold() for value in values}
        ),
        (requested,),
    )
    for alias in aliases:
        normalized = re.sub(r"\s+", "", alias).casefold()
        if item == normalized:
            return True
        if item.startswith(normalized):
            suffix = item[len(normalized) :]
            if suffix.startswith(("정", "캡슐", "주", "액", "시럽", "서방")):
                return True
    return False


def _is_explicit_not_subject(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).casefold()
    return any(marker in normalized for marker in ("대상아님", "비대상", "notsubject"))


def _reexamination_statement(
    label: str,
    record: Mapping[str, str],
    *,
    observed_on: date,
) -> str:
    date_value = str(record.get("date") or "").strip()
    target = str(record.get("target") or "").strip()
    if date_value:
        dates = [_parsed_date(match) for match in _DATE_TOKEN_RE.finditer(date_value)]
        dates = [value for value in dates if value is not None]
        if dates and dates[-1] < observed_on:
            return (
                f"{label}의 재심사 기간은 {date_value}였으며 종료일 "
                f"{dates[-1].isoformat()}은 이미 경과했습니다. [출처: 식품의약품안전처]"
            )
        end_clause = f"이며 종료일은 {dates[-1].isoformat()}입니다" if dates else "입니다"
        return (
            f"{label}의 재심사 기간은 {date_value}{end_clause}. "
            "[출처: 식품의약품안전처]"
        )
    if _is_explicit_not_subject(target):
        return f"{label}는 현재 재심사 대상이 아닙니다. [출처: 식품의약품안전처]"
    return (
        f"식품의약품안전처 품목 자료에서 {label}의 재심사 기간을 확인할 수 없습니다. "
        "재심사 날짜 부재만으로 기간 경과를 뜻하지는 않습니다. [출처: 식품의약품안전처]"
    )


def _parsed_date(match: re.Match[str]) -> date | None:
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _without_model_reexamination_claims(
    answer: str,
    product_names: Sequence[str],
) -> str:
    kept: list[str] = []
    for line in answer.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("##"):
            if stripped != "## 핵심 답":
                kept.append(stripped)
            continue
        reexamination_status_claim = (
            "재심사" in stripped
            or (
                any(marker in stripped for marker in ("끝났", "경과", "종료", "대상"))
                and ("기간" in stripped or any(name in stripped for name in product_names))
            )
        )
        if reexamination_status_claim:
            continue
        kept.append(stripped)
    return "\n".join(kept)


def _apply_active_kr_clinical_empty_surface(
    answer: str,
    *,
    question: str,
    results: Sequence[SourceResult],
) -> str:
    """Keep an empty requested trial set distinct from adjacent evidence."""

    normalized = " ".join(question.split()).casefold()
    clinical_requested = "임상" in normalized
    active_requested = any(
        marker in normalized for marker in ("진행 중", "진행중", "모집 중", "모집중")
    )
    kr_requested = any(marker in normalized for marker in ("국내", "한국", "대한민국"))
    if not (clinical_requested and active_requested and kr_requested):
        return answer

    if _active_kr_clinical_subset_state(results) != "empty":
        return answer

    notice = "확인된 국내 진행 중 임상시험은 없었습니다."
    body = answer.strip()
    if notice in body:
        return body
    heading = "## 핵심 답"
    if body.startswith(heading):
        remainder = body[len(heading) :].lstrip()
        if remainder.startswith("## 인접 동향"):
            return f"{heading}\n{notice}\n\n{remainder}"
        return f"{heading}\n{notice}\n\n## 인접 동향\n{remainder}"
    return f"{heading}\n{notice}\n\n## 인접 동향\n{body}"


_ACTIVE_CLINICAL_STATUSES = {
    "ACTIVE_NOT_RECRUITING",
    "ENROLLING_BY_INVITATION",
    "NOT_YET_RECRUITING",
    "RECRUITING",
}
_CLINICAL_RECORD_KEYS = {
    "nctid",
    "nct_id",
    "studyid",
    "study_id",
    "protocolsection",
    "protocol_section",
}


def _active_kr_clinical_subset_state(
    results: Sequence[SourceResult],
) -> str:
    saw_empty = False
    saw_explicit_record = False
    saw_unknown_ok = False
    for result in results:
        if result.source != "clinicaltrials":
            continue
        query_is_kr = any(
            marker in result.query.casefold() for marker in ("korea", "대한민국", "한국")
        )
        if result.status == "empty":
            saw_empty = True
            continue
        if result.status != "ok":
            continue
        records = _clinical_records(result.payload)
        if not records:
            saw_unknown_ok = True
            continue
        record_was_explicit = False
        for record in records:
            statuses = _clinical_values(record, "status")
            countries = _clinical_values(record, "country")
            if not statuses or (not countries and not query_is_kr):
                continue
            record_was_explicit = True
            saw_explicit_record = True
            is_active = any(
                value.upper().replace(" ", "_") in _ACTIVE_CLINICAL_STATUSES
                for value in statuses
            )
            is_kr = query_is_kr if not countries else any(
                marker in value.casefold()
                for value in countries
                for marker in ("korea", "대한민국", "한국")
            )
            if is_active and is_kr:
                return "present"
        if not record_was_explicit:
            saw_unknown_ok = True
    if saw_unknown_ok:
        return "unknown"
    if saw_explicit_record or saw_empty:
        return "empty"
    return "unknown"


def _clinical_records(value: Any) -> tuple[Mapping[str, Any], ...]:
    records: list[Mapping[str, Any]] = []

    def collect(item: Any) -> None:
        if isinstance(item, Mapping):
            keys = {str(key).casefold() for key in item}
            if keys.intersection(_CLINICAL_RECORD_KEYS) or (
                any("status" in key for key in keys)
                and any("country" in key for key in keys)
            ):
                records.append(item)
                return
            for nested in item.values():
                collect(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                collect(nested)

    collect(value)
    return tuple(records)


def _clinical_values(record: Mapping[str, Any], marker: str) -> tuple[str, ...]:
    return tuple(
        str(value).strip()
        for path, value in _walk_scalars(record)
        if marker in path.casefold() and value not in (None, "")
    )


def _deep_analysis_freshness_labels(
    results: Sequence[SourceResult],
) -> tuple[str, ...]:
    labels: list[str] = []
    for result in results:
        payload = result.payload
        calls = payload.get("calls") if isinstance(payload, Mapping) else None
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, Mapping) or call.get("tool") != "agent2_deep_analysis":
                continue
            label = str(call.get("freshness_label") or "").strip()
            if label:
                labels.append(label)
    return tuple(dict.fromkeys(labels))


def _coverage_notices(results: Sequence[SourceResult]) -> tuple[str, ...]:
    notices: list[str] = []
    for result in results:
        mart_notice = str(result.notice or "").strip()
        if result.source == "mart" and mart_notice.startswith("요청한 종료 기간 "):
            notices.append(mart_notice)
            continue
        if result.source not in {"hira", "nedrug"} or not isinstance(result.payload, Mapping):
            continue
        coverage = result.payload.get("period_coverage")
        if not isinstance(coverage, Mapping):
            continue
        periods = coverage.get("periods")
        if not isinstance(periods, list):
            continue
        for item in periods:
            if not isinstance(item, Mapping):
                continue
            period = str(item.get("period") or "").strip()
            status = str(item.get("status") or "").casefold()
            if not period:
                continue
            if status == "error":
                notices.append(f"{period}년은 조회 실패로 값을 확인하지 못했습니다(환자수 0 이 아님).")
            elif status == "no_data":
                notices.append(f"{period}년은 조회 완료됐으나 해당 데이터가 없습니다.")
    return tuple(dict.fromkeys(notices))


def _deterministic_market_blocks(
    results: Sequence[SourceResult],
    *,
    question: str = "",
) -> tuple[str, ...]:
    """The deterministic mart surface: comparison table, dimension facts, history.

    This is the single generator for that surface. Both the "synthesis produced
    nothing" fallback and the always-on injection read from here, so the two
    paths cannot drift apart the way they did in R12.7c.
    """
    blocks: list[str] = []
    for result in results:
        if result.source != "mart":
            continue
        entity_bundle = _entity_bundle_fallback(result.payload)
        dimensions = render_mart_dimension_facts(
            (result,),
            question=question or result.query,
        )
        history = _mart_history_fallback(
            result.payload,
            question=question or result.query,
        )
        blocks.extend(block for block in (entity_bundle, dimensions, history) if block)
    return tuple(blocks)


def _inject_deterministic_market_surface(
    answer: str,
    results: Sequence[SourceResult],
    *,
    question: str,
    enabled: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Attach the deterministic mart surface to every answer, not just failures.

    The constitution makes code responsible for the whole fact surface; the LLM
    only adds commentary. Emitting the table only when synthesis failed inverted
    that, so this runs on every path.

    Blocks are appended rather than prepended so the model's direct answer stays
    at the top. Tables are exempt from sentence-dedup (it skips lines starting
    with "|" or "#"), so ordering cannot cost the table any rows; a prose
    sentence the model duplicated verbatim is what collapses instead.
    """
    blocks = _deterministic_market_blocks(results, question=question)
    pending = (
        [block for block in blocks if block.strip() and block.strip() not in answer]
        if enabled
        else []
    )
    trace = {
        "blocks_available": len(blocks),
        "blocks_injected": len(pending),
        "blocks_already_present": len(blocks) - len(pending),
        "table_rows": sum(
            1
            for block in blocks
            for line in block.splitlines()
            if line.startswith("|") and not set(line) <= set("| -:")
        ),
        "records_discarded": 0,
        "deferred_to_final_composition": not enabled,
    }
    if not pending:
        return answer, trace
    if not answer.strip():
        return "\n\n".join(pending), trace
    return "\n\n".join((answer.rstrip(), *pending)), trace


def _evidence_fallback(
    results: Sequence[SourceResult],
    *,
    question: str = "",
) -> str:
    paragraphs: list[str] = []
    for result in results:
        if result.source == "mart":
            blocks = _deterministic_market_blocks((result,), question=question)
            if blocks:
                paragraphs.extend(blocks)
                continue
        if result.source == "hira":
            patient_facts = _hira_patient_facts(result.payload)
            if patient_facts:
                paragraphs.append(" ".join(patient_facts) + " [출처: HIRA]")
                continue
        summaries = _safe_summaries(result.payload)
        if summaries:
            paragraphs.append(
                " ".join(summaries) + f" [출처: {_PUBLIC_SOURCE[result.source]}]"
            )
            continue
        paragraphs.append(
            f"{_PUBLIC_SOURCE[result.source]}에서 질문과 관련된 상세 근거가 확인되었습니다. "
            f"[출처: {_PUBLIC_SOURCE[result.source]}]"
        )
    if not paragraphs:
        return "조회는 완료됐지만 답변 본문에 제시할 수 있는 상세 근거를 확인하지 못했습니다."
    return "\n\n".join(paragraphs)


def _entity_bundle_fallback(payload: Any) -> str:
    calls: list[Any]
    if isinstance(payload, Mapping) and isinstance(payload.get("calls"), list):
        calls = payload["calls"]
    else:
        calls = [payload]
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        bundle = call.get("entity_bundle")
        if not isinstance(bundle, Mapping) or bundle.get("same_period_and_denominator") is not True:
            continue
        period_start = str(bundle.get("period_start") or "").strip()
        period_end = str(bundle.get("period_end") or "").strip()
        members = bundle.get("members")
        if not period_start or not period_end or not isinstance(members, list):
            continue
        rows: list[str] = []
        for member in members:
            if not isinstance(member, Mapping):
                continue
            brand = str(member.get("brand") or "").strip()
            render = member.get("render_data")
            if not brand or not isinstance(render, Mapping):
                continue
            series = render.get("brand_value_series_10pt") or render.get("series")
            if not isinstance(series, list):
                continue
            by_period = {
                str(point.get("period")): point.get("value_억원")
                for point in series
                if isinstance(point, Mapping)
                and point.get("period") not in (None, "")
                and point.get("value_억원") not in (None, "")
            }
            if period_start not in by_period or period_end not in by_period:
                continue
            role = {
                "target": "대상",
                "family": "패밀리",
                "competitor": "경쟁",
            }.get(str(member.get("role") or ""), "비교")
            company = str(member.get("company") or "-").strip() or "-"
            rank = member.get("rank")
            rank_text = str(rank) if rank not in (None, "") else "-"
            rows.append(
                f"| {role} | {brand} | {company} | {rank_text} | "
                f"{by_period[period_start]} | {by_period[period_end]} |"
            )
        if rows:
            return "\n".join(
                (
                    f"## 동일 기간 브랜드 비교 ({period_start}~{period_end})",
                    "| 구분 | 브랜드 | 회사 | 순위 | 시작 매출(억원) | 종료 매출(억원) |",
                    "| --- | --- | --- | ---: | ---: | ---: |",
                    *rows,
                    "[출처: 내부 데이터마트]",
                )
            )
    return ""


def _append_required_adverse_signal(
    answer: str,
    results: Sequence[SourceResult],
) -> str:
    if re.search(r"점유율[^.\n]{0,30}하락", answer):
        return answer
    share_direction = _comparison_facts(results).get("share_direction")
    if (
        isinstance(share_direction, Mapping)
        and share_direction.get("direction") == "하락"
        and share_direction.get("statement")
    ):
        return (
            f"{answer.rstrip()}\n\n{share_direction['statement']} "
            "[출처: 내부 데이터마트]"
        )
    for result in results:
        if result.source != "mart" or not isinstance(result.payload, Mapping):
            continue
        calls = result.payload.get("calls")
        candidates = calls if isinstance(calls, list) else [result.payload]
        for call in candidates:
            if not isinstance(call, Mapping):
                continue
            bundle = call.get("entity_bundle")
            if not isinstance(bundle, Mapping):
                continue
            members = bundle.get("members")
            if not isinstance(members, list):
                continue
            for member in members:
                if not isinstance(member, Mapping) or member.get("role") != "target":
                    continue
                delta = _decimal_value(member.get("share_delta_pctp"))
                brand = str(member.get("brand") or bundle.get("anchor") or "대상 브랜드").strip()
                if delta is None or delta >= 0:
                    continue
                shown = format(abs(delta).normalize(), "f")
                return (
                    f"{answer.rstrip()}\n\n같은 기간 {brand}의 점유율은 "
                    f"{shown}%p 하락했습니다. [출처: 내부 데이터마트]"
                )
    return answer


def _decimal_value(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _mart_history_fallback(payload: Any, *, question: str) -> str:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("calls"), list):
        return ""
    market_requested = "시장 규모" in question.casefold() or "시장규모" in question.casefold()
    series_key = "market_size_series" if market_requested else "brand_value_series_10pt"
    for call in payload["calls"]:
        if not isinstance(call, Mapping):
            continue
        render = call.get("render_data")
        if not isinstance(render, Mapping):
            continue
        series = render.get(series_key)
        if not isinstance(series, list):
            continue
        points = [
            item
            for item in series
            if isinstance(item, Mapping)
            and item.get("period") not in (None, "")
            and item.get("value_억원") not in (None, "")
        ]
        if len(points) < 2:
            continue
        selected = [
            item
            for index, item in enumerate(points)
            if index == 0
            or str(item["period"]).endswith("-12")
            or index == len(points) - 1
        ]
        if len(selected) < 2:
            continue
        first = selected[0]
        last = selected[-1]
        first_period = str(first["period"])
        last_period = str(last["period"])
        first_value = str(first["value_억원"])
        last_value = str(last["value_억원"])
        brand = str(render.get("brand") or "브랜드")
        metric = f"{brand} 전략 시장 규모" if market_requested else f"{brand} 매출"
        particle = "는" if market_requested else "은"
        direction = _mart_history_direction(first_value, last_value)
        duration = _mart_history_year_span(first_period, last_period)
        prose = (
            f"{metric}{particle} {_mart_history_period(first_period)} {first_value}억원에서 "
            f"{_mart_history_period(last_period)} {last_value}억원으로 "
            f"{duration}년간 {direction}했습니다. [출처: 내부 데이터마트]"
        )
        yearly = "연도별: " + " · ".join(
            f"{_mart_history_period(str(item['period']))} {item['value_억원']}억원"
            for item in selected
        )
        return f"{prose}\n{yearly}"
    return ""


def _mart_history_period(period: str) -> str:
    match = re.fullmatch(r"(\d{4})-(\d{2})", period)
    if match is None:
        return period
    return f"{match.group(1)}년 {int(match.group(2))}월"


def _mart_history_year_span(first_period: str, last_period: str) -> int:
    first_year = int(first_period[:4]) if first_period[:4].isdigit() else 0
    last_year = int(last_period[:4]) if last_period[:4].isdigit() else first_year
    return max(0, last_year - first_year)


def _mart_history_direction(first_value: str, last_value: str) -> str:
    try:
        first = Decimal(first_value.replace(",", ""))
        last = Decimal(last_value.replace(",", ""))
    except InvalidOperation:
        return "변화"
    if last > first:
        return "증가"
    if last < first:
        return "감소"
    return "유지"


def _hira_patient_facts(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("calls"), list):
        return ()
    disease_code = ""
    disease_name = ""
    yearly: dict[str, list[str]] = {}
    public_fields = (
        ("ptntCnt", "환자수는", "명(청구 실인원)"),
        ("rvdInsupBrdnAmt", "보험자부담금", "원"),
        ("rvdRpeTamtAmt", "요양급여비용총액", "원"),
        ("specCnt", "명세서건수", "건"),
        ("vstDdcnt", "내원일수", "일"),
    )
    for call in payload["calls"]:
        if not isinstance(call, Mapping):
            continue
        render = call.get("render_data")
        if not isinstance(render, Mapping):
            continue
        request = render.get("request") if isinstance(render.get("request"), Mapping) else {}
        year = str(request.get("year") or "")
        disease_code = disease_code or str(request.get("sickCd") or "")
        items = render.get("items")
        if not isinstance(items, list):
            continue
        for row in items:
            if not isinstance(row, Mapping):
                continue
            breakdown = row.get("sexBreakdown")
            nested_rows = (
                tuple(item for item in breakdown if isinstance(item, Mapping))
                if isinstance(breakdown, list) and breakdown
                else (row,)
            )
            for nested in nested_rows:
                resolved_row = {**row, **nested} if nested is not row else row
                disease_code = disease_code or str(resolved_row.get("sickCd") or "")
                disease_name = disease_name or str(resolved_row.get("sickNm") or "")
                row_year = year or str(resolved_row.get("year") or "")
                if not row_year or resolved_row.get("ptntCnt") in (None, ""):
                    continue
                care_type = hira_row_axis_label(resolved_row)
                values: list[str] = []
                for field, label, unit in public_fields:
                    raw_value = resolved_row.get(field)
                    if raw_value in (None, ""):
                        continue
                    source_units = resolved_row.get("units")
                    source_unit = (
                        str(source_units.get(field) or "")
                        if isinstance(source_units, Mapping)
                        else ""
                    )
                    try:
                        numeric = int(str(raw_value).replace(",", ""))
                        if field in {"rvdInsupBrdnAmt", "rvdRpeTamtAmt"} and source_unit != "원":
                            numeric *= 1000
                        display = f"{numeric:,}"
                    except ValueError:
                        display = str(raw_value)
                    values.append(f"{label} {display}{unit}")
                yearly.setdefault(row_year, []).append(f"{care_type} " + ", ".join(values))
    if not yearly:
        return ()
    subject = disease_code
    if disease_name:
        subject += f"({disease_name})" if subject else disease_name
    if subject:
        return tuple(
            f"{subject} {year}년 {', '.join(values)}으로 확인되었습니다."
            for year, values in sorted(yearly.items())
        )
    facts: list[str] = []
    for year, values in sorted(yearly.items()):
        labeled = list(values)
        facts.append(f"{year}년 {', '.join(labeled)}으로 확인되었습니다.")
    return tuple(facts)


def _safe_summaries(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        return ()
    candidates: list[str] = []
    direct = str(payload.get("summary_text") or "").strip()
    if direct:
        candidates.append(direct)
    calls = payload.get("calls")
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, Mapping):
                continue
            summary = str(call.get("summary_text") or "").strip()
            if summary:
                candidates.append(summary)
    return tuple(
        dict.fromkeys(
            summary for summary in candidates if not _INTERNAL_SURFACE_RE.search(summary)
        )
    )
