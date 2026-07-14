from __future__ import annotations

import pytest

from jw_chat_agent_poc.agent_loop import loop as agent_loop
from jw_chat_agent_poc.agent_loop.models import AgentObservation
from jw_chat_agent_poc.agent_loop.planner import _messages
from jw_chat_agent_poc.agent_loop.tools import AgentToolFacade
from jw_chat_agent_poc.resolver import BrandResolver, UnsupportedBrandError

from test_stage2_agent_loop import _metrics_tool


def _observation(step: int, tool_name: str, render_data: dict) -> AgentObservation:
    return AgentObservation(
        step=step,
        tool_name=tool_name,
        arguments={"brand": "리바로"},
        status="ok",
        preview="ok",
        call={"tool": tool_name, "render_data": render_data},
    )


def test_planner_brand_projection_exposes_full_market_on_first_expanded_step() -> None:
    # Given: market scope has resolved a large canonical member set.
    observations = (
        _observation(
            1,
            "get_market_scope",
            {"anchor_brand": "리바로", "member_brands": ("리바로", "로수젯", "피타틴")},
        ),
    )

    # When: the expanded market enum has not yet been sent to the planner.
    projected = agent_loop._planner_allowed_brands(("리바로",), observations, expanded_members_exposed=False)

    # Then: every canonical member remains available for safe planner selection.
    assert projected == ("리바로", "로수젯", "피타틴")


def test_planner_brand_projection_omits_unreferenced_members_after_full_exposure() -> None:
    # Given: the planner already saw the full market and selected a minor member.
    observations = (
        _observation(
            1,
            "get_market_scope",
            {"anchor_brand": "리바로", "member_brands": ("리바로", "로수젯", "피타틴")},
        ),
        _observation(2, "get_metric", {"brand": "피타틴", "metric": "sales"}),
    )

    # When: a later planner step is prepared.
    projected = agent_loop._planner_allowed_brands(("리바로",), observations, expanded_members_exposed=True)

    # Then: the anchor and observed minor brand survive without repeating all members.
    assert projected == ("리바로", "피타틴")


def test_csd_display_market_cannot_replace_canonical_market_id() -> None:
    observations = [
        _observation(1, "get_market_scope", {"brand": "리바로", "market_id": "ml_006"}),
        _observation(2, "csd_activity_trend", {"brand": "리바로", "market": "LIVALO Market"}),
    ]

    assert agent_loop._observed_market_by_brand(observations) == {"리바로": "ml_006"}


def test_planner_system_message_does_not_duplicate_schema_brand_enum() -> None:
    # Given: the tool schema already owns the canonical brand enum.
    brands = tuple(f"브랜드-{index}" for index in range(470))

    # When: planner messages are rendered.
    system_message = _messages("시장 질문", (), brands, ("latest",))[0]["content"]

    # Then: the system message states the contract without copying 470 names again.
    assert "470 canonical brands" in system_message
    assert "브랜드-0" not in system_message
    assert "브랜드-469" not in system_message


def test_runtime_allowed_set_remains_full_when_planner_schema_is_dieted() -> None:
    # Given: runtime validation owns the full market while the planner sees only the anchor.
    facade = AgentToolFacade(
        metrics=_metrics_tool(),
        resolver=BrandResolver(),
        allowed_brands=("리바로", "피타틴"),
    )

    # When: schemas are projected and a directly requested minor brand is grounded.
    schemas = facade.schemas(("리바로",))
    grounded = facade.ground_arguments("get_metric", {"brand": "피타틴", "measure": "sales"})

    # Then: prompt cost falls without weakening runtime membership validation.
    get_metric = next(schema for schema in schemas if schema["function"]["name"] == "get_metric")
    assert get_metric["function"]["parameters"]["properties"]["brand"]["enum"] == ["리바로"]
    assert grounded["brand"] == "피타틴"


def test_runtime_allowed_set_still_rejects_brand_outside_market() -> None:
    # Given: a market-scoped runtime facade.
    facade = AgentToolFacade(
        metrics=_metrics_tool(),
        resolver=BrandResolver(),
        allowed_brands=("리바로", "피타틴"),
    )

    # When/Then: a canonical brand outside that market is still rejected server-side.
    with pytest.raises(UnsupportedBrandError):
        facade.ground_arguments("get_metric", {"brand": "가드렛", "measure": "sales"})
