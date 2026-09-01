from __future__ import annotations

import os
import re
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import SOURCE_NAMES, QueryScope

AXIS_UNRELATED_QUERY_CAP_ENV = "AXIS_UNRELATED_QUERY_CAP"

_PATIENT_RE = re.compile(
    r"(?:환자\s*수|유병률|진료\s*인원|상병|(?<![A-Za-z0-9])[A-Za-z]\d{2}(?:\.?\d{1,2})?(?![A-Za-z0-9]))",
    re.IGNORECASE,
)
_MARKET_RE = re.compile(r"(?:매출|총액|sell\s*out|sellout|점유율|시장\s*규모)", re.IGNORECASE)
_PATENT_RE = re.compile(r"특허|patent", re.IGNORECASE)
_CLINICAL_RE = re.compile(r"임상|clinical\s*trial|\bNCT\d+", re.IGNORECASE)
_REGULATORY_RE = re.compile(r"허가|성분|효능|효과|재심사", re.IGNORECASE)
_SAFETY_RE = re.compile(r"안전성|부작용|이상사례", re.IGNORECASE)


def shape_axis_queries(plan: Any, question: str) -> tuple[Any, dict[str, Any]]:
    """Bound unrelated expansion after the executable plan is fully prepared."""

    cap = _configured_cap()
    axes = _axes(question)
    related = _related_sources(axes)
    trace: dict[str, Any] = {
        "applied": False,
        "cap": cap,
        "axes": list(axes),
        "related_sources": list(related),
        "all_tool_lanes_executable": all(
            bool(queries) for _source, queries in plan.tool_queries.items()
        ),
    }
    if cap is None:
        trace["reason"] = "switch_disabled"
        shaped, suppression = _suppress_redundant_patent_news(plan)
        trace["patent_news_suppression"] = suppression
        trace["all_tool_lanes_executable"] = all(
            bool(queries) for _source, queries in shaped.tool_queries.items()
        )
        return shaped, trace
    if not related:
        trace["reason"] = "axis_not_detected"
        shaped, suppression = _suppress_redundant_patent_news(plan)
        trace["patent_news_suppression"] = suppression
        trace["all_tool_lanes_executable"] = all(
            bool(queries) for _source, queries in shaped.tool_queries.items()
        )
        return shaped, trace

    normalized_question = " ".join(question.split())
    query_updates: dict[str, tuple[str, ...]] = {}
    requested: dict[str, int] = {}
    executed: dict[str, int] = {}
    omitted: dict[str, tuple[str, ...]] = {}
    previous = plan.query_scope
    unexecuted_reasons = dict(previous.unexecuted_reasons) if previous else {}
    capped_sources: list[str] = []
    for source, queries in plan.tool_queries.items():
        unique = tuple(dict.fromkeys(query for query in queries if query.strip()))
        previous_requested = (
            int(previous.requested_calls.get(source, 0)) if previous is not None else 0
        )
        requested[source] = max(previous_requested, len(unique))
        if source in unexecuted_reasons:
            selected = ()
        elif source in related:
            selected = unique or (normalized_question,)
        else:
            selected = unique[:cap] if unique else (normalized_question,)
            capped_sources.append(source)
        query_updates[source] = selected
        executed[source] = len(selected)
        if source not in related:
            omitted[source] = tuple(
                query for query in unique if query not in selected
            )
        elif previous is not None and previous.omitted_queries.get(source):
            omitted[source] = tuple(previous.omitted_queries[source])

    shaped = plan.model_copy(
        update={
            "tool_queries": plan.tool_queries.model_copy(update=query_updates),
            "clinical_query_specs": (
                plan.clinical_query_specs
                if "clinicaltrials" in related
                else plan.clinical_query_specs[: len(query_updates["clinicaltrials"])]
            ),
            "query_scope": QueryScope(
                requested_calls=requested,
                executed_calls=executed,
                omitted_queries=omitted,
                unexecuted_reasons=unexecuted_reasons,
            ),
        }
    )
    trace.update(
        {
            "applied": True,
            "reason": "unrelated_axis_queries_bounded",
            "capped_sources": capped_sources,
            "selection_mode": "truncate_derived_then_raw_fallback",
            "executed_calls": executed,
            "all_tool_lanes_executable": all(
                bool(queries) for _source, queries in shaped.tool_queries.items()
            ),
        }
    )
    shaped, suppression = _suppress_redundant_patent_news(shaped)
    trace["patent_news_suppression"] = suppression
    trace["all_tool_lanes_executable"] = all(
        bool(queries) for _source, queries in shaped.tool_queries.items()
    )
    return shaped, trace


def question_axes(question: str) -> tuple[str, ...]:
    return _axes(question)


def _suppress_redundant_patent_news(plan: Any) -> tuple[Any, dict[str, Any]]:
    patent_queries = tuple(plan.tool_queries.patent)
    if not patent_queries:
        return plan, {"applied": False, "reason": "patent_not_planned"}

    web_queries = tuple(plan.tool_queries.web)
    if not web_queries:
        return plan, {"applied": False, "reason": "web_not_planned"}

    previous = plan.query_scope
    requested = dict(previous.requested_calls) if previous is not None else {}
    executed = dict(previous.executed_calls) if previous is not None else {}
    omitted = dict(previous.omitted_queries) if previous is not None else {}
    unexecuted_reasons = (
        dict(previous.unexecuted_reasons) if previous is not None else {}
    )
    requested["web"] = 0
    executed["web"] = 0
    omitted["web"] = tuple(dict.fromkeys((*omitted.get("web", ()), *web_queries)))
    unexecuted_reasons.pop("web", None)

    answer_sources = plan.answer_sources
    if "web" in answer_sources:
        answer_sources = tuple(source for source in answer_sources if source != "web")
        if "patent" not in answer_sources:
            answer_sources = (*answer_sources, "patent")

    shaped = plan.model_copy(
        update={
            "answer_sources": answer_sources,
            "tool_queries": plan.tool_queries.model_copy(update={"web": ()}),
            "query_scope": QueryScope(
                requested_calls=requested,
                executed_calls=executed,
                omitted_queries=omitted,
                unexecuted_reasons=unexecuted_reasons,
            ),
        }
    )
    return shaped, {
        "applied": True,
        "reason": "official_patent_source_planned",
        "omitted_web_queries": list(web_queries),
    }


def _configured_cap() -> int | None:
    raw = os.environ.get(AXIS_UNRELATED_QUERY_CAP_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 1 <= value <= 2 else None


def _axes(question: str) -> tuple[str, ...]:
    rules = (
        ("patient_statistics", _PATIENT_RE),
        ("market_total", _MARKET_RE),
        ("patent", _PATENT_RE),
        ("clinical_trials", _CLINICAL_RE),
        ("regulatory", _REGULATORY_RE),
        ("safety", _SAFETY_RE),
    )
    return tuple(name for name, pattern in rules if pattern.search(question))


def _related_sources(axes: tuple[str, ...]) -> tuple[str, ...]:
    owners = {
        "patient_statistics": ("hira",),
        "market_total": ("mart",),
        "patent": ("patent",),
        "clinical_trials": ("clinicaltrials",),
        "regulatory": ("nedrug",),
        "safety": ("openfda",),
    }
    selected = {
        source
        for axis in axes
        for source in owners.get(axis, ())
    }
    return tuple(source for source in SOURCE_NAMES if source in selected)
