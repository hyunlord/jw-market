from __future__ import annotations

from typing import Any

from jw_chat_agent_poc.agentic import NewsFilterPlan


def news_summary(brand: str, count: int, plan: NewsFilterPlan) -> str:
    filter_hint = _filter_hint(plan)
    return f"{brand} 관련 뉴스 {count}건을 확인했습니다{filter_hint}."


def no_data_summary(brand: str, plan: NewsFilterPlan) -> str:
    if plan.unsupported:
        return f"{brand} 뉴스 질문의 일부 필터는 현재 지원하지 않습니다."
    hint = _zero_filter_hint(plan)
    return f"{brand} 관련 뉴스 없음: 조건에 맞는 이벤트를 찾지 못했습니다{hint}."


def no_data_message(plan: NewsFilterPlan) -> str:
    if plan.unsupported:
        return "지원하지 않는 뉴스 필터가 있어 조건에 맞는 뉴스를 표시하지 않았습니다."
    hint = _zero_filter_hint(plan)
    return f"조건에 맞는 관련 뉴스 없음{hint}"


def transparency_fields(plan: NewsFilterPlan, latest_event_date: str) -> dict[str, Any]:
    unsupported = [item.to_dict() for item in plan.unsupported]
    return {
        "applied_filters": plan.applied_filters(latest_event_date),
        "unsupported_filters": unsupported,
        "unsupported": unsupported,
        "interpretation_notes": [],
        "unparsed_constraints": [],
        "data_basis": {
            "source": "cache_deep_analysis.data.events",
            "date_grain": "event_date",
            "latest_event_date": latest_event_date or "-",
        },
    }


def _filter_hint(plan: NewsFilterPlan) -> str:
    applied = plan.applied_filters()
    if not applied:
        return ""
    labels = ", ".join(f"{key}={value}" for key, value in applied.items())
    return f" ({labels} 필터 적용)"


def _zero_filter_hint(plan: NewsFilterPlan) -> str:
    applied = plan.applied_filters()
    if not applied:
        return ""
    labels = ", ".join(f"{key}={value} 조건 0건" for key, value in applied.items())
    return f" ({labels})"
