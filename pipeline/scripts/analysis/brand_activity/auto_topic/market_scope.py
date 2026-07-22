from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Literal, TypeAlias

from pipeline.scripts.utils.atc4 import normalize_atc4


TargetMode: TypeAlias = Literal["existing", "all", "uncovered", "explicit", "strategic"]


class TargetSelectionError(ValueError):
    """Raised when an ATC target request cannot be resolved from keyword data."""


def select_target_markets(
    *,
    available_markets: Sequence[str],
    covered_markets: Sequence[str],
    mode: TargetMode,
    explicit_markets: Sequence[str] = (),
) -> tuple[str, ...]:
    """Select deterministic ATC targets from live keyword and topic inventories."""
    available = tuple(sorted(set(available_markets)))
    available_set = set(available)
    covered_set = set(covered_markets)
    explicit = tuple(sorted(set(explicit_markets)))
    if explicit and mode != "explicit":
        raise TargetSelectionError("--target-atc4 requires --target-mode explicit")
    unknown = tuple(market for market in explicit if market not in available_set)
    if unknown:
        raise TargetSelectionError(f"target ATC has no keyword data: {', '.join(unknown)}")
    overlap = tuple(market for market in explicit if market in covered_set)
    if overlap:
        raise TargetSelectionError(f"target ATC already has topic scope: {', '.join(overlap)}")

    match mode:
        case "existing":
            return tuple(market for market in available if market in covered_set)
        case "all":
            return available
        case "uncovered":
            return tuple(market for market in available if market not in covered_set)
        case "explicit":
            if not explicit:
                raise TargetSelectionError("explicit target mode requires --target-atc4")
            return explicit
        case "strategic":
            raise TargetSelectionError("strategic target mode requires strategic catalog selection")
        case unreachable:
            raise TargetSelectionError(f"unsupported target mode: {unreachable}")


def parse_target_markets(value: str) -> tuple[str, ...]:
    """Parse a comma-separated CLI target list into normalized ATC values."""
    return tuple(sorted({item.strip() for item in value.split(",") if item.strip()}))


def parse_target_mode(value: str) -> TargetMode:
    """Parse a CLI mode without silently falling back to a broader target."""
    match value:
        case "existing" | "all" | "uncovered" | "explicit" | "strategic":
            return value
        case unsupported:
            raise TargetSelectionError(f"unsupported target mode: {unsupported}")


def scope_id(atc4: str) -> str:
    """Build a stable market-scope id for one ATC4."""
    return f"atc4:{atc4}"


def select_missing_strategic_scopes(
    *,
    catalog_rows: Sequence[dict[str, object]],
    available_markets: Sequence[str],
    stored_scopes: Sequence[dict[str, object]],
) -> dict[str, object]:
    """Select strategic ML axes whose keyword-bearing ATC set lacks an exact stored scope."""
    available = set(_normalized_atc4_values(available_markets))
    stored = [
        (
            str(row.get("scope_id") or ""),
            set(_normalized_atc4_values(row.get("atc4_values"))),
        )
        for row in stored_scopes
    ]
    census: list[dict[str, object]] = []
    targets: list[dict[str, object]] = []
    for row in sorted(catalog_rows, key=lambda item: str(item.get("ml_id") or "")):
        ml_id = str(row.get("ml_id") or "")
        ml_name = str(row.get("ml_name") or ml_id)
        catalog_atc4_raw = list(_json_string_values(row.get("atc_codes_json")))
        catalog_atc4 = list(_normalized_atc4_values(catalog_atc4_raw))
        keyword_atc4 = [value for value in catalog_atc4 if value in available]
        keyword_set = set(keyword_atc4)
        reachable_scope_ids = sorted(
            scope_id_value
            for scope_id_value, scope_atc4 in stored
            if scope_atc4 and scope_atc4 <= keyword_set
        )
        exact_scope_ids = sorted(
            scope_id_value
            for scope_id_value, scope_atc4 in stored
            if scope_atc4 and scope_atc4 == keyword_set
        )
        if not keyword_atc4:
            status = "no_keyword_data"
        elif exact_scope_ids:
            status = "covered_exact_scope"
        else:
            status = "target_missing_exact_scope"
            targets.append(
                {
                    "ml_id": ml_id,
                    "ml_name": ml_name,
                    "scope_id": f"strategic_ml:{ml_id}",
                    "atc4_values": keyword_atc4,
                    "catalog_atc4": catalog_atc4,
                }
            )
        census.append(
            {
                "ml_id": ml_id,
                "ml_name": ml_name,
                "catalog_atc4_raw": catalog_atc4_raw,
                "catalog_atc4": catalog_atc4,
                "keyword_atc4": keyword_atc4,
                "status": status,
                "reachable_scope_ids": reachable_scope_ids,
            }
        )
    return {"target_scopes": targets, "catalog_census": census}


def strategic_scope_metadata(
    target_scopes: Sequence[dict[str, object]],
    *,
    csd_missing_atc4: Sequence[str] = (),
) -> dict[str, dict[str, object]]:
    """Build execution metadata for approved strategic ML catalog axes."""
    missing = set(csd_missing_atc4)
    metadata: dict[str, dict[str, object]] = {}
    for target in target_scopes:
        scope_id_value = str(target.get("scope_id") or "")
        atc4_values = list(_json_string_values(target.get("atc4_values")))
        missing_values = [value for value in atc4_values if value in missing]
        metadata[scope_id_value] = {
            "scope_key": scope_id_value,
            "scope_id": scope_id_value,
            "scope_type": "strategic_ml",
            "display_name": str(target.get("ml_name") or target.get("ml_id") or scope_id_value),
            "market_id": str(target.get("ml_id") or ""),
            "atc4_values": atc4_values,
            "filter_options": [],
            "source": "catalog_ml_market",
            "csd_market_missing": bool(missing_values),
            "csd_market_missing_atc4": missing_values,
            "display_name_source": "catalog_ml_market",
        }
    return metadata


def _json_string_values(value: object) -> tuple[str, ...]:
    """Normalize a JSON/list ATC value without inventing missing members."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = value.split(",")
    else:
        decoded = value
    values = decoded if isinstance(decoded, (list, tuple, set)) else []
    return tuple(sorted({str(item).strip() for item in values if str(item).strip()}))


def _normalized_atc4_values(value: object) -> tuple[str, ...]:
    """Return canonical ATC4 identities using the shared serving/mart normalizer."""
    return tuple(
        sorted(
            {
                normalized
                for item in _json_string_values(value)
                if (normalized := normalize_atc4(item))
            }
        )
    )
