from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from jw_chat_agent_poc.agent_loop.metric_intent import explicit_base_metrics_from_question
from jw_chat_agent_poc.orchestrator.narrative_intent import needs_market_series
from jw_chat_agent_poc.router.bq_router import BQSubQuestion


BACKGROUND_NEWS_CONTEXT_TOKENS: Final[tuple[str, ...]] = (
    "경쟁 구도",
    "경쟁구도",
    "경쟁 동향",
    "경쟁동향",
    "구도 변화",
    "시장 구도",
    "변화 요인",
    "변화요인",
    "향후 예상",
    "Market expansion",
    "External",
    "Internal",
    "보건 정책",
    "Line extension",
    "재편",
    "위협",
)

def allows_background_news_context(question: str) -> bool:
    return any(token in question for token in BACKGROUND_NEWS_CONTEXT_TOKENS)


def metric_from_question(question: str) -> str:
    lower = question.lower()
    explicit_metrics = explicit_base_metrics_from_question(question)
    if "prescription_volume" in explicit_metrics:
        return "prescription_volume"
    if "hhi" in lower:
        return "hhi"
    if any(keyword in question for keyword in ("시장규모", "시장 규모")) or "cagr" in lower:
        return "growth"
    if "momentum" in lower or "모멘텀" in question:
        return "momentum"
    if "ei" in lower:
        return "ei"
    if needs_market_series(question) or any(token in lower for token in ("monthly sales", "sales trend", "sales series")):
        return "series"
    if "성장" in question:
        return "growth"
    if "sales" in explicit_metrics:
        return "sales"
    if any(keyword in question for keyword in ("점유율", "ms", "순위", "경쟁")):
        return "market_share"
    return "market_share"


def requires_brand(routes: Sequence[BQSubQuestion]) -> bool:
    return any(
        ("metrics" in route.sources or "external_api" in route.sources or "deep_analysis_events" in route.sources)
        for route in routes
    )
