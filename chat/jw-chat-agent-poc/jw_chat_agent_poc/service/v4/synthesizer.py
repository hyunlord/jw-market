from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jw_chat_agent_poc.service.conversation import ConversationTurn
from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.llm import CompletionResult, GenOSV4Client


_INTERNAL_SURFACE_RE = re.compile(
    r"MCP(?:[^가-힣\n]{0,80})?(?:에서|returned|결과)|\btotalCount\b|\bslot[_ -]?id\b|"
    r"\b(?:sickCd|ptntCnt|value)\b|"
    r"\b\d{7,}(?:\.\d+)?\s*KRW(?![A-Za-z])|"
    r"\b\d{7,}(?:\.\d+)?\s*원(?:은|는|이|가|을|를|으로|에서|의)?|"
    r"\b[A-Z][A-Z0-9_]{2,}\s*[:=]\s*[^\s,;]+|"
    r"\b(?:hira|clinicaltrials|mfds|openfda|tavily)_[a-z0-9_]+\b|"
    r"(?:\bNCT\d{8}\b\s*[,/]\s*)+\bNCT\d{8}\b",
    re.IGNORECASE,
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
_DROP_KEYS = frozenset(
    {
        "tool",
        "summary_text",
        "mcp",
        "totalcount",
        "elapsed_ms",
        "sickcd",
        "ptntcnt",
        "value",
        "item_seq",
        "entp_seq",
        "prdlst_stdr_code",
    }
)
_PUBLIC_FIELD_ALIASES = {
    "CHANGE_DATE": "change_date",
    "ENTP_NAME": "manufacturer",
    "ITEM_INGR_NAME": "ingredient",
    "ITEM_NAME": "product_name",
    "ITEM_PERMIT_DATE": "permit_date",
    "REEXAM_DATE": "reexamination_period",
    "REEXAM_TARGET": "reexamination_target",
}
_BOUNDED_PRIORITY_KEYS = (
    "brand",
    "period",
    "source_label",
    "sales_억원",
    "market_size_억원",
    "brand_value_series_10pt",
    "market_size_series",
    "level_top5_trend_series",
    "series_insight",
    "request",
    "items",
)
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
        budget_s: float = 15.0,
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
        budget_s: float = 24.0,
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

        if answer and _INTERNAL_SURFACE_RE.search(answer):
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
            answer = _evidence_fallback(usable)
        elif _INTERNAL_SURFACE_RE.search(answer):
            answer = _replace_internal_blocks(answer, usable)
        answer = _finalize_answer(answer, usable)
        return SynthesisOutcome(
            text=answer,
            trace={
                "status": "fallback" if fallback_reason else "synthesized",
                "finish_reason": completion.finish_reason if completion else None,
                "usage": completion.usage if completion else {},
                "elapsed_ms": completion.elapsed_ms if completion else None,
                "prompt_chars": sum(len(message["content"]) for message in messages),
                "raw_payload_chars": sum(
                    len(json.dumps(result.payload, ensure_ascii=False, default=str))
                    for result in usable
                ),
                "coverage_notices": list(_coverage_notices(usable)),
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
    return [
        {
            "role": "system",
            "content": (
                "너는 JW MI팀의 CHAT-V4 답변 합성기다. 질문이 묻는 값이나 내용을 첫 문단에서 직접 답하고, "
                "직접 관련 없는 근거는 뒤로 보내거나 생략한다. 도구 로그를 나열하지 말고 근거를 연결한 자연스러운 "
                "한국어 줄글로 작성한다. 사실은 '~로 확인되었습니다' 또는 '~입니다'로 쓰고 문장 끝에 [출처: X]를 "
                "붙인다. 해석은 '~로 해석될 수 있습니다' 또는 '~할 것으로 추정됩니다'로 구분하며 근거에 없는 숫자를 "
                "만들지 않는다. 못 찾은 부분만 마지막 한 줄에 적는다. 내부 도구명, MCP 상태 문구, totalCount, slot id, "
                "식별자 목록과 대문자 레코드 필드명을 노출하지 않는다. <INTERNAL_DATAMART> 안의 숫자와 표기는 한 글자도 바꾸지 않는다. "
                "단위 환산, 반올림, 계산, 합산을 금지하며 UBIST와 IQVIA를 합산하지 않는다. MISMATCH 근거는 이미 "
                "제외됐고 PARTIAL, US, 기간 불일치는 한계를 본문에 명시한다. evidence.eligible_claims에 없는 주장에는 "
                "그 근거를 사용하지 않는다. 관찰연구, 기기 연구, 인접 질환, 질문과 다른 모집상태의 연구는 핵심 답이 "
                "아니라 `참고: 인접 연구` 구획에만 둔다. causal=false 근거만으로 원인을 확인했다고 쓰지 말고 관찰 사실과 "
                "가설을 분리한다. HIRA 입원과 외래 환자수는 중복 가능하므로 합산하거나 비율을 계산하지 않는다. "
                "반드시 `## 핵심 답`, `## 근거와 맥락`, "
                "`## 종합 인사이트`, `## 미확인 요소`, `## 출처`의 마크다운 소제목으로 구성한다. 한 문단은 최대 4문장으로 "
                "쓰고, 고시·허가사항은 투여대상·제외기준·투여방법·투여횟수처럼 의미 단위 불릿으로 요약한다. "
                "근거 본문은 활용하되 다운로드 안내문이나 담당부서 연락 안내는 답변에 복사하지 않는다."
                " gap_fill로 표시된 웹 근거는 공식 통계 표나 시계열에 섞지 말고 별도 문단에서 "
                "'공식 통계 아님'을 밝혀 서술한다. TIER1 또는 TIER2가 아닌 웹 정량값은 쓰지 않는다. "
                "제네릭처럼 하위 제품 집합을 묻는 질문에서는 그 집합이 근거에 없을 때 본품이나 상위 제품의 "
                "수치를 대신 답하지 않고, 요청 집합의 값을 확인하지 못했다고 먼저 밝힌다."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(prompt, ensure_ascii=False, default=str),
        },
    ]


def _evidence_packet(result: SourceResult) -> dict[str, Any]:
    evidence = result.evidence
    return {
        "source": _PUBLIC_SOURCE[result.source],
        "query": result.query,
        "evidence": evidence.model_dump(mode="json") if evidence else {
            "entity_match": _entity_match(result),
            "source_scope": _SOURCE_SCOPE[result.source],
            "time_match": _time_match(result),
        },
        "detail": _bounded_value(_public_payload(result.payload), query=result.query),
    }


def _mart_block(result: SourceResult) -> str:
    payload = _bounded_value(_public_payload(result.payload), query=result.query)
    tables = _markdown_tables(payload)
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    body = "\n\n".join((*tables, f"원형 JSON: {raw}"))
    return f"<INTERNAL_DATAMART source=\"{_PUBLIC_SOURCE[result.source]}\">\n{body}\n</INTERNAL_DATAMART>"


def _public_payload(payload: Any) -> Any:
    if isinstance(payload, Mapping):
        if isinstance(payload.get("calls"), list):
            calls = []
            for call in payload["calls"]:
                if not isinstance(call, Mapping):
                    continue
                if str(call.get("status") or "").casefold() in {
                    "error",
                    "no_data",
                    "unsupported",
                }:
                    continue
                calls.append(
                    {
                        public_key: _public_payload(value)
                        for key, value in call.items()
                        if (public_key := _public_field_name(str(key))) is not None
                        and value not in (None, "", [], {})
                    }
                )
            return {"calls": calls}
        return {
            public_key: _public_payload(value)
            for key, value in payload.items()
            if (public_key := _public_field_name(str(key))) is not None
        }
    if isinstance(payload, (list, tuple)):
        return [_public_payload(value) for value in payload]
    return payload


def _bounded_value(value: Any, *, query: str, depth: int = 0) -> Any:
    if depth >= 8:
        return "[nested detail omitted]"
    if isinstance(value, Mapping):
        items = list(value.items())
        priority = [
            (key, value[key])
            for key in _BOUNDED_PRIORITY_KEYS
            if key in value
        ]
        selected_keys = {key for key, _item in priority}
        selected = (
            priority
            + [(key, item) for key, item in items if key not in selected_keys]
        )[:16]
        return {
            str(key): _bounded_value(item, query=query, depth=depth + 1)
            for key, item in selected
        }
    if isinstance(value, list):
        period_records = [item for item in value if _period_record(item)]
        if period_records:
            context_records = [item for item in value if not _period_record(item)][:1]
            return [
                _bounded_value(item, query=query, depth=depth + 1)
                for item in (*context_records, *period_records)
            ]
        matching = [item for item in value if _matches_requested_anchor(item, query)]
        selected = matching + [item for item in value if item not in matching]
        return [_bounded_value(item, query=query, depth=depth + 1) for item in selected[:4]]
    return value


def _period_record(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if any(value.get(key) not in (None, "") for key in ("period", "year", "yyyymm")):
        return True
    render_data = value.get("render_data")
    if not isinstance(render_data, Mapping):
        return False
    request = render_data.get("request")
    return isinstance(request, Mapping) and bool(request.get("year"))


def _public_key(key: str) -> bool:
    return key.casefold() not in _DROP_KEYS and re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", key) is None


def _public_field_name(key: str) -> str | None:
    if key in _PUBLIC_FIELD_ALIASES:
        return _PUBLIC_FIELD_ALIASES[key]
    return key if _public_key(key) else None


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


def _matches_requested_anchor(value: Any, query: str) -> bool:
    anchors = set(re.findall(r"NCT\d{8}|(?:19|20)\d{2}|[A-Z]\d{2,3}", query, re.IGNORECASE))
    if not anchors:
        return False
    serialized = json.dumps(value, ensure_ascii=False, default=str).casefold()
    return any(anchor.casefold() in serialized for anchor in anchors)


def _markdown_tables(value: Any) -> tuple[str, ...]:
    tables: list[str] = []
    for rows in _dict_lists(value):
        columns = tuple(dict.fromkeys(str(key) for row in rows for key in row))[:12]
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


def _evidence_fallback(results: Sequence[SourceResult]) -> str:
    paragraphs: list[str] = []
    for result in results:
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


def _hira_patient_facts(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("calls"), list):
        return ()
    disease_code = ""
    disease_name = ""
    yearly: dict[str, list[str]] = {}
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
            patient_count = row.get("ptntCnt")
            if not row_year or patient_count in (None, ""):
                continue
            try:
                count = f"{int(str(patient_count).replace(',', '')):,}"
            except ValueError:
                count = str(patient_count)
            care_type = str(row.get("inpatOpat") or "환자")
            yearly.setdefault(row_year, []).append(f"{care_type} {count}명")
    if not yearly:
        return ()
    subject = disease_code
    if disease_name:
        subject += f"({disease_name})" if subject else disease_name
    if subject:
        return tuple(
            f"{subject} 환자수는 {year}년 {', '.join(values)}으로 확인되었습니다."
            for year, values in sorted(yearly.items())
        )
    facts: list[str] = []
    for year, values in sorted(yearly.items()):
        labeled = []
        for value in values:
            care_type, count = value.split(" ", 1)
            labeled.append(f"{care_type} 환자수는 {count}")
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
