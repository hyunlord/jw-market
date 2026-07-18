"""Parse database JSON values used by the post-reload FDM contract."""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
from typing import Any


def rows(value: Any) -> list[Mapping[str, Any]]:
    """Return only mapping rows from a captured evidence collection."""

    return [row for row in value or [] if isinstance(row, Mapping)]


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        stripped = value.strip()
        return json.loads(stripped) if stripped else {}
    return value


def _point_value(point: Any) -> float | None:
    if isinstance(point, Mapping):
        point = next(
            (
                point[key]
                for key in ("raw_value", "value", "market_size", "total", "sales")
                if point.get(key) is not None
            ),
            None,
        )
    if point is None:
        return None
    try:
        number = float(point)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def series(value: Any) -> dict[str, float | None]:
    """Normalize supported history encodings without inventing explicit values."""

    parsed = _json_value(value)
    if isinstance(parsed, Mapping):
        return {str(period): _point_value(point) for period, point in sorted(parsed.items())}
    if isinstance(parsed, list):
        result: dict[str, float | None] = {}
        for point in parsed:
            if not isinstance(point, Mapping) or not point.get("period"):
                continue
            result[str(point["period"])] = _point_value(point)
        return dict(sorted(result.items()))
    return {}
