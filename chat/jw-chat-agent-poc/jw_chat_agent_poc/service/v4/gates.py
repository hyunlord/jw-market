from __future__ import annotations

import json
import re
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import GatedAnswer, SourceResult


_NUMBER_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
_RAW_WON_RE = re.compile(r"\b\d{7,}(?:\.\d+)?\s*원(?:은|는|이|가|을|를|으로|에서|의)?")
_VALUE = r"-?\d[\d,]*(?:\.\d+)?"
_MART_VALUE_PATTERNS: dict[str, re.Pattern[str]] = {
    "매출": re.compile(
        rf"(?:매출(?:액)?\s*(?:은|는|이|가|:)?\s*(?:약\s*)?(?:KRW\s*)?"
        rf"(?P<after>{_VALUE})|(?:KRW\s*)?(?P<before>{_VALUE})\s*(?:억원?|원|KRW))",
        re.IGNORECASE,
    ),
    "점유율": re.compile(
        rf"(?:점유율\s*(?:은|는|이|가|:)?\s*(?:약\s*)?(?P<after>{_VALUE})|"
        rf"(?P<before>{_VALUE})\s*(?:%|퍼센트))"
    ),
    "성장률": re.compile(
        rf"(?:성장률\s*(?:은|는|이|가|:)?\s*(?:약\s*)?(?P<after>{_VALUE})|"
        rf"(?P<before>{_VALUE})\s*(?:%|퍼센트))"
    ),
    "순위": re.compile(r"(?P<value>\d[\d,]*)\s*위"),
    "hhi": re.compile(r"(?:HHI\D{0,12}(?P<after>\d[\d,]*(?:\.\d+)?)|(?P<before>\d[\d,]*(?:\.\d+)?)\D{0,4}HHI)", re.IGNORECASE),
}
_MART_TERMS = ("매출", "점유율", "순위", "hhi", "성장률")
_METRIC_FIELDS: dict[str, tuple[str, ...]] = {
    "매출": ("sales", "amount", "value"),
    "점유율": ("share", "percentage", "percent", "pct"),
    "순위": ("rank",),
    "hhi": ("hhi",),
    "성장률": ("growth", "yoy", "cagr", "delta"),
}
_CONTEXT_FIELDS = ("period", "year", "month", "yyyymm")


def apply_v4_gates(
    question: str,
    answer: str,
    results: tuple[SourceResult, ...],
) -> GatedAnswer:
    trace: dict[str, Any] = {}
    text = answer.strip()
    mart_results = tuple(item for item in results if item.source == "mart" and item.status == "ok")

    requested_source = _requested_source(question)
    available_sources = _mart_source_labels(mart_results)
    if requested_source and requested_source not in available_sources:
        text = _without_numbers(text)
        text = _append_sentence(text, f"요청한 {requested_source} 근거를 확보하지 못했습니다. 다른 출처의 값을 {requested_source} 값으로 대체하지 않습니다.")
        trace["source_impersonation"] = {"blocked": True, "requested": requested_source}
    else:
        trace["source_impersonation"] = {"blocked": False}

    if _asks_cross_source_sum(question):
        text = _without_numbers(text)
        text = _append_sentence(text, "UBIST와 IQVIA는 측정 체계와 분모가 달라 합산하지 않습니다.")
        trace["cross_source_sum"] = {"blocked": True}
    else:
        trace["cross_source_sum"] = {"blocked": False}

    mart_numeric_question = any(term in question.casefold() for term in _MART_TERMS)
    metric_fields = _requested_metric_fields(question)
    allowed = _payload_numbers(
        mart_results if mart_numeric_question else results,
        allowed_fields=metric_fields if mart_numeric_question else (),
    )
    answer_numbers = _answer_mart_metric_numbers(text, question)
    invented = sorted(token for token in answer_numbers if _normalize_number(token) not in allowed)
    if invented and mart_results and mart_numeric_question:
        text = _render_mart_facts(mart_results, allowed_fields=metric_fields)
        trace["mart_numeric_copy_only"] = {"blocked": True, "tokens": invented}
    else:
        trace["mart_numeric_copy_only"] = {"blocked": False, "tokens": []}

    requested_display_numbers = _requested_display_numbers(mart_results, metric_fields)
    rendered_numbers = {_normalize_number(token) for token in _NUMBER_RE.findall(text)}
    metric_missing = bool(requested_display_numbers) and requested_display_numbers.isdisjoint(
        rendered_numbers
    )
    can_repair = not trace["source_impersonation"]["blocked"] and not trace[
        "cross_source_sum"
    ]["blocked"]
    if metric_missing and can_repair:
        verified_summary = _render_mart_facts(mart_results, allowed_fields=metric_fields)
        text = f"{verified_summary}\n\n{text}".strip()
    trace["requested_metric_surface"] = {
        "repaired": metric_missing and can_repair,
        "expected_display_numbers": sorted(requested_display_numbers),
    }

    timed_out = tuple(item for item in results if item.status == "timeout")
    if timed_out:
        delayed = ", ".join(dict.fromkeys(item.source for item in timed_out))
        text = _append_sentence(text, f"응답 지연으로 미포함: {delayed}")

    raw_won_blocked = bool(_RAW_WON_RE.search(text))
    if raw_won_blocked:
        retained = [
            block.strip()
            for block in re.split(r"\n\s*\n", text)
            if block.strip() and not _RAW_WON_RE.search(block)
        ]
        verified_summary = _render_mart_facts(mart_results, allowed_fields=metric_fields)
        text = "\n\n".join(dict.fromkeys((verified_summary, *retained)))
    trace["surface_raw_won"] = {"blocked": raw_won_blocked}

    text = _append_sources(text, results)
    trace["sources_block"] = {"present": "## 출처" in text}
    return GatedAnswer(text=text, trace=trace)


def _requested_source(question: str) -> str | None:
    lowered = question.casefold()
    if "iqvia" in lowered:
        return "IQVIA"
    if "ubist" in lowered:
        return "UBIST"
    return None


def _mart_source_labels(results: tuple[SourceResult, ...]) -> set[str]:
    labels: set[str] = set()
    for result in results:
        serialized = json.dumps(result.payload, ensure_ascii=False).upper()
        if "UBIST" in serialized:
            labels.add("UBIST")
        if "IQVIA" in serialized:
            labels.add("IQVIA")
    return labels


def _asks_cross_source_sum(question: str) -> bool:
    lowered = question.casefold()
    return "ubist" in lowered and "iqvia" in lowered and any(
        token in lowered for token in ("합쳐", "합산", "총매출", "더해")
    )


def _requested_metric_fields(question: str) -> tuple[str, ...]:
    lowered = question.casefold()
    fields = {
        field
        for term, field_names in _METRIC_FIELDS.items()
        if term in lowered
        for field in field_names
    }
    return tuple(sorted(fields | set(_CONTEXT_FIELDS)))


def _answer_mart_metric_numbers(text: str, question: str) -> set[str]:
    lowered = question.casefold()
    numbers: set[str] = set()
    for metric, pattern in _MART_VALUE_PATTERNS.items():
        if metric not in lowered:
            continue
        for match in pattern.finditer(text):
            value = match.groupdict().get("value")
            value = value or match.groupdict().get("after") or match.groupdict().get("before")
            if value:
                numbers.add(value)
    return numbers


def _payload_numbers(
    results: tuple[SourceResult, ...],
    *,
    allowed_fields: tuple[str, ...] = (),
) -> set[str]:
    tokens: set[str] = set()
    for result in results:
        if allowed_fields:
            values = (
                value
                for path, value in _walk_scalars(result.payload)
                if any(field in path.casefold() for field in allowed_fields)
            )
            serialized = json.dumps(tuple(values), ensure_ascii=False, default=str)
        else:
            serialized = json.dumps(result.payload, ensure_ascii=False, default=str)
        tokens.update(_normalize_number(token) for token in _NUMBER_RE.findall(serialized))
    return tokens


def _normalize_number(value: str) -> str:
    return value.replace(",", "").lstrip("+")


def _requested_display_numbers(
    results: tuple[SourceResult, ...],
    allowed_fields: tuple[str, ...],
) -> set[str]:
    numbers: set[str] = set()
    for result in results:
        for path, value in _walk_scalars(result.payload):
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            lowered = path.casefold()
            if not any(field in lowered for field in allowed_fields):
                continue
            leaf = path.rsplit(".", 1)[-1].casefold()
            if leaf in {"value", "amount"} or leaf.endswith("_value"):
                continue
            numbers.add(_normalize_number(str(value)))
    return numbers


def _without_numbers(text: str) -> str:
    lines = [line for line in text.splitlines() if not _NUMBER_RE.search(line)]
    return "\n".join(lines).strip()


def _render_mart_facts(
    results: tuple[SourceResult, ...],
    *,
    allowed_fields: tuple[str, ...],
) -> str:
    summaries: list[str] = []
    for result in results:
        calls = result.payload.get("calls") if isinstance(result.payload, dict) else None
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            summary = str(call.get("summary_text") or "").strip()
            if summary and not re.search(
                r"MCP\s+returned|\btotalCount\b|\b(?:sickCd|ptntCnt)\b|"
                r"\b\d{7,}(?:\.\d+)?\s*KRW\b",
                summary,
                re.IGNORECASE,
            ):
                summaries.append(summary)
    if summaries:
        return "\n".join(dict.fromkeys(summaries))

    facts: list[str] = []
    for result in results:
        for key, value in _walk_scalars(result.payload):
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            lowered = key.casefold()
            if not any(field in lowered for field in allowed_fields):
                continue
            leaf = key.rsplit(".", 1)[-1]
            if leaf.endswith("_억원") or leaf.endswith("_eok"):
                label = "매출" if "sales" in lowered or "value" in lowered else "금액"
                facts.append(f"{label} {value}억원")
    if not facts:
        return "mart 근거는 확인했지만 복사 가능한 수치 필드를 찾지 못했습니다."
    return "확인된 내부 데이터마트 지표는 " + ", ".join(dict.fromkeys(facts[:20])) + "입니다."


def _walk_scalars(value: Any, prefix: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_scalars(item, path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_scalars(item, f"{prefix}[{index}]")
    else:
        yield prefix, value


def _append_sentence(text: str, sentence: str) -> str:
    return f"{text}\n\n{sentence}".strip()


def _append_sources(text: str, results: tuple[SourceResult, ...]) -> str:
    if "## 출처" in text:
        text = text.split("## 출처", 1)[0].rstrip()
    lines: list[str] = []
    seen: set[tuple[str, str | None, str]] = set()
    for result in results:
        if result.status != "ok":
            continue
        if result.citations:
            for citation in result.citations:
                key = (citation.source, citation.url, citation.query)
                if key in seen:
                    continue
                seen.add(key)
                url = f" · {citation.url}" if citation.url else ""
                reuse = " · 이전 조회 재사용" if result.cache_hit else ""
                lines.append(
                    f"- {citation.source} · 조회 {citation.retrieved_at.isoformat()} · {citation.query}{url}{reuse}"
                )
        else:
            key = (result.source, None, result.query)
            if key not in seen:
                seen.add(key)
                reuse = " · 이전 조회 재사용" if result.cache_hit else ""
                lines.append(f"- {result.source} · {result.query}{reuse}")
    if not lines:
        lines.append("- 사용 가능한 출처를 확보하지 못했습니다.")
    return f"{text}\n\n## 출처\n" + "\n".join(lines)
