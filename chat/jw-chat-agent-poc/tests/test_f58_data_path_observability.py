from __future__ import annotations

import pytest

from jw_chat_agent_poc.common.source_display import public_source_label
from jw_chat_agent_poc.service.general_view_routing import GeneralViewService
from jw_chat_agent_poc.tools import general_view_mart
from jw_chat_agent_poc.tools.cause_backend import CauseBackend
from jw_chat_agent_poc.tools.general_view_backend import (
    AtcCandidate,
    GeneralMarket,
    TopBrand,
)
from jw_chat_agent_poc.tools.general_view_mart import (
    GeneralMartRows,
    GeneralViewMartBackend,
    GeneralViewMartLoadError,
)
from jw_chat_agent_poc.tools.general_view_membership import GeneralMembershipLoadError
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.metrics.cache_live import (
    CsdActivityTarget,
    CsdActivityTargetLoadError,
    StaticCsdActivityReader,
    StaticMetricsCacheReader,
)

from test_cause_backend import FakeResponse, ScriptedSession, _cause_payload
from test_metrics_cache import BRAND_CARDS, CACHE_BRANDS


class NoStrategicMembership:
    def resolve(self, question: str, allow_default: bool = False):
        raise LookupError(question)


class DirectMartReader:
    def read(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMartRows:
        return GeneralMartRows(
            atc4_code=atc4,
            atc4_description="스타틴류",
            source=source,
            measure=measure,
            unit="KRW",
            market_size_series={"2026-05": 100.0},
            brand_ranking={"2026-05": [{"brand": "리바로", "rank": 1, "raw_value": 10.0, "ms": 10.0}]},
            brand_name=brand,
            brand_metric_history={"2026-05": {"raw_value": 10.0, "ms": 10.0, "rank": 1}} if brand else {},
        )


class BackendFallback:
    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]:
        return (AtcCandidate("C10A1", "스타틴류"),)

    def market(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMarket:
        return _market(atc4=atc4, source=source, measure=measure, brand=brand)


class FailingMartReader:
    def __init__(self, reason: str = "query_error") -> None:
        self.reason = reason

    def read(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMartRows:
        raise GeneralViewMartLoadError("fixture mart failure", reason=self.reason)


class FailingMembership:
    def resolve(self, brand: str, source: str):
        raise GeneralMembershipLoadError("fixture membership failure")


class FailingCsdTargetReader:
    def load(self) -> tuple[CsdActivityTarget, ...]:
        raise CsdActivityTargetLoadError("fixture target failure")


def test_direct_mart_result_records_true_path_and_source_label() -> None:
    service = GeneralViewService(
        GeneralViewMartBackend(DirectMartReader(), BackendFallback()),
        NoStrategicMembership(),
        enabled=True,
    )

    result = service.answer("일반뷰 C10A1 시장 규모", compact=False, dual=False)
    call = result["tool_calls"][0]

    assert call["source"] == "jw-market-direct-mart"
    assert call["render_data"]["selected_data_path"] == "direct_mart"
    assert call["qa_trace"]["selected_data_path"] == "direct_mart"


def test_backend_fallback_records_selected_path_and_reason_without_changing_answer() -> None:
    service = GeneralViewService(
        GeneralViewMartBackend(FailingMartReader("query_error"), BackendFallback()),
        NoStrategicMembership(),
        enabled=True,
    )

    result = service.answer("일반뷰 C10A1 시장 규모", compact=False, dual=False)
    call = result["tool_calls"][0]

    assert call["source"] == "jw-market-backend-api"
    assert call["render_data"]["selected_data_path"] == "backend_fallback"
    assert call["render_data"]["fallback_reason"] == "query_error"
    assert call["qa_trace"]["fallback_reason"] == "query_error"
    assert result["answer"].startswith("## 일반뷰 (ATC4)")


def test_direct_and_fallback_paths_keep_identical_answer_content() -> None:
    direct = GeneralViewService(
        GeneralViewMartBackend(DirectMartReader(), BackendFallback()),
        NoStrategicMembership(),
        enabled=True,
    ).answer("일반뷰 C10A1 시장 규모", compact=False, dual=False)
    fallback = GeneralViewService(
        GeneralViewMartBackend(FailingMartReader("query_error"), BackendFallback()),
        NoStrategicMembership(),
        enabled=True,
    ).answer("일반뷰 C10A1 시장 규모", compact=False, dual=False)

    assert direct["answer"] == fallback["answer"]
    assert direct["sources"] == fallback["sources"]
    assert direct["general_view_ready"] == fallback["general_view_ready"]


def test_data_path_sources_have_f12_compatible_public_labels() -> None:
    assert public_source_label("jw-market-direct-mart") == "JW Market 직접 Mart"
    assert public_source_label("jw-market-backend-api") == "JW Market Backend API"


@pytest.mark.parametrize(
    ("error_code", "expected"),
    (
        (1146, "table_missing"),
        (2003, "connect_error"),
        (1064, "query_error"),
    ),
)
def test_mart_query_errors_are_classified_without_exposing_error_text(error_code: int, expected: str) -> None:
    classify = getattr(general_view_mart, "_mart_query_failure_reason")

    assert classify(Exception(error_code, "sensitive connection detail")) == expected


@pytest.mark.parametrize("reason", ("zero_rows", "brand_row_missing", "missing_period"))
def test_non_query_mart_fallback_reasons_reach_result_trace(reason: str) -> None:
    service = GeneralViewService(
        GeneralViewMartBackend(FailingMartReader(reason), BackendFallback()),
        NoStrategicMembership(),
        enabled=True,
    )

    call = service.answer("일반뷰 C10A1 시장 규모", compact=False, dual=False)["tool_calls"][0]

    assert call["render_data"]["fallback_reason"] == reason
    assert call["qa_trace"]["fallback_reason"] == reason


def test_membership_backend_fallback_is_visible_in_result() -> None:
    fallback = BackendFallback()
    service = GeneralViewService(
        fallback,
        NoStrategicMembership(),
        enabled=True,
        general_membership=FailingMembership(),
    )

    result = service.answer("IQVIA 마운자로 시장 점유율", compact=False, dual=False)

    assert result["general_view_contract"]["membership_source"] == "backend_fallback"
    assert result["router_diagnostics"]["membership_source"] == "backend_fallback"


def test_csd_legacy_target_fallback_is_visible_without_changing_payload() -> None:
    tool = MetricsTool(
        mode="cache",
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS),
        csd_activity_reader=StaticCsdActivityReader(
            {("LIVALO Market", "LIVALO"): (("2026-04", 1411), ("2026-05", 1769))}
        ),
        csd_activity_target_reader=FailingCsdTargetReader(),
    )

    result = tool.get_csd_activity_trend("리바로", limit=2)

    assert result["status"] == "ok"
    assert result["render_data"]["master_product"] == "LIVALO"
    assert result["render_data"]["csd_target_source"] == "legacy_static_map"


def test_cause_auto_records_discarded_source_and_no_data_reason() -> None:
    no_data = {
        "brand": "마운자로",
        "view": "market_landscape",
        "source": "UBIST",
        "measure": "sales",
        "data": None,
        "reason": "brand_not_in_source",
    }
    backend = CauseBackend(
        base_url="http://backend",
        session=ScriptedSession(
            FakeResponse(200, no_data),
            FakeResponse(200, _cause_payload(brand="마운자로", source="IQVIA")),
        ),
    )

    market = backend.market("마운자로")

    assert market.source == "IQVIA"
    assert market.trace.fallback_from_source == "UBIST"
    assert market.trace.fallback_reason == "brand_not_in_source"
    assert market.trace.as_dict()["fallback_from_source"] == "UBIST"


def _market(*, atc4: str, source: str, measure: str, brand: str | None) -> GeneralMarket:
    return GeneralMarket(
        view_type="general_view",
        market_basis="ATC4",
        atc4_code=atc4,
        atc4_description="스타틴류",
        source=source.upper(),
        measure=measure,
        unit="KRW",
        period="2026-05",
        market_size=100.0,
        brand=brand,
        brand_value=10.0 if brand else None,
        brand_share_pct=10.0 if brand else None,
        brand_rank=1 if brand else None,
        top_brands=(TopBrand("리바로", 1, 10.0, 10.0),),
    )
