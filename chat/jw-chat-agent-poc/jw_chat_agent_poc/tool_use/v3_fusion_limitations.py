from __future__ import annotations

import re

from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    ToolDeferredRecord,
    ToolFailureRecord,
)


_ATC4 = re.compile(r"[A-Z]\d{1,2}[A-Z]\d?", re.IGNORECASE)
_KNOWN_REASON_CODES = frozenset(
    {
        "no_strategic_membership",
        "unknown_brand",
        "invalid_market_label",
        "unsupported_source",
        "ambiguous_family",
        "ambiguous_market",
        "no_anchor",
        "general_composite_unavailable",
        "general_metric_unavailable",
    }
)
_ERROR_REASON_CODES = {
    "NoStrategicMembershipError": "no_strategic_membership",
    "UnknownBrandError": "unknown_brand",
    "InvalidMarketLabelError": "invalid_market_label",
    "UnsupportedSourceError": "unsupported_source",
    "AmbiguousFamilyError": "ambiguous_family",
    "AmbiguousMarketError": "ambiguous_market",
    "NoAnchorError": "no_anchor",
    "GeneralCompositeUnavailableError": "general_composite_unavailable",
    "GeneralMetricUnavailableError": "general_metric_unavailable",
    "BrandOutsideCompositeScopeError": "brand_outside_composite_scope",
}


def failure_reason_code(failure: ToolFailureRecord) -> str:
    explicit = failure.message.split(":", 1)[0].strip()
    if explicit in _KNOWN_REASON_CODES:
        return explicit
    return _ERROR_REASON_CODES.get(failure.error_type, "tool_unavailable")


def failure_limitation(
    failure: ToolFailureRecord,
    *,
    reason_code: str | None = None,
) -> str:
    code = reason_code or failure_reason_code(failure)
    if code == "no_strategic_membership":
        return "일반 시장 기준으로 조회하지 못했습니다."
    if code == "unsupported_source":
        return "그 소스에는 해당 지표가 없습니다."
    if code == "ambiguous_family":
        return "패밀리 표현만으로는 대상을 특정할 수 없습니다. 개별 브랜드를 지정해 주세요."
    if code == "ambiguous_market":
        candidates = tuple(dict.fromkeys(_ATC4.findall(failure.message.upper())))
        if candidates:
            return (
                f"복수 시장 코드({', '.join(candidates)})에 걸쳐 있습니다. "
                "하나를 지정해 주세요."
            )
        return "복수 시장에 걸쳐 있습니다. 하나의 시장을 지정해 주세요."
    if code == "no_anchor":
        return "어떤 시장을 뜻하는지 확인할 수 없습니다. 브랜드나 시장을 지정해 주세요."
    if code == "unknown_brand":
        return "요청한 브랜드를 확인할 수 없습니다. 정확한 브랜드명을 알려주세요."
    if code == "invalid_market_label":
        return "시장 값이 유효하지 않습니다. 전략시장 ID 또는 ATC4 코드를 지정해 주세요."
    if code == "general_composite_unavailable":
        return "현재 지원하지 않는 시장 조합입니다."
    if code == "general_metric_unavailable":
        return "해당 일반 시장 지표는 현재 제공되지 않습니다."
    if code == "brand_outside_composite_scope":
        return "요청한 브랜드는 지정한 복합 시장 조건에 포함되지 않습니다."
    return "요청한 조회 중 일부를 확인하지 못했습니다."


def deferred_limitation(deferred: ToolDeferredRecord) -> str:
    if deferred.tool_name == "web_search":
        return "웹 검색 보강 결과는 현재 답변에 포함되지 않습니다."
    return "일부 근거는 현재 답변 형식으로 변환되지 않았습니다."


__all__ = ["deferred_limitation", "failure_limitation", "failure_reason_code"]
