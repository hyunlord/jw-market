"""R15 STAGE 2 — a mart call cut by its budget must keep the brands it finished."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from jw_chat_agent_poc.service.v4.executor import _accepts_retrieval_budget
from jw_chat_agent_poc.tools.external.client import ExternalCall

BRANDS = ("리바로", "리바로젯", "리피토", "크레스토")


class _Resolution:
    def __init__(self, brand: str) -> None:
        self.canonical_brand = brand
        self.market_ids = ("ml_006",)


def _build_mart(monkeypatch, *, seconds_per_brand: float):
    """Drive the real ingredient-expansion path over a fixed brand list.

    Only the two upstream edges are faked — the MFDS search that names the
    products and the per-brand mart fan-out. The brand loop, the budget
    arithmetic and the result assembly under test are the shipped ones.
    """
    from jw_chat_agent_poc.agent_loop import factory
    from jw_chat_agent_poc.service import general_view_routing
    from jw_chat_agent_poc.service.v4 import adapters as v4_adapters

    class Resolver:
        def resolve(self, query: str, *, allow_default: bool):
            assert allow_default is False
            if query in BRANDS:
                return _Resolution(query)
            raise LookupError(query)

    class External:
        timeout_s = 12

        def mfds_permission_search(self, _item_name: str) -> ExternalCall:
            return ExternalCall(
                tool="nedrug_permission_search",
                source="nedrug_mcp",
                status="ok",
                summary_text="ok",
                render_data={"items": [{"item_name": brand} for brand in BRANDS]},
            )

    monkeypatch.setattr(
        factory,
        "build_chat_agent_dependencies",
        lambda **_kwargs: SimpleNamespace(
            external=External(),
            resolver=Resolver(),
            query_layer=SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(
        general_view_routing.GeneralViewService,
        "from_env",
        lambda _resolver: SimpleNamespace(
            route=lambda _query: general_view_routing.GeneralRoute.EXISTING,
            answer=lambda *_args, **_kwargs: {},
        ),
    )

    visited: list[str] = []

    def fake_strategic(_layer, brand: str, _query: str, **_kwargs):
        visited.append(brand)
        time.sleep(seconds_per_brand)
        return [{"source": "UBIST", "brand": brand, "call": index} for index in range(8)]

    monkeypatch.setattr(v4_adapters, "_strategic_mart_calls", fake_strategic)

    return v4_adapters.build_source_adapters()["mart"], visited


def test_executor_only_budgets_adapters_that_name_the_parameter() -> None:
    def with_budget(query, *, period_from=None, period_to=None, budget_s=None):
        return query

    def without_budget(query, *, period_from=None, period_to=None):
        return query

    def kwargs_only(query, **kwargs):
        return query

    assert _accepts_retrieval_budget(with_budget) is True
    assert _accepts_retrieval_budget(without_budget) is False
    # A ``**kwargs`` double must not be credited with a behaviour it lacks.
    assert _accepts_retrieval_budget(kwargs_only) is False


def test_the_shipped_mart_adapter_accepts_a_budget() -> None:
    from jw_chat_agent_poc.service.v4 import adapters as v4_adapters

    assert _accepts_retrieval_budget(v4_adapters.build_source_adapters()["mart"])


@pytest.mark.parametrize("budget_s", [None, 1000.0])
def test_generous_budget_keeps_every_brand(monkeypatch, budget_s) -> None:
    mart, visited = _build_mart(monkeypatch, seconds_per_brand=0.0)

    result = mart("피타바스타틴 매출 알려줘", budget_s=budget_s)

    assert visited == list(BRANDS)
    assert result.status == "ok"
    assert len(result.payload["calls"]) == 8 * len(BRANDS)
    assert "partial_preservation" not in result.failure_detail


def test_tight_budget_preserves_finished_brands_and_names_the_dropped(
    monkeypatch,
) -> None:
    # Reserve 0 so the arithmetic under test is purely elapsed + worst brand.
    monkeypatch.setattr(
        "jw_chat_agent_poc.service.v4.adapters._MART_BRAND_RESERVE_S", 0.0
    )
    mart, visited = _build_mart(monkeypatch, seconds_per_brand=0.12)

    result = mart("피타바스타틴 매출 알려줘", budget_s=0.3)

    assert 0 < len(visited) < len(BRANDS)
    detail = result.failure_detail["partial_preservation"]
    assert detail["unit"] == "brand"
    assert detail["requested"] == len(BRANDS)
    assert detail["preserved"] == len(visited)
    assert detail["dropped"] == len(BRANDS) - len(visited)
    assert detail["preserved_brands"] == visited
    assert detail["dropped_brands"] == [
        brand for brand in BRANDS if brand not in visited
    ]
    assert detail["preserved"] + detail["dropped"] == detail["requested"]
    # Invariant 2: the preserved work is returned, never discarded.
    assert result.status == "ok"
    assert len(result.payload["calls"]) == 8 * len(visited)
    assert "브랜드 4개 중" in (result.notice or "")


def test_first_brand_always_runs_even_on_an_exhausted_budget(monkeypatch) -> None:
    mart, visited = _build_mart(monkeypatch, seconds_per_brand=0.0)

    result = mart("피타바스타틴 매출 알려줘", budget_s=0.0)

    assert visited == [BRANDS[0]]
    detail = result.failure_detail["partial_preservation"]
    assert detail["preserved"] == 1
    assert detail["dropped"] == len(BRANDS) - 1
    assert result.payload["calls"]


def test_partial_preservation_reaches_the_body_notice() -> None:
    from jw_chat_agent_poc.service.v4.contracts import SourceResult
    from jw_chat_agent_poc.service.v4.runtime import _retrieval_shortfall_notice

    result = SourceResult(
        source="mart",
        query="피타바스타틴 매출 알려줘",
        status="ok",
        payload={"calls": [{"source": "UBIST"}]},
        failure_detail={
            "partial_preservation": {
                "unit": "brand",
                "requested": 4,
                "preserved": 2,
                "dropped": 2,
                "preserved_brands": ["리바로", "리바로젯"],
                "dropped_brands": ["리피토", "크레스토"],
            }
        },
    )
    notice = _retrieval_shortfall_notice((result,)) or ""
    assert "대상 4개 중 2개까지 자료를 확보했고" in notice
    assert "나머지 2개는 조회 시간이 초과되어" in notice
    assert "리피토" in notice and "크레스토" in notice
    assert "partial_preservation" not in notice


def test_f2_failure_injection_without_a_budget_the_loop_never_stops_early(
    monkeypatch,
) -> None:
    """F2 (negative arm): with no budget every brand runs and nothing is flagged."""
    mart, visited = _build_mart(monkeypatch, seconds_per_brand=0.12)

    result = mart("피타바스타틴 매출 알려줘", budget_s=None)

    assert visited == list(BRANDS)
    assert result.failure_detail == {}
    assert len(result.payload["calls"]) == 8 * len(BRANDS)
