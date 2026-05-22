from __future__ import annotations

from typing import Any


def choose_primary_market(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ordered = sorted(rows, key=lambda row: str(row["market_id"]))
    primary = ordered[0]
    markets = [
        {"market_id": row["market_id"], "is_primary": row["market_id"] == primary["market_id"]}
        for row in ordered
    ]
    return primary, markets
