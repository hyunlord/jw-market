from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from jw_chat_agent_poc.service.conversation import ConversationTurn
from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.llm import GenOSV4Client


_INTERNAL_SURFACE_RE = re.compile(
    r"MCP\s+returned|\btotalCount\b|\bslot[_ -]?id\b|"
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
_DROP_KEYS = frozenset({"tool", "summary_text", "mcp", "totalCount", "elapsed_ms"})
_META_KEYS = frozenset({"request", "resultCode", "message", "status", "source", "safe_url"})


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
        usable = tuple(
            result
            for result in results
            if result.status == "ok"
            and _entity_match(result) != "MISMATCH"
            and (result.source != "web" or _web_has_citable_body(result.payload))
        )
        if not usable:
            return "이번 조회에서 확인된 근거가 없어 구체적인 답을 구성하지 못했습니다."

        messages = _synthesis_messages(plan, usable, turns)
        try:
            answer = self._client.complete(
                messages,
                budget_s=budget_s,
                max_tokens=1800,
            ).strip()
        except Exception:  # noqa: BLE001 - a grounded fallback is preferable to a 500
            answer = ""

        if answer and _INTERNAL_SURFACE_RE.search(answer):
            repair_messages = [
                *messages,
                {"role": "assistant", "content": answer},
                {
                    "role": "user",
                    "content": (
                        "내부 도구명, MCP 상태 문구, totalCount, slot id, 쉼표로 나열한 NCT 식별자를 "
                        "노출하지 말고 같은 근거로 자연스러운 답변을 다시 작성하라. 개별 임상 ID는 "
                        "시험명·단계·설명에 녹여 쓸 때만 허용한다."
                    ),
                },
            ]
            try:
                answer = self._client.complete(
                    repair_messages,
                    budget_s=min(6.0, budget_s),
                    max_tokens=1800,
                ).strip()
            except Exception:  # noqa: BLE001 - deterministic surface replacement follows
                answer = ""

        if not answer:
            answer = _evidence_fallback(usable)
        elif _INTERNAL_SURFACE_RE.search(answer):
            answer = _replace_internal_blocks(answer, usable)
        return _append_automatic_footnotes(answer, usable)


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
                "식별자 목록을 노출하지 않는다. <INTERNAL_DATAMART> 안의 숫자와 표기는 한 글자도 바꾸지 않는다. "
                "단위 환산, 반올림, 계산, 합산을 금지하며 UBIST와 IQVIA를 합산하지 않는다. MISMATCH 근거는 이미 "
                "제외됐고 PARTIAL, US, 기간 불일치는 한계를 본문에 명시한다. 출력은 핵심 답, 근거와 맥락, 종합 "
                "인사이트, 미확인 요소 순서로 구성하되 불필요한 고정 제목은 쓰지 않는다."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(prompt, ensure_ascii=False, default=str),
        },
    ]


def _evidence_packet(result: SourceResult) -> dict[str, Any]:
    return {
        "source": _PUBLIC_SOURCE[result.source],
        "query": result.query,
        "entity_match": _entity_match(result),
        "source_scope": _SOURCE_SCOPE[result.source],
        "time_match": _time_match(result),
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
                calls.append(
                    {
                        key: _public_payload(value)
                        for key, value in call.items()
                        if key not in _DROP_KEYS and value not in (None, "", [], {})
                    }
                )
            return {"calls": calls}
        return {
            str(key): _public_payload(value)
            for key, value in payload.items()
            if key not in _DROP_KEYS
        }
    if isinstance(payload, (list, tuple)):
        return [_public_payload(value) for value in payload]
    return payload


def _bounded_value(value: Any, *, query: str, depth: int = 0) -> Any:
    if depth >= 8:
        return "[nested detail omitted]"
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_value(item, query=query, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        matching = [item for item in value if _matches_requested_anchor(item, query)]
        selected = matching + [item for item in value if item not in matching]
        return [_bounded_value(item, query=query, depth=depth + 1) for item in selected[:20]]
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + " [excerpt]"
    return value


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


def _evidence_fallback(results: Sequence[SourceResult]) -> str:
    paragraphs: list[str] = []
    for result in results:
        summaries = _safe_summaries(result.payload)
        if summaries:
            paragraphs.append(
                " ".join(summaries) + f" [출처: {_PUBLIC_SOURCE[result.source]}]"
            )
            continue
        facts = []
        for path, value in _walk_scalars(_public_payload(result.payload)):
            label = path.rsplit(".", 1)[-1]
            if label in _META_KEYS or value in (None, "", [], {}):
                continue
            if isinstance(value, (str, int, float)):
                facts.append(f"{label} {value}")
            if len(facts) >= 12:
                break
        if facts:
            paragraphs.append(
                f"{_PUBLIC_SOURCE[result.source]}에서 " + ", ".join(facts) + "이 확인되었습니다. "
                f"[출처: {_PUBLIC_SOURCE[result.source]}]"
            )
    if not paragraphs:
        return "조회는 완료됐지만 답변 본문에 제시할 수 있는 상세 근거를 확인하지 못했습니다."
    return "\n\n".join(paragraphs)


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
