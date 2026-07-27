from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
import requests

from jw_chat_agent_poc.tools.cause_backend import (
    CauseBackend,
    CauseBackendError,
    CauseBackendTrace,
    parse_cause_market_response,
)
from jw_chat_agent_poc.agent_loop.tools import QUERY_FAILED_STATUS, _tool_error
from jw_chat_agent_poc.common.qa_trace import attach_tool_qa_trace
from jw_chat_agent_poc.tools.query_layer import StrategicQueryLayer
from jw_chat_agent_poc.tools.query_layer import MartRecord, StaticStrategicMartReader


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}", response=self)  # type: ignore[arg-type]

    def json(self) -> dict[str, Any]:
        return self._payload


class ScriptedSession:
    def __init__(self, *outcomes: FakeResponse | BaseException) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_cause_backend_uses_brand_only_contract_and_projects_internal_ids_out() -> None:
    session = ScriptedSession(FakeResponse(200, _cause_payload()))
    backend = CauseBackend(base_url="http://backend", session=session, ttl_seconds=60)  # type: ignore[arg-type]

    market = backend.market("리바로")
    scope = market.render_market_scope(limit=5)
    hhi = market.render_brand_metric("hhi")

    sent = session.requests[0]
    assert sent["method"] == "GET"
    assert sent["url"] == "http://backend/api/cause/%EB%A6%AC%EB%B0%94%EB%A1%9C"
    assert sent["params"] == {"view": "market_landscape", "source": "UBIST", "measure": "sales"}
    assert sent["timeout"] == (3.0, 10.0)
    assert market.market_name == "고지혈증"
    assert market.period == "2026-05"
    assert market.market_size == pytest.approx(213_925_043_319.3602)
    assert market.brand_share_pct == pytest.approx(3.7577)
    assert market.brand_rank == 6
    assert hhi["hhi_recent"] == pytest.approx(262.4174)
    assert [row["brand"] for row in scope["level_segments"]] == ["로수젯", "리피토", "리바로"]
    assert not (_recursive_keys(scope) | _recursive_keys(hhi)) & {
        "market_id",
        "markets",
        "ml_id",
        "cd_market_id",
        "strategic_market_id",
    }


def test_cause_backend_probes_iqvia_only_after_explicit_no_data() -> None:
    no_data = {
        "brand": "마운자로",
        "view": "market_landscape",
        "source": "UBIST",
        "measure": "sales",
        "data": None,
        "reason": "brand_not_in_source",
    }
    session = ScriptedSession(FakeResponse(200, no_data), FakeResponse(200, _cause_payload(brand="마운자로", source="IQVIA")))
    backend = CauseBackend(base_url="http://backend", session=session)  # type: ignore[arg-type]

    market = backend.market("마운자로")

    assert market.source == "IQVIA"
    assert [request["params"]["source"] for request in session.requests] == ["UBIST", "IQVIA"]


def test_cause_backend_timeout_fails_without_source_probe_or_cached_calculation() -> None:
    session = ScriptedSession(requests.Timeout("injected timeout"), FakeResponse(200, _cause_payload(source="IQVIA")))
    backend = CauseBackend(base_url="http://backend", session=session)  # type: ignore[arg-type]

    with pytest.raises(CauseBackendError) as raised:
        backend.market("마운자로")

    assert len(session.requests) == 1
    assert raised.value.endpoint == "/api/cause/%EB%A7%88%EC%9A%B4%EC%9E%90%EB%A1%9C"
    assert raised.value.status == "timeout"
    assert raised.value.latency_ms >= 0


def test_cause_backend_does_not_probe_iqvia_for_global_brand_absence() -> None:
    session = ScriptedSession(FakeResponse(404, {"detail": "brand not found"}), FakeResponse(200, _cause_payload(source="IQVIA")))
    backend = CauseBackend(base_url="http://backend", session=session)  # type: ignore[arg-type]

    with pytest.raises(CauseBackendError) as raised:
        backend.market("없는브랜드")

    assert raised.value.status == "no_data"
    assert len(session.requests) == 1


def test_auto_source_cache_never_leaks_iqvia_result_into_explicit_ubist_request() -> None:
    no_data = {
        "brand": "마운자로",
        "view": "market_landscape",
        "source": "UBIST",
        "measure": "sales",
        "data": None,
        "reason": "brand_not_in_source",
    }
    session = ScriptedSession(
        FakeResponse(200, no_data),
        FakeResponse(200, _cause_payload(brand="마운자로", source="IQVIA")),
        FakeResponse(200, no_data),
    )
    backend = CauseBackend(base_url="http://backend", session=session, ttl_seconds=60)  # type: ignore[arg-type]

    assert backend.market("마운자로").source == "IQVIA"
    with pytest.raises(CauseBackendError):
        backend.market("마운자로", source="UBIST")

    assert len(session.requests) == 3


def test_cause_backend_cache_returns_raw_facts_with_cache_trace() -> None:
    session = ScriptedSession(FakeResponse(200, _cause_payload()))
    backend = CauseBackend(base_url="http://backend", session=session, ttl_seconds=60)  # type: ignore[arg-type]

    first = backend.market("리바로")
    cached = backend.market("리바로")

    assert len(session.requests) == 1
    assert first.trace.cache_hit is False
    assert cached.trace.cache_hit is True
    assert cached.trace.endpoint == first.trace.endpoint


def test_query_layer_uses_serving_mart_for_scope_top_brands_and_hhi() -> None:
    layer = StrategicQueryLayer(reader=StaticStrategicMartReader(_mart_records()))

    scope = layer.market_scope("리바로", market="ml_006")
    top = layer.top_brands("리바로", limit=2, market="ml_006")
    hhi = layer.brand_metric("리바로", "hhi", "latest", market="ml_006")

    assert scope["render_data"]["market_size_recent_krw"] == pytest.approx(60.0)
    assert [row["brand"] for row in top["render_data"]["level_segments"]] == ["로수젯", "리피토"]
    assert hhi["render_data"]["hhi_recent"] == pytest.approx(3888.8889)
    assert "backend_trace" not in scope


def test_query_layer_never_calls_cause_backend_when_serving_mart_is_available() -> None:
    layer = StrategicQueryLayer(reader=StaticStrategicMartReader(_mart_records()))

    assert not hasattr(layer, "_cause_backend")
    layer.market_scope("리바로", market="ml_006")
    layer.top_brands("리바로", market="ml_006")
    layer.brand_metric("리바로", "hhi", "latest", market="ml_006")


def _mart_records() -> tuple[MartRecord, ...]:
    periods = tuple(f"2026-{month:02d}" for month in range(1, 13))
    return tuple(
        MartRecord(
            ml_id="ml_006",
            brand_name=brand,
            source="ubist",
            measure="sales",
            metric_history={period: {"raw_value": value} for period in periods},
            channel_data={},
            specialty_data={},
            dimension_data={},
            by_dimension={},
        )
        for brand, value in (("로수젯", 30.0), ("리피토", 20.0), ("리바로", 10.0))
    )


def test_backend_failure_trace_preserves_actual_tool_and_endpoint() -> None:
    error = CauseBackendError(
        "cause backend timeout",
        endpoint="/api/cause/%EB%A7%88%EC%9A%B4%EC%9E%90%EB%A1%9C",
        status="timeout",
        latency_ms=10_000.0,
    )
    execution = _tool_error(
        "get_market_scope",
        {"brand": "마운자로"},
        "요청한 지표 조회 실행이 실패했습니다.",
        status=QUERY_FAILED_STATUS,
        error=error,
    )

    attach_tool_qa_trace(execution.call, started_at=datetime.now(UTC), status=QUERY_FAILED_STATUS)

    assert execution.call["tool"] == "query_failed"
    assert execution.call["render_data"]["tool_name"] == "get_market_scope"
    assert execution.call["qa_trace"]["status"] == "query_failed"
    assert execution.call["qa_trace"]["endpoint"] == error.endpoint
    assert execution.call["qa_trace"]["latency_ms"] == 10_000.0


def _recursive_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            keys.add(str(key))
            keys.update(_recursive_keys(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            keys.update(_recursive_keys(item))
    return keys


def _cause_payload(*, brand: str = "리바로", source: str = "UBIST") -> dict[str, Any]:
    return {
        "brand": brand,
        "brand_name": brand,
        "market_id": "strategy_006",
        "view": "market_landscape",
        "source": source,
        "measure": "sales",
        "unit_label": "KRW",
        "source_epoch": "epoch-20260720",
        "built_at": "2026-07-20T00:00:00Z",
        "market_meta": {
            "strategic_market_id": "strategy_006",
            "view_source_id": "ml_006",
            "market_definition_label": "고지혈증",
            "direct_competition_count": 3,
        },
        "markets": [{"market_id": "strategy_006", "ml_id": "ml_006"}],
        "data": {
            "kpi": {
                "market_size_recent": 213_925_043_319.3602,
                "market_cagr_5y_pct": 9.3677,
                "top3_share_pct": 20.37,
                "hhi_recent": 262.4174,
                "direct_competition_count": 3,
                "target_brand": brand,
                "target_ei": 41.6922,
                "brand_cagr_pct": 3.9056,
                "market_cagr_pct": 9.3677,
                "target_momentum": -0.0165,
                "target_rank": 6,
                "target_share_pct": 3.7577,
                "brand_value_recent": 8_038_598_793.61,
            },
            "sources_data": {
                "market_size_series": [
                    {"period": "2026-04", "value": 226_577_368_890.98, "yoy_growth_pct": 5.0},
                    {"period": "2026-05", "value": 213_925_043_319.3602, "yoy_growth_pct": 4.0},
                ]
            },
            "level_top5_trend": {
                "by_level": {
                    "Brand": {
                        "periods_10pt": ["2026-04", "2026-05"],
                        "values": [
                            {
                                "is_overall": True,
                                "brands_in_value": [
                                    {
                                        "brand": brand,
                                        "rank": 6,
                                        "value_series_10pt": [8_493_234_217.11, 8_038_598_793.61],
                                        "ms_series_10pt": [3.752, 3.7577],
                                        "rank_series_10pt": [6, 6],
                                    }
                                ],
                            }
                        ],
                    }
                }
            },
            "ei_ms_matrix": {
                "data": [
                    {"brand": brand, "rank": 6, "value_recent": 8_038_598_793.61, "share_pct": 3.7577, "is_jw": True},
                    {"brand": "리피토", "rank": 2, "value_recent": 13_108_840_203.03, "share_pct": 6.1278},
                    {"brand": "로수젯", "rank": 1, "value_recent": 19_523_856_225.95, "share_pct": 9.1265},
                    {"brand": "기타", "rank": None, "value_recent": 142_744_758_160.5, "share_pct": 66.7, "is_others": True},
                ]
            },
            "hhi_series_5y": [
                {"period": "2025", "period_full": "2025", "year": 2025, "hhi": 270.0},
                {"period": "2026", "period_full": "2026", "year": 2026, "hhi": 262.4174},
            ],
        },
    }
