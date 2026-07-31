from __future__ import annotations

from typing import Any

from jw_chat_agent_poc.agent_loop.tools import AgentToolFacade
from jw_chat_agent_poc.orchestrator.agent import (
    QueryFailureReason,
    _query_failure_reason,
)
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.service.answer_safety import (
    enforce_relational_numeric_claims_with_trace,
)
from jw_chat_agent_poc.tools.query_layer import (
    IncompatibleComparisonError,
    MartRecord,
    StaticStrategicMartReader,
    StrategicQueryLayer,
)


def test_cross_market_comparison_exposes_typed_reason() -> None:
    facade = _facade(
        (
            _record("ml_006", "리바로", 100.0),
            _record("ml_009", "가드렛", 80.0),
        )
    )

    execution = facade.execute(
        "compare_brands_series",
        {"brand": "리바로", "comparison_brand": "가드렛"},
    )

    assert execution.call["tool"] == "query_failed"
    assert execution.call["render_data"]["reason_code"] == "incompatible_comparison"
    assert execution.call["render_data"]["anchor_brand"] == "리바로"
    assert execution.call["render_data"]["comparison_brand"] == "가드렛"


def test_cross_market_absolute_sales_uses_each_brands_own_market() -> None:
    facade = _facade(
        (
            _record("ml_006", "리바로", 100.0),
            _record("ml_009", "가드렛", 80.0),
        )
    )

    execution = facade.execute(
        "compare_brands_series",
        {
            "brand": "리바로",
            "comparison_brand": "가드렛",
            "measure": "sales",
        },
    )

    assert execution.status == "ok"
    assert execution.call["render_data"]["brand"] == "가드렛"
    assert execution.call["render_data"]["market_id"] == "ml_009"
    assert execution.call["render_data"]["metric"] == "sales"


def test_absent_comparison_brand_remains_generic_query_failure() -> None:
    facade = _facade((_record("ml_006", "리바로", 100.0),))

    execution = facade.execute(
        "compare_brands_series",
        {"brand": "리바로", "comparison_brand": "가드렛"},
    )

    assert execution.call["tool"] == "query_failed"
    assert "reason_code" not in execution.call["render_data"]
    assert execution.call["render_data"]["error_type"] == "LookupError"


def test_common_failure_registry_maps_incompatible_comparison() -> None:
    error = IncompatibleComparisonError(
        anchor_brand="리바로",
        comparison_brand="가드렛",
        anchor_market="ml_006",
        comparison_markets=("ml_009",),
    )

    assert (
        _query_failure_reason(error)
        is QueryFailureReason.INCOMPATIBLE_COMPARISON
    )


def test_incompatible_comparison_uses_typed_partial_guidance() -> None:
    failed_call = _facade(
        (
            _record("ml_006", "리바로", 100.0),
            _record("ml_009", "가드렛", 80.0),
        )
    ).execute(
        "compare_brands_series",
        {"brand": "리바로", "comparison_brand": "가드렛"},
    ).call
    successful_call = {
        "tool": "get_brand_metric",
        "status": "ok",
        "render_data": {
            "status": "ok",
            "brand": "리바로",
            "metric": "market_share",
            "brand_value_series_10pt": [
                {"period": "2026-03", "ms_pct": 11.0},
                {"period": "2026-04", "ms_pct": 12.0},
            ],
        },
    }

    result = enforce_relational_numeric_claims_with_trace(
        "리바로와 가드렛의 점유율 변화 비교",
        "리바로의 점유율은 상승했습니다.",
        [successful_call, failed_call],
    )

    assert result.disposition == "partial"
    assert "동일한 시장 정의와 분모" in result.answer
    assert "직접 비교할 수 없습니다" in result.answer
    assert "리바로" in result.answer
    assert "가드렛" in result.answer
    assert "도구 상태 error" not in result.answer


def _facade(records: tuple[MartRecord, ...]) -> AgentToolFacade:
    return AgentToolFacade(
        metrics=_UnusedMetrics(),
        resolver=BrandResolver(),
        allowed_brands=("리바로", "가드렛"),
        query_layer=StrategicQueryLayer(reader=StaticStrategicMartReader(records)),
        market_by_brand={"리바로": "ml_006", "가드렛": "ml_009"},
    )


class _UnusedMetrics:
    pass


def _record(market: str, brand: str, value: float) -> MartRecord:
    history: dict[str, dict[str, Any]] = {
        "2026-03": {"raw_value": value, "ms": 10.0},
        "2026-04": {"raw_value": value + 1.0, "ms": 11.0},
    }
    return MartRecord(
        ml_id=market,
        brand_name=brand,
        source="ubist",
        measure="sales",
        metric_history=history,
        channel_data={},
        specialty_data={},
        dimension_data={},
        by_dimension={"company": "테스트제약", "molecule": f"{brand}성분"},
    )
