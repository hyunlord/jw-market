from __future__ import annotations

from typing import Any


def choose_primary_market(
    rows: list[dict[str, Any]],
    *,
    preferred_market_id: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ordered = sorted(rows, key=lambda row: str(row["market_id"]))
    primary = next(
        (row for row in ordered if preferred_market_id and str(row["market_id"]) == preferred_market_id),
        ordered[0],
    )
    markets = [
        {"market_id": row["market_id"], "is_primary": row["market_id"] == primary["market_id"]}
        for row in ordered
    ]
    return primary, markets
