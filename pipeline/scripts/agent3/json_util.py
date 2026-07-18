from __future__ import annotations

import json
from typing import Any


def parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_history(value: Any) -> dict[str, float]:
    payload = parse_json_object(value)
    result: dict[str, float] = {}
    for period, amount in payload.items():
        if isinstance(amount, dict):
            amount = amount.get("raw_value")
        try:
            result[str(period)] = float(amount or 0.0)
        except (TypeError, ValueError):
            result[str(period)] = 0.0
    return result


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

