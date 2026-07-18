from __future__ import annotations

from collections.abc import Iterable, Sequence
import re
from typing import Any

from pipeline.scripts.api import db
from pipeline.scripts.api.catalog import get_display_brand
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.resolvers import normalize_source
from pipeline.scripts.api.dynamic_market.types import DynamicMarketInputError, quote_identifier


ATC_TOKEN_RE = re.compile(r"[A-Z]+|\d+")
PUBLIC_SOURCES = {"ubist", "iqvia"}


def build_market_filter_atc_options(*, brand_name: str | None, view: str, source: str) -> dict[str, object]:
    """Return ATC1~4 key-only option lists for market filter step 1.

    The public contract accepts and echoes only ``ubist`` or ``iqvia``; IQVIA's
    internal ``iqvia_nsa`` source value is resolved behind this boundary.
    """

    normalized_brand = (brand_name or "").strip()
    normalized_view = normalize_view(view)
    if not normalized_brand and normalized_view != "general":
        raise DynamicMarketInputError("brand_name is required")
    public_source = normalize_public_source(source)
    normalized_source = normalize_source(public_source)
    market_id = None
    flagged_atc4: tuple[str, ...] = ()
    if normalized_brand:
        market_id = _resolve_market_id(brand=normalized_brand, view=normalized_view, source=normalized_source)
        flagged_atc4 = _load_brand_atc4_values(
            brand=normalized_brand,
            view=normalized_view,
            source=normalized_source,
            market_id=market_id,
        )
    atc_rows = _load_atc_rows(view=normalized_view, source=normalized_source, market_id=market_id)
    return {
        "brand_name": normalized_brand,
        "view": normalized_view,
        "source": public_source,
        "market_id": market_id,
        "flagged_atc4": list(flagged_atc4),
        "atc": _build_flagged_atc_hierarchy(atc_rows, flagged_atc4),
    }


def normalize_view(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"general", "strategic"}:
        raise DynamicMarketInputError(f"unsupported market filter view: {value}")
    return normalized


def normalize_public_source(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in PUBLIC_SOURCES:
        raise DynamicMarketInputError(f"unsupported market filter source: {value}")
    return normalized


def _resolve_market_id(*, brand: str, view: str, source: str) -> str | None:
    if view == "general":
        atc4_values = _load_brand_atc4_values(brand=brand, view=view, source=source, market_id=None)
        return atc4_values[0] if atc4_values else None

    display_brand = get_display_brand(brand)
    if display_brand is not None:
        return display_brand.ml_id

    rows = db.fetch_all(
        f"""
        SELECT DISTINCT ml_id
        FROM {quote_identifier(config.db_name)}.mart_strategic_ml_brand_metric
        WHERE source = %s
          AND measure = 'sales'
          AND (brand_key = %s OR brand_name = %s OR LOWER(REPLACE(brand_name, ' ', '')) = LOWER(REPLACE(%s, ' ', '')))
        ORDER BY ml_id
        """,
        [source, brand, brand, brand],
    )
    market_ids = tuple(str(row["ml_id"]) for row in rows if row.get("ml_id"))
    if not market_ids:
        return None
    if len(market_ids) > 1:
        raise DynamicMarketInputError(f"ambiguous strategic market for brand: {', '.join(market_ids)}")
    return market_ids[0]


def _load_brand_atc4_values(*, brand: str, view: str, source: str, market_id: str | None) -> tuple[str, ...]:
    if view == "general":
        return _general_brand_atc4_source_values(brand=brand, source=source)

    where = [
        "source = %s",
        "measure = 'sales'",
        "(brand_key = %s OR brand_name = %s OR LOWER(REPLACE(brand_name, ' ', '')) = LOWER(REPLACE(%s, ' ', '')))",
    ]
    params: list[object] = [source, brand, brand, brand]
    if market_id:
        where.append("ml_id = %s")
        params.append(market_id)
    rows = db.fetch_all(
        f"""
        SELECT DISTINCT JSON_UNQUOTE(JSON_EXTRACT(by_dimension, '$.atc4_code')) AS atc4_code
        FROM {quote_identifier(config.db_name)}.mart_strategic_ml_brand_metric
        WHERE {" AND ".join(where)}
        ORDER BY atc4_code
        """,
        params,
    )
    return _unique_atc4(row.get("atc4_code") for row in rows)


def general_brand_atc4_values(*, brand: str, source: str) -> tuple[str, ...]:
    """Return one brand's canonical general-view memberships in stable catalog order."""

    return canonical_atc4_values(_general_brand_atc4_source_values(brand=brand, source=source))


def _general_brand_atc4_source_values(*, brand: str, source: str) -> tuple[str, ...]:
    display_brand = get_display_brand(brand)
    aliases = display_brand.layer3_aliases if display_brand is not None else ()
    alias_predicates = "".join(
        " OR brand_key = %s OR brand_name = %s OR LOWER(REPLACE(brand_name, ' ', '')) = LOWER(REPLACE(%s, ' ', ''))"
        for _ in aliases
    )
    params: list[object] = [source, brand, brand, brand]
    for alias in aliases:
        params.extend((alias, alias, alias))

    rows = db.fetch_all(
        f"""
        SELECT DISTINCT atc4_code
        FROM {quote_identifier(config.db_name)}.mart_general_brand_metric
        WHERE source = %s
          AND measure = 'sales'
          AND (brand_key = %s OR brand_name = %s OR LOWER(REPLACE(brand_name, ' ', '')) = LOWER(REPLACE(%s, ' ', '')){alias_predicates})
        ORDER BY atc4_code
        """,
        params,
    )
    values = _raw_unique_atc4(row.get("atc4_code") for row in rows)
    return values if source == "ubist" else canonical_atc4_values(values)


def canonical_atc4_values(values: Iterable[Any]) -> tuple[str, ...]:
    """Canonicalize an unordered ATC4 collection using the public filter contract."""

    return _unique_atc4(values)


def _load_atc_rows(*, view: str, source: str, market_id: str | None) -> tuple[str, ...]:
    if view == "strategic":
        where = ["source = %s"]
        params: list[object] = [source]
        if market_id:
            where.append("ml_id = %s")
            params.append(market_id)
        rows = db.fetch_all(
            f"""
            SELECT DISTINCT JSON_UNQUOTE(JSON_EXTRACT(by_dimension, '$.atc4_code')) AS atc4_code
            FROM {quote_identifier(config.db_name)}.mart_strategic_ml_brand_metric
            WHERE {" AND ".join(where)}
            ORDER BY atc4_code
            """,
            params,
        )
        return _unique_atc4(row.get("atc4_code") for row in rows)

    rows = db.fetch_all(
        f"""
        SELECT DISTINCT atc4_code
        FROM {quote_identifier(config.db_name)}.mart_general_brand_metric
        WHERE source = %s
        ORDER BY atc4_code
        """,
        [source],
    )
    values = _raw_unique_atc4(row.get("atc4_code") for row in rows)
    return values if source == "ubist" else canonical_atc4_values(values)


def _raw_unique_atc4(values: Iterable[Any]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value or "").strip().upper()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _unique_atc4(values: Iterable[Any]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = _canonical_atc4_code(str(value or "").strip().upper())
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _canonical_atc4_code(value: str) -> str:
    """Normalize ATC3-shaped mart values when they appear in ATC4 fields."""

    tokens = ATC_TOKEN_RE.findall(value)
    if not tokens:
        return value
    canonical_tokens = list(tokens)
    if len(canonical_tokens) >= 2 and canonical_tokens[1].isdigit() and len(canonical_tokens[1]) == 1:
        canonical_tokens[1] = canonical_tokens[1].zfill(2)
    canonical = "".join(canonical_tokens)
    if len(tokens) == 3 and tokens[0].isalpha() and tokens[1].isdigit() and tokens[2].isalpha():
        return f"{canonical}0"
    return canonical


def _build_flagged_atc_hierarchy(atc4_values: Iterable[str], flagged_atc4: Sequence[str]) -> dict[str, list[dict[str, object]]]:
    flagged_by_level: dict[str, set[str]] = {"atc1": set(), "atc2": set(), "atc3": set(), "atc4": set()}
    for code in flagged_atc4:
        parsed = parse_atc_code(code)
        if parsed is None:
            continue
        for level, value in parsed.items():
            flagged_by_level[level].add(value)

    buckets: dict[str, set[str]] = {"atc1": set(), "atc2": set(), "atc3": set(), "atc4": set()}
    for code in atc4_values:
        parsed = parse_atc_code(code)
        if parsed is None:
            continue
        for level, value in parsed.items():
            buckets[level].add(value)

    hierarchy: dict[str, list[dict[str, object]]] = {}
    for level in ("atc1", "atc2", "atc3", "atc4"):
        hierarchy[level] = [
            {
                "key": value,
                "level": level,
                "parent": _parent_for_atc_level(level, value),
                "flag": value in flagged_by_level[level],
            }
            for value in sorted(buckets[level])
        ]
    return hierarchy


def _parent_for_atc_level(level: str, value: str) -> str | None:
    parsed = parse_atc_code(value)
    if parsed is None:
        return None
    match level:
        case "atc1":
            return None
        case "atc2":
            return parsed.get("atc1")
        case "atc3":
            return parsed.get("atc2")
        case "atc4":
            return parsed.get("atc3")
        case _:
            raise DynamicMarketInputError(f"unsupported ATC level: {level}")


def parse_atc_code(code: str) -> dict[str, str] | None:
    normalized = code.strip().upper()
    if not normalized:
        return None
    tokens = ATC_TOKEN_RE.findall(normalized)
    if not tokens:
        return {"atc1": normalized[:1], "atc2": normalized[:3], "atc3": normalized[:4], "atc4": normalized}
    canonical_tokens = tuple(token.zfill(2) if token.isdigit() and len(token) == 1 else token for token in tokens)
    levels = {
        "atc1": canonical_tokens[0],
        "atc2": "".join(canonical_tokens[:2]),
        "atc3": "".join(canonical_tokens[:3]),
        "atc4": normalized,
    }
    return {level: value for level, value in levels.items() if value}
