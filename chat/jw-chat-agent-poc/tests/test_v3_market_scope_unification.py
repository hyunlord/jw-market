from __future__ import annotations

import pytest

from jw_chat_agent_poc.tool_use.internal_adapters import InternalToolAdapterRegistry
from jw_chat_agent_poc.tool_use.market_scope_execution import (
    AmbiguousFamilyError,
    AmbiguousMarketError,
    InvalidMarketLabelError,
    MarketScopeKind,
    NoAnchorError,
    ScopeResolver,
    UnknownBrandError,
    UnsupportedSourceError,
)
from jw_chat_agent_poc.tool_use.v3_selection import selection_tool_specs
from v3_market_scope_fakes import FakeGeneralMembership, FakeStrategicLayer, make_backend
from jw_chat_agent_poc.tools.general_view_backend import AtcCandidate
from jw_chat_agent_poc.tools.general_view_membership import GeneralMembershipResolution


def test_no_strategic_membership_is_the_only_implicit_general_fallback() -> None:
    backend, strategic, general = make_backend()

    result = backend.execute_catalog_tool(
        "market.get_brand_metric",
        {"brand": "아일리아", "metric": "sales"},
    )

    assert strategic.calls == []
    assert general.calls == [("S01P0", "아일리아", "iqvia", "sales")]
    assert result["render_data"]["view_type"] == "general_view"
    assert result["render_data"]["scope_trace"]["fallback_reason"] == "no_strategic_membership"


def test_confirmed_general_membership_overrides_stale_existing_route_hint() -> None:
    strategic = FakeStrategicLayer()
    resolver = ScopeResolver(
        strategic_memberships=strategic.brand_memberships,
        general_membership=FakeGeneralMembership(),
        route_hint="existing",
    )

    resolution = resolver.resolve({"brand": "아일리아", "metric": "sales"})

    assert resolution.scope.kind is MarketScopeKind.GENERAL_ATC4
    assert resolution.scope.atc4 == ("S01P0",)
    assert resolution.fallback_reason == "no_strategic_membership"


@pytest.mark.parametrize(
    ("brand", "error_type"),
    (
        ("카나브패밀리", AmbiguousFamilyError),
        ("이 시장", NoAnchorError),
        ("존재하지않는브랜드XYZ987654", UnknownBrandError),
    ),
)
def test_existing_route_hint_preserves_specific_unresolved_failures(
    brand: str,
    error_type: type[LookupError],
) -> None:
    strategic = FakeStrategicLayer()
    resolver = ScopeResolver(
        strategic_memberships=strategic.brand_memberships,
        general_membership=FakeGeneralMembership(),
        route_hint="existing",
    )

    with pytest.raises(error_type):
        resolver.resolve({"brand": brand, "metric": "sales"})


def test_multiple_atc4_memberships_are_not_reported_as_family_language() -> None:
    class MultipleMarketMembership:
        def resolve(
            self,
            brand: str,
            source: str,
        ) -> GeneralMembershipResolution | None:
            if brand != "복수시장브랜드" or source != "iqvia":
                return None
            return GeneralMembershipResolution(
                brand_key=brand,
                brand_name=brand,
                candidates=(
                    AtcCandidate("A10B0", "첫 번째 시장"),
                    AtcCandidate("A10C0", "두 번째 시장"),
                ),
            )

    resolver = ScopeResolver(
        strategic_memberships=FakeStrategicLayer().brand_memberships,
        general_membership=MultipleMarketMembership(),
    )

    with pytest.raises(AmbiguousMarketError, match="A10B0,A10C0"):
        resolver.resolve({"brand": "복수시장브랜드", "metric": "sales"})


def test_existing_strategic_call_is_byte_for_byte_delegate_equivalent() -> None:
    backend, strategic, _general = make_backend()
    arguments = {"brand": "리바로", "metric": "sales", "period": "latest"}

    direct = strategic.brand_metric("리바로", "sales", "latest")
    strategic.calls.clear()
    scoped = backend.execute_catalog_tool("market.get_brand_metric", arguments)

    assert scoped == direct
    assert strategic.calls == [
        (
            "brand_metric",
            ("리바로", "sales", "latest"),
            {"market": None, "source": "", "history_points": 10},
        )
    ]


def test_unsupported_strategic_source_is_not_swallowed_by_general_fallback() -> None:
    backend, _strategic, general = make_backend()

    with pytest.raises(LookupError, match="unavailable for source=IQVIA"):
        backend.execute_catalog_tool(
            "market.get_brand_metric",
            {"brand": "리바로", "metric": "sales", "source": "IQVIA"},
        )

    assert general.calls == []


def test_view_labels_are_moved_to_view_and_recorded() -> None:
    backend, _strategic, _general = make_backend()

    result = backend.execute_catalog_tool(
        "market.get_brand_metric",
        {"brand": "리바로", "metric": "sales", "market": "전략뷰"},
    )

    assert result["scope_trace"] == {
        "normalizations": ("market:전략뷰->view:strategic",),
        "requested_view": "strategic",
    }
    assert result["render_data"]["market_id"] == "resolved"


def test_explicit_strategic_scope_passes_market_id_to_existing_layer() -> None:
    backend, strategic, _general = make_backend()

    backend.execute_catalog_tool(
        "market.get_brand_metric",
        {
            "brand": "리바로",
            "metric": "sales",
            "scope": {"kind": "strategic", "market_id": "ml_006"},
        },
    )

    assert strategic.calls[-1][2]["market"] == "ml_006"


@pytest.mark.parametrize(
    "scope",
    (
        {
            "kind": "strategic",
            "market_id": "ml_006",
            "filters": {"channel": ["hospital"]},
        },
        {
            "kind": "general_atc4",
            "atc4": ["S01P0"],
            "filters": {"channel": ["hospital"]},
        },
    ),
)
def test_unimplemented_scope_filters_fail_closed(scope: dict[str, object]) -> None:
    backend, strategic, general = make_backend()

    with pytest.raises(
        InvalidMarketLabelError,
        match="scope filters are only valid for general_composite",
    ):
        backend.execute_catalog_tool(
            "market.get_brand_metric",
            {"brand": "리바로", "metric": "sales", "scope": scope},
        )

    assert strategic.calls == []
    assert general.calls == []


def test_general_surface_reproduces_legacy_s01p0_values() -> None:
    backend, _strategic, _general = make_backend()

    sales = backend.execute_catalog_tool(
        "market.get_brand_metric", {"brand": "아일리아", "metric": "sales"}
    )["render_data"]
    share = backend.execute_catalog_tool(
        "market.get_brand_metric", {"brand": "아일리아", "metric": "share"}
    )["render_data"]
    rank = backend.execute_catalog_tool(
        "market.get_brand_metric", {"brand": "아일리아", "metric": "rank"}
    )["render_data"]
    hhi = backend.execute_catalog_tool("market.get_hhi", {"brand": "아일리아"})[
        "render_data"
    ]
    members = backend.execute_catalog_tool(
        "market.get_market_members", {"brand": "아일리아"}
    )["render_data"]

    assert round(sales["value"] / 100_000_000, 1) == 218.7
    assert round(share["value"], 2) == 51.38
    assert rank["value"] == 1
    assert hhi["value"] == 3188.0404
    assert members["market_size_recent_krw"] == 42_559_564_361.0
    assert members["member_population_count"] == 10
    assert members["member_population"][-1] == "비쥬다인"
    assert members["active_member_count"] == 9
    assert members["active_members"][0] == "아일리아"
    assert members["active_members_period"] == "2026-Q1"
    assert members["display_member_count"] == 5
    assert members["display_members"][0] == "아일리아"
    assert "total_brands_in_market" not in members


@pytest.mark.parametrize(
    ("raw_hhi", "display_hhi"),
    (
        (3188.040362260885, 3188.0404),
        (3015.4124533412323, 3015.4125),
        (5652.065915370253, 5652.0659),
    ),
)
def test_hhi_is_rounded_only_at_the_display_boundary(
    raw_hhi: float,
    display_hhi: float,
) -> None:
    from jw_chat_agent_poc.tool_use.market_scope_projection import rounded_hhi

    assert rounded_hhi(raw_hhi) == display_hhi


@pytest.mark.parametrize(
    ("arguments", "error_type"),
    (
        ({"brand": "카나브패밀리", "metric": "sales"}, AmbiguousFamilyError),
        ({"brand": "존재하지않는브랜드XYZ987654", "metric": "sales"}, UnknownBrandError),
        ({"brand": "이 시장"}, NoAnchorError),
        ({"brand": "리바로", "market": "고지혈증"}, InvalidMarketLabelError),
        ({"brand": "아일리아", "source": "UNKNOWN", "metric": "sales"}, UnsupportedSourceError),
    ),
)
def test_scope_failures_remain_typed(
    arguments: dict[str, object], error_type: type[LookupError]
) -> None:
    backend, _strategic, _general = make_backend()
    tool = "market.get_brand_metric" if "metric" in arguments else "market.get_hhi"

    with pytest.raises(error_type):
        backend.execute_catalog_tool(tool, arguments)


def test_scope_contract_is_additive_to_all_market_selection_schemas() -> None:
    market_specs = [spec for spec in selection_tool_specs() if spec.name.startswith("market.")]

    assert len(market_specs) == 9
    for spec in market_specs:
        properties = spec.input_model.model_json_schema()["properties"]
        assert "scope" in properties
        assert "view" in properties


def test_internal_registry_uses_scope_aware_entrypoint_only_for_market_tools() -> None:
    backend, _strategic, _general = make_backend()
    registry = InternalToolAdapterRegistry(market_layer=backend)

    result = registry.execute(
        "market.get_brand_metric", {"brand": "아일리아", "metric": "sales"}
    )

    assert result["render_data"]["view_type"] == "general_view"
