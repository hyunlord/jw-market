from __future__ import annotations

from typing import Final


INCOMPATIBLE_COMPARISON_REASON: Final = "incompatible_comparison"


class IncompatibleComparisonError(LookupError):
    """Raised when two known brands belong to different strategic markets."""

    def __init__(
        self,
        *,
        anchor_brand: str,
        comparison_brand: str,
        anchor_market: str,
        comparison_markets: tuple[str, ...],
    ) -> None:
        self.anchor_brand = anchor_brand
        self.comparison_brand = comparison_brand
        self.anchor_market = anchor_market
        self.comparison_markets = comparison_markets
        super().__init__(
            "comparison brand belongs to a different market: "
            f"anchor={anchor_brand} market={anchor_market} "
            f"comparison={comparison_brand} comparison_markets={','.join(comparison_markets)}"
        )
