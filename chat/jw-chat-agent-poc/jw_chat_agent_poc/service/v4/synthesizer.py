from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from jw_chat_agent_poc.service.conversation import ConversationTurn
from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.gates import (
    inspect_requested_hira_surface,
    render_mart_dimension_facts,
)
from jw_chat_agent_poc.service.v4.llm import CompletionResult, GenOSV4Client


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
_PUBLIC_SOURCE = {
    "mart": "내부 데이터마트",
    "nedrug": "식품의약품안전처",
    "hira": "HIRA",
    "openfda": "FDA",
    "clinicaltrials": "ClinicalTrials.gov",
    "web": "웹 자료",
    "patent": "특허 자료",
}
_SOURCE_SCOPE = {
    "mart": "KR",
    "nedrug": "KR",
    "hira": "KR",
    "openfda": "US",
    "clinicaltrials": "GLOBAL",
    "web": "GLOBAL",
    "patent": "GLOBAL",
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
    "붙은 값과 단위를 그대로 쓴다. 반드시 `## 핵심 답`, `## 근거와 맥락`, `## 종합 인사이트`, "
    "`## 미확인 요소`, `## 출처`의 마크다운 소제목으로 구성한다. 한 문단은 최대 4문장으로 쓰고, "
    "고시·허가사항은 투여대상·제외기준·투여방법·투여횟수처럼 의미 단위 불릿으로 요약한다. 근거 본문은 "
    "활용하되 다운로드 안내문이나 담당부서 연락 안내는 답변에 복사하지 않는다. gap_fill로 표시된 웹 근거는 "
    "공식 통계 표나 시계열에 섞지 말고 별도 문단에서 '공식 통계 아님'을 밝혀 서술한다. TIER1 또는 TIER2가 "
    "아닌 웹 정량값은 쓰지 않는다. 제네릭처럼 하위 제품 집합을 묻는 질문에서는 그 집합이 근거에 없을 때 "
    "본품이나 상위 제품의 수치를 대신 답하지 않고 요청 집합의 값을 확인하지 못했다고 먼저 밝힌다."
    " `required_hira_surface`가 있으면 모든 항목을 첫 합성에서 본문에 정확히 포함한다."
)
_CAUSE_MARKERS = ("원인", "왜 ", "이유")


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
    ) -> str:
        return self.synthesize_with_trace(
            plan,
            results,
            turns,
            budget_s=budget_s,
        ).text

    def synthesize_with_trace(
        self,
        plan: PlannerOutput,
        results: Sequence[SourceResult],
        turns: Sequence[ConversationTurn],
        *,
        budget_s: float = 60.0,
    ) -> SynthesisOutcome:
        usable = _select_usable_results(plan, tuple(
            result
            for result in results
            if result.status == "ok"
            and _entity_match(result) != "MISMATCH"
            and (result.source != "web" or _web_has_citable_body(result.payload))
        ))
        if not usable:
            return SynthesisOutcome(
                text="이번 조회에서 확인된 근거가 없어 구체적인 답을 구성하지 못했습니다.",
                trace={
                    "status": "no_usable_evidence",
                    "fallback_reason": "no_evidence",
                    "serving_id": "not_applicable",
                    "model": "not_applicable",
                },
            )

        messages = _synthesis_messages(plan, usable, turns)
        completion: CompletionResult | None = None
        error_type: str | None = None
        try:
            completion = _complete_detailed(
                self._client,
                messages,
                budget_s=budget_s,
                max_tokens=8192,
            )
            answer = completion.text.strip()
        except Exception as exc:  # noqa: BLE001 - a grounded fallback is preferable to a 500
            answer = ""
            error_type = type(exc).__name__

        fallback_reason: str | None = None
        if completion is not None and completion.finish_reason == "length":
            answer = ""
            fallback_reason = "length"
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
            answer = _evidence_fallback(
                usable,
                question=plan.resolved_question,
            )
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
                    max_tokens=8192,
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
        answer = _finalize_answer(answer, usable)
        return SynthesisOutcome(
            text=answer,
            trace={
                "status": "fallback" if fallback_reason else "synthesized",
                "finish_reason": completion.finish_reason if completion else None,
                "usage": completion.usage if completion else {},
                "elapsed_ms": completion.elapsed_ms if completion else None,
                "prompt_chars": sum(len(message["content"]) for message in messages),
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
                "serving_id": completion.serving_id if completion else "not_applicable",
                "model": completion.model if completion else "not_applicable",
            },
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


def _synthesis_messages(
    plan: PlannerOutput,
    results: Sequence[SourceResult],
    turns: Sequence[ConversationTurn],
) -> list[dict[str, str]]:
    mart = tuple(result for result in results if result.source == "mart")
    external = tuple(result for result in results if result.source != "mart")
    history = [
        {"question": turn.question, "answer": turn.answer}
        for turn in tuple(turns)[-3:]
    ]
    asks_cause = any(marker in plan.resolved_question.casefold() for marker in _CAUSE_MARKERS)
    prompt = {
        "internal_datamart": [_mart_block(result) for result in mart],
        "external_evidence": [_evidence_packet(result) for result in external],
        "source_mapping": [
            {
                "source": _PUBLIC_SOURCE[result.source],
                "url": citation.url,
                "retrieved_at": citation.retrieved_at.isoformat(),
            }
            for result in results
            for citation in result.citations
        ],
        "recent_turns": history,
        "resolved_intents": list(plan.expanded_intents),
        "user_question": plan.resolved_question,
        "output_guide": [
            "핵심 답을 첫 문단에서 바로 제시",
            "근거와 맥락",
            "종합 인사이트",
            "미확인 요소 한 줄",
            "출처는 본문 문장 끝에 [출처: 이름]으로 표시",
        ],
    }
    required_hira_surface = _required_hira_surface(plan.resolved_question, results)
    if required_hira_surface:
        prompt["required_hira_surface"] = required_hira_surface
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


def _evidence_packet(result: SourceResult) -> dict[str, Any]:
    evidence = result.evidence
    packet = {
        "source": _PUBLIC_SOURCE[result.source],
        "query": result.query,
        "evidence": evidence.model_dump(mode="json") if evidence else {
            "entity_match": _entity_match(result),
            "source_scope": _SOURCE_SCOPE[result.source],
            "time_match": _time_match(result),
        },
        "detail": result.payload,
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
    payload = _public_mart_payload(result.payload)
    tables = _markdown_tables(payload)
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    body = "\n\n".join((*tables, f"원형 JSON: {raw}"))
    return f"<INTERNAL_DATAMART source=\"{_PUBLIC_SOURCE[result.source]}\">\n{body}\n</INTERNAL_DATAMART>"


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
    selected: list[SourceResult] = []
    counts: dict[str, int] = {}
    for result in sorted(
        results,
        key=lambda item: (item.source not in plan.answer_sources, SOURCE_ORDER[item.source]),
    ):
        limit = 2 if result.source in plan.answer_sources else 1
        if counts.get(result.source, 0) >= limit:
            continue
        counts[result.source] = counts.get(result.source, 0) + 1
        selected.append(result)
    return tuple(selected)


SOURCE_ORDER = {
    "mart": 0,
    "nedrug": 1,
    "hira": 2,
    "openfda": 3,
    "clinicaltrials": 4,
    "web": 5,
    "patent": 6,
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
    answer = _append_automatic_footnotes(answer, results)
    return answer


def _coverage_notices(results: Sequence[SourceResult]) -> tuple[str, ...]:
    notices: list[str] = []
    for result in results:
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


def _evidence_fallback(
    results: Sequence[SourceResult],
    *,
    question: str = "",
) -> str:
    paragraphs: list[str] = []
    for result in results:
        if result.source == "mart":
            dimensions = render_mart_dimension_facts(
                (result,),
                question=question or result.query,
            )
            history = _mart_history_fallback(
                result.payload,
                question=question or result.query,
            )
            if dimensions or history:
                paragraphs.extend(block for block in (dimensions, history) if block)
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
            care_type = str(row.get("inpatOpat") or "환자")
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
