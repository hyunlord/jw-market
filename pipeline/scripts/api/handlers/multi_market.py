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
        min(ordered, key=lambda row: (-_latest_market_total(row), str(row["market_id"]))),
    )
    markets = [
        {"market_id": row["market_id"], "is_primary": row["market_id"] == primary["market_id"]}
        for row in ordered
    ]
    return primary, markets


def _latest_market_total(row: dict[str, Any]) -> float:
    payload = row.get("response_json")
    if not isinstance(payload, dict):
        return 0.0
    data = payload.get("data")
    if not isinstance(data, dict):
        return 0.0
    series = data.get("market_size_series")
    if not isinstance(series, list):
        return 0.0
    points = [
        (str(point.get("period") or ""), float(point.get("market_size") or 0.0))
        for point in series
        if isinstance(point, dict)
    ]
    return max(points, default=("", 0.0))[1]
