from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .json_util import parse_history


@dataclass(frozen=True, slots=True)
class BrandIdentity:
    brand_key: str
    brand_name: str
    latest_sales: float = 0.0


def canonical_brand_names_from_rows(rows: list[dict[str, Any]]) -> dict[str, str]:
    totals: dict[str, dict[str, float]] = {}
    for row in rows:
        brand_key = str(row.get("brand_key") or "")
        brand_name = str(row.get("brand_name") or "")
        if not brand_key or not brand_name:
            continue
        history = parse_history(row.get("raw_value_history"))
        latest_value = history[max(history)] if history else 0.0
        totals.setdefault(brand_key, {})
        totals[brand_key][brand_name] = totals[brand_key].get(brand_name, 0.0) + latest_value
    result: dict[str, str] = {}
    for brand_key, values_by_name in totals.items():
        result[brand_key] = sorted(values_by_name.items(), key=lambda item: (-item[1], item[0]))[0][0]
    return result


def latest_sales_by_brand_key_from_rows(rows: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in rows:
        brand_key = str(row.get("brand_key") or "")
        if not brand_key:
            continue
        history = parse_history(row.get("raw_value_history"))
        result[brand_key] = result.get(brand_key, 0.0) + (history[max(history)] if history else 0.0)
    return result


def serving_brand_names_for_identities(identities: list[BrandIdentity]) -> dict[str, str | None]:
    representatives: dict[str, BrandIdentity] = {}
    for identity in identities:
        current = representatives.get(identity.brand_name)
        if current is None or identity.latest_sales > current.latest_sales or (
            identity.latest_sales == current.latest_sales and identity.brand_key < current.brand_key
        ):
            representatives[identity.brand_name] = identity
    return {
        identity.brand_key: identity.brand_name if representatives[identity.brand_name].brand_key == identity.brand_key else None
        for identity in identities
    }
