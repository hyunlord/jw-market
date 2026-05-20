from __future__ import annotations


def to_strategy_id(market_id: str) -> str:
    """Convert internal ml_NNN ids to the v0.9 API strategy_NNN form."""
    if market_id.startswith("ml_"):
        return f"strategy_{market_id[3:]}"
    return market_id


def to_ml_id(market_id: str) -> str:
    """Convert v0.9 strategy_NNN ids back to the internal ml_NNN form."""
    if market_id.startswith("strategy_"):
        return f"ml_{market_id[9:]}"
    return market_id
