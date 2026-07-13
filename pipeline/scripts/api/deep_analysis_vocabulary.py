"""Canonical vocabulary used by deep-analysis serving adapters."""

from typing import Final


STRENGTH_VIEW_KIND_BY_FORMAL_VIEW: Final[dict[str, str]] = {
    "strategic_ml": "market_landscape",
    "strategic_cd": "competitive_dynamics",
}
