from __future__ import annotations

from jw_chat_agent_poc import ChatAgent
from jw_chat_agent_poc.agent_loop.factory import ambiguous_brand_result
from jw_chat_agent_poc.orchestrator.market_answer_contract import (
    enforce_market_answer_contract,
)
from jw_chat_agent_poc.orchestrator.router_diagnostics import router_diagnostics
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.resolver.catalog_membership import (
    StaticCatalogMembershipReader,
    TtlCatalogMembershipReader,
)
from jw_chat_agent_poc.router import BQRouter
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.metrics.cache_live import StaticMetricsCacheReader
from jw_chat_agent_poc.tools.query_layer import (
    MartRecord,
    StaticStrategicMartReader,
    StrategicQueryLayer,
)


def _family_agent(
    values: tuple[tuple[str, str, float], ...],
) -> ChatAgent:
    memberships = TtlCatalogMembershipReader(
        StaticCatalogMembershipReader(
            tuple(
                {
                    "brand": brand,
                    "market_id": "ml_006",
                    "market_name": "이상지질혈증",
                    "support_source": "strategic_mart",
                }
                for brand, _source, _value in values
            )
        )
    )
    resolver = BrandResolver(
        mode="cache",
        brand_reader=StaticMetricsCacheReader(cache_brands=[], market_status={}),
        membership_reader=memberships,
    )
    records = tuple(
        MartRecord(
            ml_id="ml_006",
            brand_name=brand,
            source=source,
            measure="sales",
            metric_history={
                "2026-05": {
                    "raw_value": value,
                    "ms": 1.0,
                    "source_status": "OK",
                }
            },
            channel_data={},
            specialty_data={},
            dimension_data={},
            by_dimension={},
            unit_label="KRW",
        )
        for brand, source, value in values
    )
    layer = StrategicQueryLayer(reader=StaticStrategicMartReader(records))
    return ChatAgent(
        router=BQRouter(),
        resolver=resolver,
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        query_layer=layer,
    )


def test_family_sales_aggregates_all_candidates_on_identical_axes() -> None:
    members = (
        "리바로",
        "리바로젯",
        "리바로트",
        "리바로 브이",
        "리바로브이",
        "리바로페노",
        "리바로 하이",
        "리바로하이",
    )
    agent = _family_agent(
        tuple(
            (brand, "ubist", float(index * 1_000_000_000))
            for index, brand in enumerate(members, start=1)
        )
    )

    result = agent.answer("리바로 계열 매출 알려줘")

    assert result["resolution"]["status"] == "family_aggregate"
    aggregate = result["tool_calls"][0]["render_data"]
    assert aggregate["metric"] == "family_sales"
    assert aggregate["family_member_count"] == 8
    assert aggregate["family_members"] == list(members)
    assert aggregate["sales_krw"] == 36_000_000_000.0
    assert aggregate["period"] == "2026-05"
    assert aggregate["source_label"] == "UBIST"
    assert "360.00억원" in result["answer"]


def test_family_sales_does_not_aggregate_across_sources() -> None:
    agent = _family_agent(
        (
            ("리바로", "ubist", 8_000_000_000.0),
            ("리바로젯", "iqvia_nsa", 12_000_000_000.0),
        )
    )

    result = agent.answer("리바로 계열 매출 알려줘")

    assert result["sources"] == ["ambiguous_brand"]
    assert result["tool_calls"] == []
    assert "합산" not in result["answer"]
    assert "하나를 지정" in result["answer"]


def test_large_family_stays_actionable_instead_of_automatic_aggregation() -> None:
    agent = _family_agent(
        tuple(
            (f"아스피린{index}", "ubist", float(index * 100_000_000))
            for index in range(1, 19)
        )
    )

    result = agent.answer("아스피린 계열 매출 알려줘")

    assert result["sources"] == ["ambiguous_brand"]
    assert result["resolution"]["candidates"]
    assert "후보 18개" in result["answer"]
    assert "제품명 일부" in result["answer"]


def test_long_candidate_list_has_exact_count_and_actionable_narrowing() -> None:
    candidates = tuple(f"아스피린 후보 {index}" for index in range(1, 31))
    router = BQRouter()

    result = ambiguous_brand_result(
        "아스피린 계열 매출 알려줘",
        router.route("아스피린 계열 매출 알려줘", has_documents=False),
        router_diagnostics(router),
        candidates,
    )

    assert result["resolution"]["candidates"] == list(candidates)
    assert "후보 30개" in result["answer"]
    assert "제품명 일부" in result["answer"]
    assert "아스피린 후보 1" in result["answer"]
    assert "아스피린 후보 30" not in result["answer"]


def test_candidate_display_removes_dangling_separator_without_mutating_identity() -> None:
    router = BQRouter()
    raw_candidates = ("카나브", "카나브젯 /", "카나브 플러스", "카나브플러스")

    result = ambiguous_brand_result(
        "카나브패밀리 실적 어때?",
        router.route("카나브패밀리 실적 어때?", has_documents=False),
        router_diagnostics(router),
        raw_candidates,
    )

    assert result["resolution"]["candidates"] == list(raw_candidates)
    assert "카나브젯 /" not in result["answer"]
    assert "카나브젯" in result["answer"]


def test_bare_sales_question_requires_only_brand_axis() -> None:
    answer = enforce_market_answer_contract(
        question="매출?",
        answer="매출 데이터가 확보되지 않았습니다.",
        tool_calls=[],
    )

    assert answer.startswith("브랜드를 지정해 주세요.")
    assert "시장·기간" not in answer
