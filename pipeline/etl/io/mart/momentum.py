"""Compatibility exports for market-share momentum formulas.

New cross-layer callers should import :mod:`pipeline.domain.momentum`.
"""

from pipeline.domain.momentum import compute_market_share_momentum, compute_momentum


__all__ = ("compute_market_share_momentum", "compute_momentum")
