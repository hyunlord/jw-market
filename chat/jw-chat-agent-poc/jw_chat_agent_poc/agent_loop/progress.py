from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypedDict


class ToolProgressPayload(TypedDict):
    event: str
    stage: str
    tool: str
    label: str
    index: int
    total: int


ProgressCallback = Callable[[ToolProgressPayload], None]


@dataclass(frozen=True, slots=True)
class ToolLabelRule:
    label: str
    exact: frozenset[str] = frozenset()
    prefixes: tuple[str, ...] = ()

    def matches(self, tool_name: str) -> bool:
        return tool_name in self.exact or any(tool_name.startswith(prefix) for prefix in self.prefixes)


TOOL_LABEL_RULES: Final[tuple[ToolLabelRule, ...]] = (
    ToolLabelRule(
        label="시장 지표 조회 중",
        exact=frozenset({"get_metric", "get_top_brands", "compare_brands_series"}),
        prefixes=("get_brand_",),
    ),
    ToolLabelRule(label="시장 구조 확인 중", exact=frozenset({"get_market_scope"})),
    ToolLabelRule(
        label="세그먼트 데이터 집계 중",
        exact=frozenset({"query", "query_spec", "get_segment_breakdown", "get_channel_breakdown", "get_specialty_breakdown"}),
        prefixes=("query_",),
    ),
    ToolLabelRule(label="관련 뉴스 검색 중", exact=frozenset({"deep_analysis_related_news", "search_news"})),
    ToolLabelRule(
        label="허가·특허·임상 조회 중",
        exact=frozenset({"search_patent", "search_clinical", "search_drug_info", "clinical_trials", "openfda_label_search"}),
        prefixes=("mfds_",),
    ),
    ToolLabelRule(label="환자·행위 통계 조회 중", exact=frozenset({"get_disease_stats", "get_procedure_stats"})),
    ToolLabelRule(label="웹 검색 중", exact=frozenset({"web_search"})),
    ToolLabelRule(label="경쟁 지표 계산 중", exact=frozenset({"agent_calculation"})),
)

DEFAULT_TOOL_PROGRESS_LABEL: Final = "데이터 조회 중"


def tool_progress_label(tool_name: str) -> str:
    for rule in TOOL_LABEL_RULES:
        if rule.matches(tool_name):
            return rule.label
    return DEFAULT_TOOL_PROGRESS_LABEL


def tool_progress_payload(tool_name: str, *, index: int, total: int) -> ToolProgressPayload:
    return {
        "event": "progress",
        "stage": "tool",
        "tool": tool_name,
        "label": tool_progress_label(tool_name),
        "index": index,
        "total": total,
    }
