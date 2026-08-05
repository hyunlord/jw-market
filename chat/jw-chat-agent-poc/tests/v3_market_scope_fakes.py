from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from jw_chat_agent_poc.tool_use.market_scope_execution import (
    MarketScopeCatalogBackend,
    ScopeResolver,
)
from jw_chat_agent_poc.tools.general_view_backend import (
    AtcCandidate,
    BrandMetricPoint,
    GeneralMarket,
    TopBrand,
)
from jw_chat_agent_poc.tools.general_view_membership import GeneralMembershipResolution


class FakeStrategicLayer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def brand_memberships(self) -> tuple[dict[str, str], ...]:
        return (
            {"brand": "리바로", "market_id": "ml_006", "market_name": "ml_006"},
            {"brand": "가드렛", "market_id": "ml_003", "market_name": "ml_003"},
            {"brand": "헴리브라", "market_id": "ml_013", "market_name": "ml_013"},
        )

    def brand_metric(
        self,
        brand: str,
        metric: str,
        period: str,
        market: str | None = None,
        source: str = "",
        history_points: int = 10,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "brand_metric",
                (brand, metric, period),
                {"market": market, "source": source, "history_points": history_points},
            )
        )
        if (brand, source.lower()) in {("리바로", "iqvia"), ("헴리브라", "ubist")}:
            raise LookupError(f"{metric} is unavailable for source={source}")
        return {
            "source": source or "UBIST",
            "tool": "get_brand_metric",
            "summary_text": "strategic",
            "render_data": {"brand": brand, "market_id": market or "resolved", "metric": metric},
        }

    def market_scope(self, brand: str, market: str | None = None) -> dict[str, Any]:
        self.calls.append(("market_scope", (brand,), {"market": market}))
        return {
            "source": "UBIST",
            "tool": "get_market_landscape",
            "summary_text": "strategic",
            "render_data": {"brand": brand, "market_id": market or "resolved"},
        }

    def dimension_breakdown(
        self,
        brand: str,
        dimension: str,
        source: str = "",
        period: str = "latest",
        limit: int = 10,
        market: str | None = None,
        metric: str = "sales",
    ) -> dict[str, Any]:
        self.calls.append(("dimension_breakdown", (brand, dimension), {"market": market}))
        return {"source": source or "UBIST", "tool": "get_brand_metric", "render_data": {}}

    def market_member_metric(
        self,
        brand: str,
        comparison: str,
        market: str | None = None,
        metric: str = "series",
    ) -> dict[str, Any]:
        self.calls.append(("market_member_metric", (brand, comparison), {"market": market}))
        return {"source": "UBIST", "tool": "get_brand_metric", "render_data": {}}


class FakeGeneralMembership:
    def resolve(self, brand: str, source: str) -> GeneralMembershipResolution | None:
        if brand not in {"아일리아", "비오뷰", "리바로"} or source != "iqvia":
            return None
        return GeneralMembershipResolution(
            brand_key=brand,
            brand_name=brand,
            candidates=(AtcCandidate("S01P0", "안과용 VEGF 억제제"),),
        )


@dataclass
class FakeGeneralBackend:
    calls: list[tuple[str, str | None, str, str]]

    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]:
        resolution = FakeGeneralMembership().resolve(brand, source)
        return resolution.candidates if resolution is not None else ()

    def market(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMarket:
        self.calls.append((atc4, brand, source, measure))
        rows = (
            ("아일리아", 1, 21_867_326_960.0, 51.38052348119992),
            ("루센티스", 2, 7_000_000_000.0, 16.447),
            ("바비스모", 3, 5_000_000_000.0, 11.748),
            ("비오뷰", 4, 3_000_000_000.0, 7.049),
            ("아바스틴", 5, 2_000_000_000.0, 4.699),
            ("브랜드6", 6, 1_500_000_000.0, 3.524),
            ("브랜드7", 7, 1_000_000_000.0, 2.35),
            ("브랜드8", 8, 700_000_000.0, 1.645),
            ("브랜드9", 9, 492_237_401.0, 1.157),
        )
        members = tuple(TopBrand(*row) for row in rows)
        member_population = tuple(row.brand for row in members) + ("비쥬다인",)
        selected = next((row for row in members if row.brand == brand), None)
        series = (
            BrandMetricPoint("2025-Q4", 20_000_000_000.0, 49.0, 1),
            BrandMetricPoint(
                "2026-Q1",
                selected.value if selected else None,
                selected.share_pct if selected else None,
                selected.rank if selected else None,
            ),
        )
        return GeneralMarket(
            view_type="general_view",
            market_basis="ATC4",
            atc4_code=atc4,
            atc4_description="안과용 VEGF 억제제",
            source="IQVIA",
            measure=measure,
            unit="KRW",
            period="2026-Q1",
            market_size=42_559_564_361.0,
            brand=brand,
            brand_value=selected.value if selected else None,
            brand_share_pct=selected.share_pct if selected else None,
            brand_rank=selected.rank if selected else None,
            top_brands=members[:5],
            market_size_series=(("2025-Q4", 40_000_000_000.0), ("2026-Q1", 42_559_564_361.0)),
            member_brands=members,
            member_population=member_population,
            active_members=members,
            display_members=members[:5],
            selected_data_path="direct_mart",
            hhi_recent=3188.040362260885,
            brand_metric_series=series,
        )

    def composite_market(
        self,
        atc4: tuple[str, ...],
        filters: tuple[tuple[str, tuple[str, ...]], ...],
        brand: str | None,
        source: str,
        measure: str,
    ) -> GeneralMarket:
        market = self.market(atc4[0], brand, source, measure)
        return replace(
            market,
            market_basis="ATC4 composite",
            atc4_code=",".join(atc4),
            atc4_codes=atc4,
            scope_filters=filters,
            selected_data_path="dynamic_market_composite",
            dashboard_tables=(
                {
                    "name": "브랜드 순위",
                    "columns": ("순위", "브랜드", "최근 값", "점유율(%)"),
                    "rows": tuple(
                        (row.rank, row.brand, row.value, row.share_pct)
                        for row in market.display_members
                    ),
                },
            ),
        )


def make_backend() -> tuple[MarketScopeCatalogBackend, FakeStrategicLayer, FakeGeneralBackend]:
    strategic = FakeStrategicLayer()
    general = FakeGeneralBackend([])
    resolver = ScopeResolver(
        strategic_memberships=strategic.brand_memberships,
        general_membership=FakeGeneralMembership(),
    )
    return MarketScopeCatalogBackend(strategic, resolver, general), strategic, general
