from __future__ import annotations

import pytest

from jw_chat_agent_poc.agent_loop.loop import ToolUseAgent
from jw_chat_agent_poc.agent_loop.tools import AgentToolFacade
from jw_chat_agent_poc.agent_loop.planner import _brand
from jw_chat_agent_poc.orchestrator.agent import ChatAgent, _catalog_dimension_for_level
from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.resolver.brand_resolver import BrandResolver
from jw_chat_agent_poc.service.app import compute_final_answer
from jw_chat_agent_poc.tools.metrics.cache_live import StaticMetricsCacheReader
from jw_chat_agent_poc.tools.query_layer import MartRecord, StaticStrategicMartReader, StrategicQueryLayer


class StaticMembershipReader:
    def __init__(self, rows: tuple[dict[str, str], ...]) -> None:
        self.rows = rows

    def brand_memberships(self) -> tuple[dict[str, str], ...]:
        return self.rows


def _resolver() -> BrandResolver:
    return BrandResolver(
        mode="cache",
        brand_reader=StaticMetricsCacheReader(
            cache_brands=[
                {"brand": "리바로", "market_id": "strategy_006", "market_name": "JW 스타틴"},
            ],
            market_status=[],
        ),
        membership_reader=StaticMembershipReader(
            (
                {"brand": "리바로", "market_id": "ml_006", "market_name": "스타틴"},
                {"brand": "리바로", "market_id": "ml_008", "market_name": "복합제 Class"},
                {"brand": "리바로하이", "market_id": "ml_008", "market_name": "복합제 Class"},
            )
        ),
    )


def _mixed_market_resolver() -> BrandResolver:
    return BrandResolver(
        mode="cache",
        brand_reader=StaticMetricsCacheReader(cache_brands=[], market_status=[]),
        membership_reader=StaticMembershipReader(
            (
                {"brand": "마운자로", "market_id": "ml_003", "market_name": "당뇨병 시장"},
                {
                    "brand": "리바로",
                    "market_id": "ml_006",
                    "market_name": "고지혈증 치료제 시장",
                },
            )
        ),
    )


def test_resolver_preserves_all_brand_market_memberships() -> None:
    resolution = _resolver().resolve("리바로 매출", allow_default=False)

    assert resolution.market_id is None
    assert resolution.market_ids == ("ml_006", "ml_008")
    assert resolution.requires_market_clarification is True


def test_explicit_market_selects_matching_membership() -> None:
    resolution = _resolver().resolve("리바로 ml_008 Class 매출", allow_default=False)

    assert resolution.market_id == "ml_008"
    assert resolution.market_ids == ("ml_006", "ml_008")
    assert resolution.requires_market_clarification is False


def test_explicit_market_name_selects_matching_membership() -> None:
    resolution = _resolver().resolve("리바로 복합제 Class 매출", allow_default=False)

    assert resolution.market_id == "ml_008"
    assert resolution.requires_market_clarification is False


def test_resolver_rejects_explicit_market_outside_brand_membership() -> None:
    resolution = _mixed_market_resolver().resolve(
        "고지혈증 시장에서 마운자로 점유율",
        allow_default=False,
    )

    assert resolution.market_id is None
    assert resolution.requested_market_id == "ml_006"
    assert resolution.requested_market_name == "고지혈증 치료제 시장"
    assert resolution.has_market_membership_mismatch is True


def test_resolver_accepts_data_derived_market_name_alias_for_own_membership() -> None:
    resolution = _mixed_market_resolver().resolve(
        "당뇨병 시장에서 마운자로 점유율",
        allow_default=False,
    )

    assert resolution.market_id == "ml_003"
    assert resolution.requested_market_id == "ml_003"
    assert resolution.has_market_membership_mismatch is False


def test_chat_rejects_explicit_market_outside_brand_membership_before_tools() -> None:
    result = ChatAgent(resolver=_mixed_market_resolver()).answer(
        "고지혈증 시장에서 마운자로 점유율"
    )

    assert result["tool_calls"] == []
    assert "마운자로는 요청한 고지혈증 치료제 시장에 포함되지 않습니다" in result["answer"]
    assert "당뇨병 시장" in result["answer"]


def test_agent_loop_rejects_explicit_market_outside_brand_membership_before_tools() -> None:
    result = ToolUseAgent(
        metrics=MetricsTool(mode="fixture"),
        resolver=_mixed_market_resolver(),
    ).answer("고지혈증 시장에서 마운자로 점유율")

    assert result["tool_calls"] == []
    assert result["router_diagnostics"]["scope"] == "market_membership_mismatch"
    assert "마운자로는 요청한 고지혈증 치료제 시장에 포함되지 않습니다" in result["answer"]


def test_chat_returns_shared_market_clarification_instead_of_first_market() -> None:
    result = ChatAgent(resolver=_resolver()).answer("리바로 매출")

    assert result["tool_calls"] == []
    assert "스타틴·복합제 Class" in result["answer"]
    assert "어느 시장 기준" in result["answer"]


def test_market_clarification_bypasses_final_llm_synthesis(monkeypatch: pytest.MonkeyPatch) -> None:
    result = ChatAgent(resolver=_resolver()).answer("리바로 매출")

    def unexpected_stream(*args: object, **kwargs: object) -> object:
        raise AssertionError("market clarification must not enter final LLM synthesis")

    monkeypatch.setattr(
        "jw_chat_agent_poc.service.app.GenosClient.stream_answer",
        unexpected_stream,
    )

    final = compute_final_answer("리바로 매출", result, "hardcode-gate-test")

    assert final.text == "리바로는 스타틴·복합제 Class 여러 시장에 속합니다. 어느 시장 기준으로 볼지 지정해 주세요."


def test_mixed_market_question_without_brand_asks_for_clarification() -> None:
    result = ChatAgent().answer(
        "업로드한 시장 전망이랑 실제 우리 점유율 비교",
        documents=[__file__],
    )

    assert result["tool_calls"] == []
    assert "브랜드 또는 시장을 지정" in result["answer"]


def test_portfolio_brands_excludes_global_catalog_members() -> None:
    portfolio = _resolver().portfolio_brands()

    assert tuple(item.canonical_brand for item in portfolio) == ("리바로",)


@pytest.mark.parametrize(
    "spelling",
    (
        " 리바로하이 ",
        "리바로  하이",
        "리바로\t하이",
        "리바로\u3000하이",
        "\uff2c\uff29\uff36\uff21\uff2c\uff2f\uff28\uff29\uff27\uff28",
    ),
)
def test_brand_whitespace_and_width_variants_resolve(spelling: str) -> None:
    resolution = _resolver().resolve(spelling, allow_default=False)

    assert resolution.canonical_brand == "리바로하이"


def test_planner_refuses_to_choose_a_default_brand() -> None:
    with pytest.raises(LookupError, match="brand is unresolved"):
        _brand("매출 알려줘", ())


def test_ml008_class_capability_is_inferred_from_catalog_records() -> None:
    layer = StrategicQueryLayer(reader=StaticStrategicMartReader(_ml008_records()))

    catalog = layer.catalog_for_brand("리바로하이", market="ml_008")

    assert catalog.market == "ml_008"
    assert catalog.market_structure["type"] == "class_split"
    assert "class_2" in catalog.dimensions


def test_class_filter_uses_catalog_display_axis() -> None:
    layer = StrategicQueryLayer(reader=StaticStrategicMartReader(_ml008_records()))

    dimension = _catalog_dimension_for_level(layer, "리바로하이", "ml_008", "Class")

    assert dimension == "class_2"


def test_ml008_selection_reaches_query_execution() -> None:
    layer = StrategicQueryLayer(reader=StaticStrategicMartReader(_ml008_records()))
    facade = AgentToolFacade(
        metrics=MetricsTool(query_layer=layer),
        resolver=_resolver(),
        allowed_brands=("리바로하이",),
        query_layer=layer,
        market_by_brand={"리바로하이": "ml_008"},
    )

    execution = facade.execute(
        "query",
        {
            "brand": "리바로하이",
            "spec": '{"group_by":["class_2"],"metrics":["sales"]}',
        },
    )

    assert execution.status == "ok"
    assert execution.call["render_data"]["market_id"] == "ml_008"
    assert execution.call["render_data"]["level_segments"]


def test_explicit_market_reaches_derived_metric_execution() -> None:
    layer = StrategicQueryLayer(reader=StaticStrategicMartReader(_ml008_records()))

    call = layer.brand_metric("리바로하이", "momentum", "latest", market="ml_008")

    assert call["render_data"]["market_id"] == "ml_008"


def test_denominator_note_never_uses_static_counterpart_constant() -> None:
    calls = [
        {
            "source": "UBIST",
            "tool": "get_brand_metric",
            "render_data": {
                "brand": "리바로",
                "metric": "rank",
                "period": "2026-05",
                "rank": 6,
                "total_brands_in_market": 555,
                "market_id": "ml_006",
                "query_spec": {
                    "market": "ml_006",
                    "rank": 6,
                    "total_brands_in_market": 555,
                },
            },
        }
    ]

    fact = answer_fact_markdown(calls, ["UBIST"])

    assert "555" in fact
    assert "470" not in fact
    assert "516" not in fact


def test_internal_class_axis_is_rendered_as_public_class_label() -> None:
    from jw_chat_agent_poc.service.markdown_cleanup import scrub_internal_terminology

    answer = scrub_internal_terminology("시장 축 class_2 기준이며 class_1은 상위 분류입니다.")

    assert answer == "시장 축 Class 2 기준이며 Class 1은 상위 분류입니다."
    assert "class_" not in answer


def _ml008_records() -> tuple[MartRecord, ...]:
    return tuple(
        MartRecord(
            ml_id="ml_008",
            brand_name=brand,
            source="ubist",
            measure="sales",
            metric_history={
                f"{year}-05": {"raw_value": value + (year - 2022), "ms": (value + (year - 2022)) / 11}
                for year in range(2022, 2027)
            },
            channel_data={},
            specialty_data={},
            dimension_data={},
            by_dimension={"class_1": "복합제", "class_2": class_2},
        )
        for brand, class_2, value in (
            ("리바로하이", "고용량", 1.0),
            ("리바로브이", "저용량", 2.0),
        )
    )
