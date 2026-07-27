"""Re-export of the canonical ``cache_cause`` key contract.

Agent2 is launched both as a package (repo root on ``sys.path``) and as a script
from ``pipeline/scripts/ai_analysis`` (repo root absent), so the repo root is
bootstrapped explicitly here. The import is deliberately *not* wrapped in a
try/except: reader and producer disagreeing about the key is the defect this
module exists to prevent, so a silent degrade would be worse than an ImportError.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.etl.io.cache.cause_key import (  # noqa: E402
    CACHE_CAUSE_KEY_COLUMNS,
    CACHE_CAUSE_TARGET_KEY_COLUMNS,
    VIEW_COMPETITIVE_DYNAMICS,
    VIEW_MARKET_LANDSCAPE,
    cache_cause_identity,
    cache_market_id,
    cache_view_source_id,
    strategy_id_for,
)

__all__ = [
    "CACHE_CAUSE_KEY_COLUMNS",
    "CACHE_CAUSE_TARGET_KEY_COLUMNS",
    "VIEW_COMPETITIVE_DYNAMICS",
    "VIEW_MARKET_LANDSCAPE",
    "cache_cause_identity",
    "cache_market_id",
    "cache_view_source_id",
    "strategy_id_for",
]
