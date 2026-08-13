from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
import re
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult


_KCD_RANGE_RE = re.compile(
    r"(?<![A-Z0-9])(?P<prefix>[A-Z])(?P<start>\d{2})\s*(?:~|～|부터|[-–—])\s*"
    r"(?:(?P=prefix))?(?P<end>\d{2})(?![A-Z0-9])",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})\s*년?")
_RECENT_YEARS_RE = re.compile(r"최근\s*(?P<count>\d{1,2})\s*년")
_PRODUCT_KEYS = frozenset(
    {
        "item_name",
        "product_name",
        "product",
        "brand",
        "brand_name",
        "품목명",
        "제품명",
        "브랜드",
    }
)
_QUERY_SUFFIX = {
    "mart": "내부 시장 데이터",
    "nedrug": "허가 정보",
    "hira": "급여 및 환자 통계",
    "openfda": "미국 허가 및 안전성",
    "clinicaltrials": "임상현황",
    "web": "공개 자료",
    "patent": "특허현황",
}


@dataclass(frozen=True, slots=True)
class ExpansionOutcome:
    plan: PlannerOutput
    trace: dict[str, Any]


def expand_parameter_axes(
    plan: PlannerOutput,
    question: str,
    *,
    observed_on: date,
) -> ExpansionOutcome:
    codes = _kcd_codes(question)
    years = _years(question, observed_on)
    updates: dict[str, tuple[str, ...]] = {}
    if codes:
        base = _query_subject(question, codes, years) or "환자수"
        if years:
            updates["hira"] = tuple(
                f"{code} {base} {year}년" for code in codes for year in years
            )
        else:
            updates["hira"] = tuple(f"{code} {base}" for code in codes)
    elif len(years) > 1:
        for source in plan.answer_sources:
            queries = getattr(plan.tool_queries, source)
            updates[source] = tuple(
                dict.fromkeys(
                    f"{_strip_years(query)} {year}년".strip()
                    for query in queries
                    for year in years
                )
            )
    expanded = (
        plan.model_copy(
            update={"tool_queries": plan.tool_queries.model_copy(update=updates)}
        )
        if updates
        else plan
    )
    return ExpansionOutcome(
        plan=expanded,
        trace={
            "status": "expanded" if updates else "not_applicable",
            "axes": {"kcd_codes": list(codes), "years": list(years)},
            "requests": {
                source: list(queries) for source, queries in sorted(updates.items())
            },
            "deterministic": True,
        },
    )


def build_second_hop_expansion(
    plan: PlannerOutput,
    question: str,
    results: Sequence[SourceResult],
    *,
    max_queries: int = 3,
) -> ExpansionOutcome | None:
    if max_queries <= 0:
        return None
    observed: list[tuple[str, str]] = []
    for result in results:
        if result.status != "ok":
            continue
        for value in _product_values(result.payload):
            if value.casefold() in question.casefold():
                continue
            pair = (value, result.source)
            if pair not in observed:
                observed.append(pair)
    if not observed:
        return None
    selected = observed[:max_queries]
    updates = {
        source: tuple(
            f"{entity} {_QUERY_SUFFIX[source]}"
            for entity, _origin in selected
        )
        for source in plan.answer_sources
    }
    expanded = plan.model_copy(
        update={
            "tool_queries": plan.tool_queries.model_copy(update=updates),
            "needs_second_hop": False,
            "requested_answer_shape": plan.requested_answer_shape.model_copy(
                update={"entities": tuple(entity for entity, _origin in selected)}
            ),
        }
    )
    return ExpansionOutcome(
        plan=expanded,
        trace={
            "status": "expanded",
            "source": selected[0][1],
            "entities": [entity for entity, _origin in selected],
            "requests": {
                source: list(queries) for source, queries in sorted(updates.items())
            },
            "candidate_count": len(observed),
            "truncated_count": max(0, len(observed) - len(selected)),
            "max_queries": max_queries,
            "deterministic": True,
        },
    )


def _kcd_codes(question: str) -> tuple[str, ...]:
    match = _KCD_RANGE_RE.search(question.upper())
    if match is None:
        return ()
    start = int(match.group("start"))
    end = int(match.group("end"))
    if end < start or end - start > 20:
        return ()
    prefix = match.group("prefix").upper()
    return tuple(f"{prefix}{value:02d}" for value in range(start, end + 1))


def _years(question: str, observed_on: date) -> tuple[int, ...]:
    explicit = tuple(dict.fromkeys(int(value) for value in _YEAR_RE.findall(question)))
    if explicit:
        return explicit
    recent = _RECENT_YEARS_RE.search(question)
    if recent is None:
        return ()
    count = min(max(int(recent.group("count")), 1), 10)
    return tuple(range(observed_on.year - count + 1, observed_on.year + 1))


def _query_subject(
    question: str,
    codes: Sequence[str],
    years: Sequence[int],
) -> str:
    value = _KCD_RANGE_RE.sub(" ", question.upper(), count=1)
    for code in codes:
        value = value.replace(code, " ")
    for year in years:
        value = re.sub(rf"(?<!\d){year}\s*년?", " ", value)
    value = re.sub(r"(?:^|\s)(?:과|와|및)(?=\s|$)", " ", value)
    value = re.sub(r"\b(?:비교|알려줘|보여줘)\b", " ", value, flags=re.IGNORECASE)
    return " ".join(value.split()).strip()


def _strip_years(value: str) -> str:
    return " ".join(_YEAR_RE.sub(" ", value).split())


def _product_values(value: Any) -> tuple[str, ...]:
    output: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if str(key).casefold() in _PRODUCT_KEYS and isinstance(nested, str):
                    cleaned = " ".join(nested.split())
                    if 1 < len(cleaned) <= 80 and cleaned not in output:
                        output.append(cleaned)
                else:
                    visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(output)
