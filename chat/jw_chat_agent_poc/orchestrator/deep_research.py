from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from jw_chat_agent_poc.agent_loop.models import AgentDecision, AgentObservation, ToolCallPlan


_DEEP_TRIGGER = re.compile(r"^/deep(?:[ \t]+|\n|$)")


@dataclass(frozen=True, slots=True)
class DeepResearchRequest:
    enabled: bool
    question: str
    original_question: str


def parse_deep_research_request(question: str) -> DeepResearchRequest:
    """Parse only a leading, token-delimited /deep command."""

    match = _DEEP_TRIGGER.match(question)
    if match is None:
        return DeepResearchRequest(False, question, question)
    return DeepResearchRequest(True, question[match.end() :].lstrip(), question)


@dataclass(frozen=True, slots=True)
class DeepResearchToolPlanner:
    """Issue one broad, independent evidence batch for an explicit /deep request."""

    def decide(
        self,
        question: str,
        observations: tuple[AgentObservation, ...],
        schemas: tuple[dict[str, Any], ...],
        allowed_brands: tuple[str, ...] = (),
        allowed_periods: tuple[str, ...] = (),
    ) -> AgentDecision:
        del allowed_periods
        if observations:
            return AgentDecision(final_answer="evidence_ready")
        if not allowed_brands:
            return AgentDecision(final_answer="brand_unresolved")

        brand = allowed_brands[0]
        available = {
            str(schema.get("function", {}).get("name") or "")
            for schema in schemas
            if isinstance(schema, dict)
        }
        calls = _deep_research_calls(question, brand, available)
        return AgentDecision(tool_calls=calls)


def _deep_research_calls(
    question: str,
    brand: str,
    available: set[str],
) -> tuple[ToolCallPlan, ...]:
    planned: list[ToolCallPlan] = []

    def add(name: str, arguments: dict[str, str], reason: str) -> None:
        if name in available:
            planned.append(ToolCallPlan(name=name, arguments=arguments, reason=reason))

    add("get_metric", {"brand": brand, "measure": "series"}, "브랜드 장기 지표")
    add("get_market_scope", {"brand": brand}, "시장 범위와 경쟁군")
    add("get_brand_series", {"brand": brand, "history_points": "24"}, "브랜드와 시장 시계열")
    add("get_top_brands", {"brand": brand, "limit": "10"}, "상위 경쟁 브랜드")
    add("search_news", {"brand": brand, "query": ""}, "검증된 뉴스 코퍼스")
    add("get_disease_stats", {"brand": brand}, "질환 환자 통계")
    add("search_clinical", {"brand": brand}, "국내외 임상 근거")
    add("search_drug_info", {"brand": brand}, "식약처 허가 근거")
    add("search_safety", {"brand": brand}, "FDA 안전성 근거")
    add("search_patent", {"brand": brand, "query": question}, "국내외 특허 근거")
    add("csd_activity_trend", {"brand": brand}, "판매 활동 추이")

    web_angles = (
        f"{question} 최신 시장 경쟁 제품 출시",
        f"{brand} 임상 허가 규제 안전성 최신 동향",
        f"{brand} 치료 가이드라인 환자 접근성 시장 전망",
    )
    for query in web_angles:
        add("web_search", {"brand": brand, "query": query}, "다각도 웹 근거")
    return tuple(planned)
