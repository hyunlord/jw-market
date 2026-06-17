"""Response composition for dynamic market runtime metrics."""

from __future__ import annotations

from dataclasses import dataclass

from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, MarketDefinition
from pipeline.scripts.api.models.dynamic_market import (
    DynamicMarketBrand,
    DynamicMarketDefinition,
    DynamicMarketMetrics,
    DynamicMarketResponse,
)


@dataclass(frozen=True, slots=True)
class ResponseComposer:
    """Serialize resolver and aggregator outputs into the public API schema."""

    def compose(self, *, definition: MarketDefinition, metrics: AggregatedMetrics) -> DynamicMarketResponse:
        """Return a stable response object for FastAPI/Pydantic serialization."""

        return DynamicMarketResponse(
            market_definition=DynamicMarketDefinition(
                filter_echo=definition.filter_echo,
                brand_count=len(definition.brands),
                brand_list=[
                    {"brand_key": item.brand_key, "brand_name": item.brand_name, "atc4_code": item.atc4_code}
                    for item in definition.brands
                ],
            ),
            metrics=DynamicMarketMetrics(
                market_size=metrics.market_size,
                hhi=metrics.hhi,
                cagr=metrics.cagr,
                monthly_series=[dict(item) for item in metrics.monthly_series],
                brands=[
                    DynamicMarketBrand(
                        brand_key=item.brand_key,
                        brand_name=item.brand_name,
                        atc4_code=item.atc4_code,
                        total_value=item.total_value,
                        market_share_pct=item.market_share_pct,
                        rank=item.rank,
                        latest_period=item.latest_period,
                        latest_value=item.latest_value,
                        monthly_series=[dict(point) for point in item.monthly_series],
                    )
                    for item in metrics.brands
                ],
            ),
            computed="runtime",
        )
