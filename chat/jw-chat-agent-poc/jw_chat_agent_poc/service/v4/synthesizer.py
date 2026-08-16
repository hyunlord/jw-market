from __future__ import annotations

import hashlib
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
from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.gates import (
    hira_row_axis_label,
    inspect_requested_hira_surface,
    render_mart_dimension_facts,
)
from jw_chat_agent_poc.service.v4.llm import (
    CompletionResult,
    CompletionTransportError,
    GenOSV4Client,
    thinking_observability,
)
from jw_chat_agent_poc.service.v4.reason_code_enforcement import typed_absence_record
from jw_chat_agent_poc.service.v4.session_state import SessionState
from jw_chat_agent_poc.service.v4.synthesis_policy import (
    SynthesisPolicy,
    bound_synthesis_messages,
)
from jw_chat_agent_poc.service.v4.source_labels import (
    SOURCE_LABELS as _PUBLIC_SOURCE,
)
from jw_chat_agent_poc.service.v4.time_context import (
    as_of_date_instruction,
    current_kst_date as _current_kst_date,
)


LOGGER = logging.getLogger(__name__)

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
_SOURCE_SCOPE = {
    "mart": "KR",
    "nedrug": "KR",
    "hira": "KR",
    "openfda": "US",
    "clinicaltrials": "GLOBAL",
    "web": "GLOBAL",
    "patent": "GLOBAL",
    "document": "KR",
}
_FOOTNOTES = {
    "hira": "HIRA 환자수는 주상병 기준 청구 실인원이며 유병률과 다릅니다.",
    "openfda": "FAERS/OpenFDA는 자발적 보고 자료로 인과관계나 발생률 산출에 쓸 수 없습니다.",
    "clinicaltrials": "ClinicalTrials.gov 모집상태는 갱신이 지연될 수 있습니다.",
    "patent": "특허 존속기간 만료가 곧 제네릭 진입 시점을 뜻하지 않습니다.",
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
    "한국어 줄글로 작성한다. 사실은 '~로 확인되었습니다' 또는 '~입니다'로 쓰고 문장 끝에 [출처: X]를 "
    "붙인다. 해석은 '~로 해석될 수 있습니다' 또는 '~할 것으로 추정됩니다'로 구분하며 근거에 없는 숫자를 "
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
    "합산하거나 비율을 계산하지 않는다. study_classification에서 ADJACENT로 표시된 임상은 `참고: 인접 연구` "
    "구획에만 두고 인접 연구를 종합 인사이트에서 다시 요약하거나 해석하지 않는다. HIRA 환자수는 `환자수(명)` "
    "값만 사용한다. `명세서건수(건)`이나 `방문일수(일)`를 환자수로 바꾸어 쓰지 않고, 금액은 `(원)` 라벨이 "
    "붙은 값과 단위를 그대로 쓴다. 질문에 대한 답을 첫 문장에서 바로 제시한다. 출처별 소제목이나 "
    "고정된 섹션 수를 강제하지 말고, 질문의 논리에 맞는 소제목만 한 번씩 사용한다. 확인되지 않은 내용이 "
    "있을 때만 명시적인 확인 한계를 마지막에 둔다. 내용이 없는 소제목은 만들지 않는다. 같은 주어와 기간을 "
    "되풀이하는 선두 문장을 만들지 않는다. 렌더 대상 레코드가 5건 이상이면 표를 제외한 서술을 1,500자 이상으로 "
    "작성하되, 근거 없는 수식어로 분량을 채우지 않는다. "
    "결정론적 사실면 표는 전건 보존용이므로 표의 행을 해설에 다시 나열하지 말고, 표가 뜻하는 맥락과 시사점을 "
    "충분한 길이의 자연스러운 문장으로 연결한다. 결정론적 사실면의 [직접 확인] 레코드 관계는 코드가 "
    "재계산한 주장만 포함하므로 서술에 반영하되, 그 목록에 없는 레코드 간 관계를 새로 만들지 않는다. "
    "핵심 답은 질문에 직접 답하고, 근거와 맥락은 사실 간 관계를, "
    "종합 인사이트는 의사결정상 함의를 설명한다. 한 문단은 최대 4문장으로 쓰고, "
    "고시·허가사항은 투여대상·제외기준·투여방법·투여횟수처럼 의미 단위 불릿으로 요약한다. 근거 본문은 "
    "활용하되 다운로드 안내문이나 담당부서 연락 안내는 답변에 복사하지 않는다. gap_fill로 표시된 웹 근거는 "
    "공식 통계 표나 시계열에 섞지 말고 별도 문단에서 '공식 통계 아님'을 밝혀 서술한다. TIER1 또는 TIER2가 "
    "아닌 웹 정량값은 쓰지 않는다. 제네릭처럼 하위 제품 집합을 묻는 질문에서는 그 집합이 근거에 없을 때 "
    "본품이나 상위 제품의 수치를 대신 답하지 않고 요청 집합의 값을 확인하지 못했다고 먼저 밝힌다."
    " `required_hira_surface`가 있으면 모든 항목을 첫 합성에서 본문에 정확히 포함한다."
)
_CAUSE_MARKERS = ("원인", "왜 ", "이유")
_DEFAULT_SYNTHESIS_MAX_TOKENS = 16384
_MIN_SYNTHESIS_MAX_TOKENS = 8192
_MAX_SYNTHESIS_MAX_TOKENS = 32768


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
    ) -> str:
        return self.synthesize_with_trace(
            plan,
            results,
            turns,
            budget_s=budget_s,
            state=state,
            deterministic_facts=deterministic_facts,
        ).text

    def synthesize_with_trace(
        self,
        plan: PlannerOutput,
        results: Sequence[SourceResult],
        turns: Sequence[ConversationTurn],
        *,
        budget_s: float = 60.0,
        state: SessionState | None = None,
        deterministic_facts: str | None = None,
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
        )
        try:
            messages, prompt_bound_trace = bound_synthesis_messages(
                messages,
                char_limit=SynthesisPolicy.from_env().prompt_char_limit,
            )
        except Exception as exc:  # noqa: BLE001 - bounding is an optimisation, never a gate
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
        hira_retry_attempted = bool(hira_surface["missing"] and completion is not None)
        hira_retry_error_type: str | None = None
        if hira_retry_attempted:
            retry_messages = [
                *messages,
                {"role": "assistant", "content": answer},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "instruction": (
                                "HIRA 요청 지표 결속 검사에서 누락이 발견됐다. 원형 detail을 다시 읽고 "
                                "요청 연도와 입원/외래 구분마다 아래 값을 정확히 본문에 써라. "
                                "환자수는 환자수(명) 값만 쓰고 명세서건수(건)를 환자수로 쓰지 마라. "
                                "금액과 방문일수도 표시된 단위를 유지하라."
                            ),
                            "missing": [
                                {
                                    "year": fact.year,
                                    "care_type": fact.care_type,
                                    "metric": fact.label,
                                    "value": fact.display,
                                }
                                for fact in hira_surface["missing"]
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
            try:
                retried = _complete_detailed(
                    self._client,
                    retry_messages,
                    budget_s=min(30.0, budget_s),
                    max_tokens=synthesis_max_tokens,
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
        # After _finalize_answer on purpose: that step truncates at a model-owned
        # "## 출처" heading, which would swallow anything appended before it.
        answer, market_surface_trace = _inject_deterministic_market_surface(
            answer,
            usable,
            question=plan.resolved_question,
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
                    "error_type": hira_retry_error_type,
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


def _complete_detailed(
    client: Any,
    messages: Sequence[dict[str, str]],
    *,
    budget_s: float,
    max_tokens: int,
) -> CompletionResult:
    detailed = getattr(client, "complete_detailed", None)
    if callable(detailed):
        return detailed(messages, budget_s=budget_s, max_tokens=max_tokens)
    text = client.complete(messages, budget_s=budget_s, max_tokens=max_tokens)
    return CompletionResult(
        text=text,
        finish_reason="stop",
        usage={},
        elapsed_ms=0.0,
    )


def _complete_sentence_prefix(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return ""
    boundaries = tuple(
        match.end()
        for match in re.finditer(r"(?:[.!?。](?=\s|$)|(?:다|요|음|임)\.(?=\s|$))", cleaned)
    )
    return cleaned[: boundaries[-1]].strip() if boundaries else ""


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
) -> list[dict[str, str]]:
    mart = tuple(result for result in results if result.source == "mart")
    external = tuple(result for result in results if result.source != "mart")
    fact_backed = bool(deterministic_facts)
    history = [
        {"question": turn.question, "answer": turn.answer}
        for turn in tuple(turns)[-3:]
    ]
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
        "external_evidence": [
            _evidence_packet(result, include_detail=False)
            for result in external
        ],
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
            "핵심 답을 첫 문단에서 바로 제시",
            "근거와 맥락",
            "종합 인사이트",
            "미확인 요소 한 줄",
            "출처는 본문 문장 끝에 [출처: 이름]으로 표시",
        ],
    }
    if deterministic_facts:
        prompt["deterministic_facts"] = deterministic_facts
        prompt["deterministic_commentary_contract"] = {
            "facts_are_precomputed_and_rendered_before_commentary": True,
            "do_not_recalculate_or_rewrite_facts": True,
            "do_not_repeat_full_tables_or_source_documents": True,
            "commentary_scope": "해석과 맥락만 작성하며 근거에 없는 사실을 추가하지 않는다",
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
    return [
        {
            "role": "system",
            "content": _SYNTHESIS_SYSTEM_PROMPT,
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
            krw_per_eok = Decimal("100000000")
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
    return ((end - start) / start * Decimal("100")).quantize(
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
    source_text = ", ".join(dict.fromkeys(_PUBLIC_SOURCE[result.source] for result in results))
    replacement = f"해당 근거는 {source_text}에서 확인되었습니다."
    blocks = [replacement if _INTERNAL_SURFACE_RE.search(block) else block for block in re.split(r"\n\s*\n", answer)]
    return "\n\n".join(dict.fromkeys(block.strip() for block in blocks if block.strip()))


def _append_automatic_footnotes(answer: str, results: Sequence[SourceResult]) -> str:
    notes = tuple(dict.fromkeys(_FOOTNOTES[result.source] for result in results if result.source in _FOOTNOTES))
    if not notes:
        return answer
    missing = tuple(note for note in notes if note not in answer)
    return answer if not missing else f"{answer.rstrip()}\n\n" + "\n".join(f"- {note}" for note in missing)


def _finalize_answer(answer: str, results: Sequence[SourceResult]) -> str:
    # The final gate renders citations from typed results. Remove the model-owned
    # source block first so deterministic footnotes remain.
    if "## 출처" in answer:
        answer = answer.split("## 출처", 1)[0].rstrip()
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
    pending = [block for block in blocks if block.strip() and block.strip() not in answer]
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
            disease_code = disease_code or str(row.get("sickCd") or "")
            disease_name = disease_name or str(row.get("sickNm") or "")
            row_year = year or str(row.get("year") or "")
            if not row_year or row.get("ptntCnt") in (None, ""):
                continue
            care_type = hira_row_axis_label(row)
            values: list[str] = []
            for field, label, unit in public_fields:
                raw_value = row.get(field)
                if raw_value in (None, ""):
                    continue
                source_units = row.get("units")
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
        labeled = []
        for value in values:
            labeled.append(value)
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
