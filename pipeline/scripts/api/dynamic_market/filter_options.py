"""Filter option list helpers for dynamic market UIs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import json
import os
import re
from threading import RLock
from time import monotonic

from pipeline.scripts.api import db
from pipeline.scripts.api.catalog import get_display_brand
from pipeline.scripts.api.dynamic_market.channel_axis import parse_channel_specialty_matrix
from pipeline.scripts.api.dynamic_market.resolvers import normalize_source
from pipeline.scripts.api.dynamic_market.types import DynamicMarketInputError, quote_identifier
from pipeline.scripts.utils.ubist_channel_mapping import parse_channel_code


GENERAL_DIMENSION_TABLE = "mart_general_filter_dimension_metric"
STRATEGIC_DIMENSION_TABLE = "mart_strategic_filter_dimension_metric"
SELECTABLE_ATC_LEVELS = ("atc3", "atc4")
ATC_TOKEN_RE = re.compile(r"[A-Z]+|\d+")
FILTER_OPTION_CACHE_ENV = "DYNAMIC_MARKET_FILTER_OPTIONS_CACHE"
FILTER_OPTION_CACHE_TTL_ENV = "DYNAMIC_MARKET_FILTER_OPTIONS_CACHE_TTL_SECONDS"
DEFAULT_FILTER_OPTION_CACHE_TTL_SECONDS = 6 * 60 * 60
DIMENSION_LABELS: dict[str, str] = {
    "seller": "판매사",
    "molecule_strength": "성분용량",
    "form": "제형",
    "route": "투여경로",
    "reimbursement": "급여구분",
    "mfr": "MFR NAME KOR",
    "molecule_type": "MOLECULE TYPE",
    "molecule_desc": "성분",
    "strength": "STRENGTH",
    "nhi": "NHI TYPE",
}
DIMENSION_ORDER_HINTS: tuple[str, ...] = (
    "class",
    "molecule",
    "molecule_strength",
    "strength_pack",
    "ox_gx",
    "seller",
    "form",
    "route",
    "reimbursement",
    "mfr",
    "mfr_name_kor",
    "molecule_type",
    "molecule_desc",
    "pack_desc",
    "strength",
    "nhi",
    "nhi_type",
    "audit_code",
)


@dataclass(frozen=True, slots=True)
class DimensionOptionRow:
    dimension_type: str
    dimension_value: str
    dimension_value_norm: str
    row_count: int


@dataclass(frozen=True, slots=True)
class FilterOptionCacheEntry:
    payload: dict[str, object]
    expires_at: float


FilterOptionCacheKey = tuple[str, str, str | None, str, str]
_FILTER_OPTION_CACHE: dict[FilterOptionCacheKey, FilterOptionCacheEntry] = {}
_FILTER_OPTION_CACHE_LOCK = RLock()


def build_filter_options(
    *,
    mart_db: str,
    view: str,
    source: str,
    market_id: str | None = None,
    brand: str | None = None,
    measure: str = "sales",
    atc4_codes: Sequence[str] | None = None,
    selections: Mapping[str, Sequence[str]] | str | None = None,
    general_dimension_db: str | None = None,
    strategic_dimension_db: str | None = None,
) -> dict[str, object]:
    """Return filter options for one source/view, resolving brand-only markets.

    ``market_id`` is kept as a backward-compatible explicit override.  New
    callers should send ``brand`` with ``view`` and ``source``; strategic views
    resolve the catalog ML id, while general views resolve the brand's ATC4
    bucket from the mart and echo that resolved id in the response.
    """

    normalized_view = normalize_view(view)
    normalized_source = normalize_source(source)
    normalized_measure = measure.strip().lower() or "sales"
    normalized_brand = brand.strip() if brand else ""
    resolved_market_id = resolve_filter_option_market_id(
        mart_db=mart_db,
        view=normalized_view,
        source=normalized_source,
        brand=brand,
        market_id=market_id,
    )
    dimension_db = (general_dimension_db if normalized_view == "general" else strategic_dimension_db) or mart_db
    parsed_atc4_codes = _parse_atc4_codes(resolved_market_id, atc4_codes)
    parsed_selections = _parse_selection_map(selections)
    payload = _build_filter_options_uncached(
        mart_db=mart_db,
        dimension_db=dimension_db,
        view=normalized_view,
        source=normalized_source,
        brand=normalized_brand,
        market_id=resolved_market_id,
        measure=normalized_measure,
        atc4_codes=parsed_atc4_codes,
        selections=parsed_selections,
    )
    brand_matched: dict[str, list[str]] = {}
    if normalized_brand:
        payload["brand"] = normalized_brand
        brand_matched = _load_brand_dimension_matches(
            dimension_db=dimension_db,
            brand=normalized_brand,
            view=normalized_view,
            source=normalized_source,
            market_id=resolved_market_id,
            measure=normalized_measure,
        )
        if normalized_view == "strategic":
            brand_matched.update(
                _load_strategic_brand_by_dimension_matches(
                    mart_db=mart_db,
                    brand=normalized_brand,
                    source=normalized_source,
                    market_id=resolved_market_id,
                    measure=normalized_measure,
                )
            )
        if normalized_view == "general" and parsed_atc4_codes:
            brand_matched.setdefault("atc4", [parsed_atc4_codes[0]])
        payload["brand_matched"] = brand_matched
    _apply_option_state(
        payload=payload,
        view=normalized_view,
        atc4_codes=parsed_atc4_codes,
        selections=parsed_selections,
        brand_matched=brand_matched,
    )
    return payload


def resolve_filter_option_market_id(
    *,
    mart_db: str,
    view: str,
    source: str,
    brand: str | None,
    market_id: str | None,
) -> str | None:
    """Resolve the optional market id hidden behind the filter-options API.

    Explicit ``market_id`` stays authoritative for old callers.  Without it,
    strategic views use the 25-brand display catalog and general views use the
    general mart's brand-to-ATC4 mapping.  Missing or unknown brands fall back
    to the source-wide option universe instead of failing the option list.
    """

    explicit_market_id = market_id.strip() if market_id else ""
    if explicit_market_id:
        return explicit_market_id.upper() if view == "general" else explicit_market_id

    normalized_brand = brand.strip() if brand else ""
    if not normalized_brand:
        return None

    if view == "strategic":
        display_brand = get_display_brand(normalized_brand)
        return display_brand.ml_id if display_brand else None

    return _general_market_id_for_brand(mart_db=mart_db, source=source, brand=normalized_brand)


def clear_filter_option_cache() -> None:
    """Clear in-process option payloads after a sidecar refresh or in tests."""

    with _FILTER_OPTION_CACHE_LOCK:
        _FILTER_OPTION_CACHE.clear()


def _filter_option_cache_key(
    *,
    mart_db: str,
    dimension_db: str,
    view: str,
    source: str,
    market_id: str | None,
) -> FilterOptionCacheKey:
    normalized_market_id = market_id.strip() if market_id else None
    return (view, source, normalized_market_id, mart_db, dimension_db)


def _filter_option_cache_enabled() -> bool:
    value = os.getenv(FILTER_OPTION_CACHE_ENV, "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _filter_option_cache_ttl_seconds() -> float:
    raw_value = os.getenv(FILTER_OPTION_CACHE_TTL_ENV, "").strip()
    if not raw_value:
        return float(DEFAULT_FILTER_OPTION_CACHE_TTL_SECONDS)
    try:
        ttl_seconds = float(raw_value)
    except ValueError:
        return float(DEFAULT_FILTER_OPTION_CACHE_TTL_SECONDS)
    return max(0.0, ttl_seconds)


def _get_cached_filter_options(cache_key: FilterOptionCacheKey) -> dict[str, object] | None:
    if not _filter_option_cache_enabled():
        return None
    now = monotonic()
    with _FILTER_OPTION_CACHE_LOCK:
        entry = _FILTER_OPTION_CACHE.get(cache_key)
        if entry is None:
            return None
        if entry.expires_at <= now:
            del _FILTER_OPTION_CACHE[cache_key]
            return None
        return deepcopy(entry.payload)


def _set_cached_filter_options(cache_key: FilterOptionCacheKey, payload: dict[str, object]) -> None:
    if not _filter_option_cache_enabled():
        return
    ttl_seconds = _filter_option_cache_ttl_seconds()
    if ttl_seconds <= 0:
        return
    with _FILTER_OPTION_CACHE_LOCK:
        _FILTER_OPTION_CACHE[cache_key] = FilterOptionCacheEntry(
            payload=deepcopy(payload),
            expires_at=monotonic() + ttl_seconds,
        )


def _build_filter_options_uncached(
    *,
    mart_db: str,
    dimension_db: str,
    view: str,
    source: str,
    brand: str,
    market_id: str | None,
    measure: str,
    atc4_codes: Sequence[str],
    selections: Mapping[str, Sequence[str]],
) -> dict[str, object]:
    dimensions = _load_dimension_options(
        mart_db=mart_db,
        dimension_db=dimension_db,
        view=view,
        source=source,
        market_id=market_id,
        measure=measure,
        atc4_codes=atc4_codes,
        selections=selections,
    )
    atc_rows = _load_atc_rows(
        mart_db=mart_db,
        view=view,
        source=source,
        market_id=market_id,
        atc4_codes=atc4_codes,
    )
    channel_axis = _load_channel_axis_options(
        mart_db=mart_db,
        view=view,
        source=source,
        market_id=market_id,
        measure=measure,
        brand=brand,
        atc4_codes=atc4_codes,
    )
    return build_filter_option_payload(
        view=view,
        source=source,
        market_id=market_id,
        dimensions=dimensions,
        atc_rows=atc_rows,
        channel_axis=channel_axis,
    )


def build_brand_option_check(
    *,
    mart_db: str,
    brand: str,
    view: str,
    source: str,
    market_id: str | None = None,
    general_dimension_db: str | None = None,
    strategic_dimension_db: str | None = None,
) -> dict[str, object]:
    """Return all option values plus the values already carried by one brand.

    The portal uses this as a short-term test2 convenience endpoint: it can
    draw the same option list as ``filter-options`` and pre-check all
    product-level sidecar dimensions that the selected brand actually owns.
    We deliberately read from the view-specific sidecar so strategic recode
    values never leak back to the general ATC sidecar, and vice versa.
    """

    return build_filter_options(
        mart_db=mart_db,
        general_dimension_db=general_dimension_db,
        strategic_dimension_db=strategic_dimension_db,
        brand=brand,
        view=view,
        source=source,
        market_id=market_id,
    )


def build_filter_option_payload(
    *,
    view: str,
    source: str,
    market_id: str | None,
    dimensions: Sequence[DimensionOptionRow],
    atc_rows: Sequence[Mapping[str, object]],
    channel_axis: Mapping[str, object] | None = None,
) -> dict[str, object]:
    grouped: dict[str, list[DimensionOptionRow]] = defaultdict(list)
    for row in dimensions:
        grouped[row.dimension_type].append(row)
    ordered_dimensions: list[dict[str, object]] = []
    for dimension_type in sorted(grouped, key=_dimension_sort_key):
        rows = sorted(grouped.get(dimension_type, ()), key=lambda item: item.dimension_value)
        ordered_dimensions.append(
            {
                "dimension_type": dimension_type,
                "label": DIMENSION_LABELS.get(dimension_type, dimension_type),
                "values": [
                    {"key": item.dimension_value_norm, "value": item.dimension_value, "row_count": item.row_count}
                    for item in rows
                ],
            }
        )
    payload: dict[str, object] = {
        "view": view,
        "source": source,
        "market_id": market_id,
        "dimensions": ordered_dimensions,
        "atc": build_atc_hierarchy(atc_rows),
    }
    if channel_axis:
        payload["channel_axis"] = dict(channel_axis)
    return payload


def build_atc_hierarchy(rows: Iterable[Mapping[str, object]]) -> dict[str, object]:
    buckets: dict[str, dict[str, str]] = {"atc1": {}, "atc2": {}, "atc3": {}, "atc4": {}}
    for row in rows:
        code = str(row.get("atc4_code") or "").strip().upper()
        parsed = parse_atc_code(code)
        if parsed is None:
            continue
        for level, value in parsed.items():
            if value:
                buckets[level].setdefault(value, value)
    return {
        **{
            level: [
                {
                    "key": value,
                    "value": value,
                    "label": value,
                    "level": level,
                    "parent": _parent_for_atc_level(level, value),
                }
                for value in sorted(values)
            ]
            for level, values in buckets.items()
        },
        "selectable_levels": list(SELECTABLE_ATC_LEVELS),
    }


def parse_atc_code(code: str) -> dict[str, str] | None:
    """Return code-only ATC hierarchy while preserving the original ATC4 code.

    UBIST omits zero padding in one-digit numeric ATC segments (``A1A2``),
    while IQVIA already uses canonical two-digit numeric segments (``A01A2``).
    The UI groups upper levels on canonical tokens, but ATC4 remains the raw
    code because downstream filters and marts use that exact key.
    """

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


def normalize_view(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"general", "strategic"}:
        raise DynamicMarketInputError(f"unsupported filter option view: {value}")
    return normalized


def _load_dimension_options(
    *,
    mart_db: str,
    dimension_db: str,
    view: str,
    source: str,
    market_id: str | None,
    measure: str,
    atc4_codes: Sequence[str],
    selections: Mapping[str, Sequence[str]],
) -> tuple[DimensionOptionRow, ...]:
    table = GENERAL_DIMENSION_TABLE if view == "general" else STRATEGIC_DIMENSION_TABLE
    where = ["base.source = %s", "base.measure = %s"]
    params: list[object] = [source, measure]
    if view == "general":
        if atc4_codes:
            where.append(f"base.atc4_code IN ({', '.join(['%s'] * len(atc4_codes))})")
            params.extend(atc4_codes)
        _append_selection_exists_filters(
            where=where,
            params=params,
            dimension_db=dimension_db,
            table=table,
            selections=selections,
        )
        rows = db.fetch_all(
            f"""
            SELECT base.dimension_type AS dimension_type,
                   MIN(base.dimension_value) AS dimension_value,
                   MIN(base.dimension_value_norm) AS dimension_value_norm,
                   COUNT(*) AS row_count
            FROM {quote_identifier(dimension_db)}.{table} AS base
            WHERE {" AND ".join(where)}
            GROUP BY base.dimension_type, base.dimension_value_hash
            ORDER BY base.dimension_type, base.dimension_value_hash
            """,
            params,
        )
        return _dimension_option_rows(rows)
    elif market_id:
        market_kind, normalized_market_id = _strategic_market_filter(market_id)
        where.extend(["base.market_kind = %s", "base.market_id = %s"])
        params.extend([market_kind, normalized_market_id])
    rows = db.fetch_all(
        f"""
        SELECT base.dimension_type AS dimension_type,
               base.dimension_value AS dimension_value,
               base.dimension_value_norm AS dimension_value_norm,
               COUNT(*) AS row_count
        FROM {quote_identifier(dimension_db)}.{table} AS base
        WHERE {" AND ".join(where)}
        GROUP BY base.dimension_type, base.dimension_value, base.dimension_value_norm
        ORDER BY base.dimension_type, base.dimension_value
        """,
        params,
    )
    sidecar_rows = _dimension_option_rows(rows)
    by_dimension_rows = _load_strategic_by_dimension_options(
        mart_db=mart_db,
        source=source,
        market_id=market_id,
        measure=measure,
    )
    return _merge_dimension_rows(sidecar_rows, by_dimension_rows)


def _dimension_option_rows(rows: Sequence[Mapping[str, object]]) -> tuple[DimensionOptionRow, ...]:
    return tuple(
        DimensionOptionRow(
            dimension_type=str(row["dimension_type"]),
            dimension_value=str(row["dimension_value"]),
            dimension_value_norm=str(row["dimension_value_norm"]),
            row_count=int(row["row_count"]),
        )
        for row in rows
    )


def _merge_dimension_rows(*groups: Sequence[DimensionOptionRow]) -> tuple[DimensionOptionRow, ...]:
    merged: dict[tuple[str, str], DimensionOptionRow] = {}
    for rows in groups:
        for row in rows:
            key = (row.dimension_type, row.dimension_value_norm)
            current = merged.get(key)
            if current is None:
                merged[key] = row
                continue
            merged[key] = DimensionOptionRow(
                dimension_type=current.dimension_type,
                dimension_value=current.dimension_value,
                dimension_value_norm=current.dimension_value_norm,
                row_count=current.row_count + row.row_count,
            )
    return tuple(merged.values())


def _load_channel_axis_options(
    *,
    mart_db: str,
    view: str,
    source: str,
    market_id: str | None,
    measure: str,
    brand: str,
    atc4_codes: Sequence[str],
) -> dict[str, object]:
    """Build the UBIST channel-axis registry from scoped raw matrices."""

    if source != "ubist":
        return {}
    if view == "strategic":
        return _load_strategic_channel_axis_options(
            mart_db=mart_db,
            market_id=market_id,
            measure=measure,
            brand=brand,
        )
    if view != "general" or not atc4_codes:
        return {}
    rows = db.fetch_all(
        f"""
        SELECT brand_key, brand_name, channel_specialty_matrix
        FROM {quote_identifier(mart_db)}.mart_general_brand_metric
        WHERE source = %s
          AND measure = %s
          AND atc4_code IN ({', '.join(['%s'] * len(atc4_codes))})
        ORDER BY brand_name, brand_key
        """,
        [source, measure, *atc4_codes],
    )
    facility_counts: dict[str, set[str]] = defaultdict(set)
    specialty_counts: dict[str, set[str]] = defaultdict(set)
    pair_counts: dict[tuple[str, str], set[str]] = defaultdict(set)
    flagged_facilities: set[str] = set()
    flagged_specialties: set[str] = set()
    flagged_pairs: set[tuple[str, str]] = set()
    for row in rows:
        brand_key = str(row.get("brand_key") or "")
        brand_name = str(row.get("brand_name") or "")
        brand_marker = brand_key or brand_name
        is_brand_match = _is_brand_match(brand=brand, brand_key=brand_key, brand_name=brand_name)
        matrix = parse_channel_specialty_matrix(row.get("channel_specialty_matrix"))
        for facility, specialties in matrix.items():
            facility_counts[facility].add(brand_marker)
            if is_brand_match:
                flagged_facilities.add(facility)
            for specialty in specialties:
                specialty_counts[specialty].add(brand_marker)
                pair = (facility, specialty)
                pair_counts[pair].add(brand_marker)
                if is_brand_match:
                    flagged_specialties.add(specialty)
                    flagged_pairs.add(pair)
    if not facility_counts and not specialty_counts and not pair_counts:
        return {}
    return {
        "ubist": {
            "facility": [
                _channel_axis_option(value, row_count=len(facility_counts[value]), flagged=value in flagged_facilities)
                for value in sorted(facility_counts)
            ],
            "specialty": [
                _channel_axis_option(value, row_count=len(specialty_counts[value]), flagged=value in flagged_specialties)
                for value in sorted(specialty_counts)
            ],
            "pairs": [
                _channel_axis_pair_option(pair, row_count=len(pair_counts[pair]), flagged=pair in flagged_pairs)
                for pair in sorted(pair_counts)
            ],
        }
    }


def _load_strategic_channel_axis_options(
    *,
    mart_db: str,
    market_id: str | None,
    measure: str,
    brand: str,
) -> dict[str, object]:
    if not market_id:
        return {}
    brand_table, id_column = _strategic_atc_table(market_id)
    _, normalized_market_id = _strategic_market_filter(market_id)
    rows = db.fetch_all(
        f"""
        SELECT brand_key, brand_name, ubist_channel_by_code
        FROM {quote_identifier(mart_db)}.{brand_table}
        WHERE {id_column} = %s
          AND source = %s
          AND measure = %s
        ORDER BY brand_name, brand_key
        """,
        [normalized_market_id, "ubist", measure],
    )
    facility_counts: dict[str, set[str]] = defaultdict(set)
    specialty_counts: dict[str, set[str]] = defaultdict(set)
    pair_counts: dict[tuple[str, str], set[str]] = defaultdict(set)
    flagged_facilities: set[str] = set()
    flagged_specialties: set[str] = set()
    flagged_pairs: set[tuple[str, str]] = set()
    for row in rows:
        brand_key = str(row.get("brand_key") or "")
        brand_name = str(row.get("brand_name") or "")
        brand_marker = brand_key or brand_name
        is_brand_match = _is_brand_match(brand=brand, brand_key=brand_key, brand_name=brand_name)
        for code in _decode_json_object(row.get("ubist_channel_by_code")):
            try:
                parsed = parse_channel_code(str(code))
            except ValueError:
                continue
            if parsed is None:
                continue
            for facility in parsed.facility_raw_values:
                facility_counts[facility].add(brand_marker)
                if is_brand_match:
                    flagged_facilities.add(facility)
            for specialty in parsed.specialty_raw_values:
                specialty_counts[specialty].add(brand_marker)
                if is_brand_match:
                    flagged_specialties.add(specialty)
                for facility in parsed.facility_raw_values:
                    pair = (facility, specialty)
                    pair_counts[pair].add(brand_marker)
                    if is_brand_match:
                        flagged_pairs.add(pair)
    if not facility_counts and not specialty_counts and not pair_counts:
        return {}
    return {
        "ubist": {
            "facility": [
                _channel_axis_option(value, row_count=len(facility_counts[value]), flagged=value in flagged_facilities)
                for value in sorted(facility_counts)
            ],
            "specialty": [
                _channel_axis_option(value, row_count=len(specialty_counts[value]), flagged=value in flagged_specialties)
                for value in sorted(specialty_counts)
            ],
            "pairs": [
                _channel_axis_pair_option(pair, row_count=len(pair_counts[pair]), flagged=pair in flagged_pairs)
                for pair in sorted(pair_counts)
            ],
        }
    }


def _channel_axis_option(value: str, *, row_count: int, flagged: bool) -> dict[str, object]:
    return {
        "key": value,
        "value": value,
        "row_count": row_count,
        "default": False,
        "selected": False,
        "flag": flagged,
    }


def _channel_axis_pair_option(pair: tuple[str, str], *, row_count: int, flagged: bool) -> dict[str, object]:
    facility, specialty = pair
    return {
        "key": f"{facility}|{specialty}",
        "value": {"facility": facility, "specialty": specialty},
        "row_count": row_count,
        "default": False,
        "selected": False,
        "flag": flagged,
    }


def _is_brand_match(*, brand: str, brand_key: str, brand_name: str) -> bool:
    if not brand:
        return False
    requested = _compact_text(brand)
    return requested in {_compact_text(brand_key), _compact_text(brand_name)}


def _compact_text(value: str) -> str:
    return value.replace(" ", "").lower()


def _load_strategic_by_dimension_options(
    *,
    mart_db: str,
    source: str,
    market_id: str | None,
    measure: str,
) -> tuple[DimensionOptionRow, ...]:
    if not market_id:
        return ()
    dimension_keys = _load_strategic_analysis_dimension_keys(
        mart_db=mart_db,
        source=source,
        market_id=market_id,
        measure=measure,
    )
    if not dimension_keys:
        return ()
    brand_table, id_column = _strategic_atc_table(market_id)
    _, normalized_market_id = _strategic_market_filter(market_id)
    rows = db.fetch_all(
        f"""
        SELECT by_dimension
        FROM {quote_identifier(mart_db)}.{brand_table}
        WHERE {id_column} = %s
          AND source = %s
          AND measure = %s
        """,
        [normalized_market_id, source, measure],
    )
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        dimensions = _decode_json_object(row.get("by_dimension"))
        for dimension_type in dimension_keys:
            for display_value in _dimension_values(dimensions.get(dimension_type)):
                option_key = _dimension_option_key(display_value)
                if not option_key:
                    continue
            entry = grouped.setdefault(
                (dimension_type, option_key),
                {
                    "dimension_type": dimension_type,
                    "dimension_value": display_value,
                    "dimension_value_norm": option_key,
                    "row_count": 0,
                },
            )
            entry["row_count"] = int(entry["row_count"]) + 1
    return _dimension_option_rows(tuple(grouped.values()))


def _load_strategic_brand_by_dimension_matches(
    *,
    mart_db: str,
    brand: str,
    source: str,
    market_id: str | None,
    measure: str,
) -> dict[str, list[str]]:
    if not market_id:
        return {}
    dimension_keys = _load_strategic_analysis_dimension_keys(
        mart_db=mart_db,
        source=source,
        market_id=market_id,
        measure=measure,
    )
    if not dimension_keys:
        return {}
    brand_table, id_column = _strategic_atc_table(market_id)
    _, normalized_market_id = _strategic_market_filter(market_id)
    rows = db.fetch_all(
        f"""
        SELECT by_dimension
        FROM {quote_identifier(mart_db)}.{brand_table}
        WHERE {id_column} = %s
          AND source = %s
          AND measure = %s
          AND (
              brand_name = %s
              OR brand_key = %s
              OR LOWER(REPLACE(brand_name, ' ', '')) = LOWER(REPLACE(%s, ' ', ''))
              OR LOWER(REPLACE(brand_key, ' ', '')) = LOWER(REPLACE(%s, ' ', ''))
          )
        """,
        [normalized_market_id, source, measure, brand, brand, brand, brand],
    )
    grouped: dict[str, list[str]] = {}
    for row in rows:
        dimensions = _decode_json_object(row.get("by_dimension"))
        for dimension_type in dimension_keys:
            for display_value in _dimension_values(dimensions.get(dimension_type)):
                option_key = _dimension_option_key(display_value)
                if option_key:
                    grouped.setdefault(dimension_type, []).append(option_key)
    return {dimension_type: list(dict.fromkeys(values)) for dimension_type, values in grouped.items()}


def _load_strategic_analysis_dimension_keys(
    *,
    mart_db: str,
    source: str,
    market_id: str,
    measure: str,
) -> tuple[str, ...]:
    market_table, id_column = _strategic_market_table(market_id)
    _, normalized_market_id = _strategic_market_filter(market_id)
    rows = db.fetch_all(
        f"""
        SELECT analysis_levels
        FROM {quote_identifier(mart_db)}.{market_table}
        WHERE {id_column} = %s
          AND source = %s
          AND measure = %s
        LIMIT 1
        """,
        [normalized_market_id, source, measure],
    )
    row = rows[0] if rows else None
    analysis_levels = _decode_json_object(row.get("analysis_levels") if row else None)
    return tuple(key for key in analysis_levels if key not in {"atc3", "atc4"})


def _decode_json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _dimension_values(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if value in (None, ""):
        return ()
    return (str(value).strip(),)


def _dimension_option_key(value: str) -> str:
    return value.strip().lower()


def _load_brand_dimension_matches(
    *,
    dimension_db: str,
    brand: str,
    view: str,
    source: str,
    market_id: str | None,
    measure: str,
) -> dict[str, list[str]]:
    table = GENERAL_DIMENSION_TABLE if view == "general" else STRATEGIC_DIMENSION_TABLE
    where = [
        "source = %s",
        "measure = %s",
        "(brand_name = %s OR brand_key = %s OR LOWER(REPLACE(brand_name, ' ', '')) = LOWER(REPLACE(%s, ' ', '')) OR LOWER(REPLACE(brand_key, ' ', '')) = LOWER(REPLACE(%s, ' ', '')))",
    ]
    params: list[object] = [source, measure, brand, brand, brand, brand]
    if view == "general":
        if atc_prefix := _general_atc_prefix(market_id):
            where.append("atc4_code LIKE %s")
            params.append(atc_prefix)
    elif market_id:
        market_kind, normalized_market_id = _strategic_market_filter(market_id)
        where.extend(["market_kind = %s", "market_id = %s"])
        params.extend([market_kind, normalized_market_id])

    rows = db.fetch_all(
        f"""
        SELECT dimension_type, dimension_value_norm
        FROM {quote_identifier(dimension_db)}.{table}
        WHERE {" AND ".join(where)}
        GROUP BY dimension_type, dimension_value_norm
        ORDER BY dimension_type, dimension_value_norm
        """,
        params,
    )
    grouped: dict[str, list[str]] = {}
    for row in rows:
        dimension_type = str(row["dimension_type"])
        value = str(row["dimension_value_norm"])
        if value:
            grouped.setdefault(dimension_type, []).append(value)
    return {dimension_type: values for dimension_type, values in grouped.items() if values}


def _load_atc_rows(
    *,
    mart_db: str,
    view: str,
    source: str,
    market_id: str | None,
    atc4_codes: Sequence[str],
) -> tuple[dict[str, object], ...]:
    if view == "strategic":
        table, id_column = _strategic_atc_table(market_id)
        where = ["source = %s"]
        params: list[str] = [source]
        if market_id:
            _, normalized_market_id = _strategic_market_filter(market_id)
            where.append(f"{id_column} = %s")
            params.append(normalized_market_id)
        sql = f"""
            SELECT DISTINCT JSON_UNQUOTE(JSON_EXTRACT(by_dimension, '$.atc4_code')) AS atc4_code
            FROM {quote_identifier(mart_db)}.{table}
            WHERE {" AND ".join(where)}
        """
        return tuple(db.fetch_all(sql, params))
    where = ["source = %s"]
    params: list[str] = [source]
    if atc4_codes:
        where.append(f"atc4_code IN ({', '.join(['%s'] * len(atc4_codes))})")
        params.extend(atc4_codes)
    rows = db.fetch_all(
        f"""
        SELECT atc4_code
        FROM {quote_identifier(mart_db)}.mart_general_brand_metric FORCE INDEX (idx_general_atc_universe)
        WHERE {" AND ".join(where)}
        GROUP BY atc4_code
        ORDER BY atc4_code
        """,
        params,
    )
    return tuple(rows)


def _append_selection_exists_filters(
    *,
    where: list[str],
    params: list[object],
    dimension_db: str,
    table: str,
    selections: Mapping[str, Sequence[str]],
) -> None:
    for dimension_type, values in sorted(selections.items()):
        if dimension_type in {"atc1", "atc2", "atc3", "atc4", "atc4_code"}:
            continue
        normalized_values = _clean_values(values)
        if not normalized_values:
            continue
        where.append(
            f"""
            EXISTS (
                SELECT 1
                FROM {quote_identifier(dimension_db)}.{table} AS selected
                WHERE selected.source = base.source
                  AND selected.measure = base.measure
                  AND COALESCE(selected.product_code, selected.brand_key) = COALESCE(base.product_code, base.brand_key)
                  AND selected.dimension_type = %s
                  AND selected.dimension_value_norm IN ({', '.join(['%s'] * len(normalized_values))})
            )
            """
        )
        params.append(dimension_type)
        params.extend(normalized_values)


def _parse_atc4_codes(market_id: str | None, atc4_codes: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            _canonical_general_atc4(value)
            for value in [*(_split_list_values(market_id) if market_id else []), *list(atc4_codes or [])]
            if _canonical_general_atc4(value)
        )
    )


def _canonical_general_atc4(value: str) -> str:
    return value.strip().upper()


def _parse_selection_map(selections: Mapping[str, Sequence[str]] | str | None) -> dict[str, tuple[str, ...]]:
    if selections is None:
        return {}
    if isinstance(selections, str):
        raw_text = selections.strip()
        if not raw_text:
            return {}
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise DynamicMarketInputError("invalid filter option selections JSON") from exc
        if not isinstance(parsed, dict):
            raise DynamicMarketInputError("filter option selections must be a JSON object")
        selections = {
            str(key): _coerce_selection_sequence(value)
            for key, value in parsed.items()
        }
    return {
        _canonical_selection_key(key): tuple(values)
        for key, raw_values in selections.items()
        if (values := _clean_values(raw_values))
    }


def _coerce_selection_sequence(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(_split_list_values(value))
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value)
    return ()


def _split_list_values(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _clean_values(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _canonical_selection_key(key: str) -> str:
    normalized = key.strip()
    if normalized == "atc4_code":
        return "atc4"
    return normalized


def _dimension_sort_key(dimension_type: str) -> tuple[int, str]:
    try:
        return (DIMENSION_ORDER_HINTS.index(dimension_type), dimension_type)
    except ValueError:
        return (len(DIMENSION_ORDER_HINTS), dimension_type)


def _apply_option_state(
    *,
    payload: dict[str, object],
    view: str,
    atc4_codes: Sequence[str],
    selections: Mapping[str, Sequence[str]],
    brand_matched: Mapping[str, Sequence[str]],
) -> None:
    default_selections: dict[str, list[str]] = {}
    applied_selections: dict[str, list[str]] = {key: list(values) for key, values in selections.items()}
    dimensions = payload.get("dimensions")
    if not isinstance(dimensions, list):
        return
    for dimension in dimensions:
        if not isinstance(dimension, dict):
            continue
        dimension_type = str(dimension.get("dimension_type") or "")
        values = dimension.get("values")
        if not dimension_type or not isinstance(values, list):
            continue
        if view == "strategic":
            default_selections[dimension_type] = [
                str(value.get("key") or "")
                for value in values
                if isinstance(value, dict) and value.get("key")
            ]
            applied_selections.setdefault(dimension_type, list(default_selections[dimension_type]))
        flagged_values = set(brand_matched.get(dimension_type, ()))
        default_values = set(default_selections.get(dimension_type, ()))
        selected_values = set(applied_selections.get(dimension_type, ()))
        for value in values:
            if not isinstance(value, dict):
                continue
            option_key = str(value.get("key") or "")
            option_label = str(value.get("value") or "")
            value["default"] = option_key in default_values or option_label in default_values
            value["selected"] = option_key in selected_values or option_label in selected_values
            value["flag"] = option_key in flagged_values or option_label in flagged_values
    atc_defaults = _mark_atc_state(
        payload=payload,
        view=view,
        atc4_codes=atc4_codes,
        brand_atc4=set(brand_matched.get("atc4", ())),
    )
    default_selections.update(atc_defaults)
    selected_by_level = _atc_values_by_level(atc4_codes)
    for level, values in selected_by_level.items():
        if values:
            applied_selections.setdefault(level, sorted(values))
    payload["default_selections"] = default_selections
    payload["applied_selections"] = applied_selections


def _mark_atc_state(
    *,
    payload: dict[str, object],
    view: str,
    atc4_codes: Sequence[str],
    brand_atc4: set[str],
) -> dict[str, list[str]]:
    atc = payload.get("atc")
    if not isinstance(atc, dict):
        return {}
    selected_by_level = _atc_values_by_level(atc4_codes)
    brand_by_level = _atc_values_by_level(tuple(brand_atc4))
    defaults: dict[str, list[str]] = {}
    for level in ("atc1", "atc2", "atc3", "atc4"):
        options = atc.get(level)
        if not isinstance(options, list):
            continue
        if view == "strategic":
            default_values = {str(option.get("key")) for option in options if isinstance(option, dict)}
        else:
            default_values = brand_by_level[level]
        defaults[level] = sorted(value for value in default_values if value)
        for option in options:
            if not isinstance(option, dict):
                continue
            key = str(option.get("key") or "")
            option["default"] = key in default_values
            option["selected"] = key in selected_by_level[level] or key in default_values
            option["flag"] = key in brand_by_level[level]
    return defaults


def _atc_values_by_level(atc4_codes: Sequence[str]) -> dict[str, set[str]]:
    values: dict[str, set[str]] = {"atc1": set(), "atc2": set(), "atc3": set(), "atc4": set()}
    for code in atc4_codes:
        parsed = parse_atc_code(code)
        if parsed is None:
            continue
        for level, value in parsed.items():
            values[level].add(value)
    return values


def _general_market_id_for_brand(*, mart_db: str, source: str, brand: str) -> str | None:
    rows = db.fetch_all(
        f"""
        SELECT DISTINCT atc4_code
        FROM {quote_identifier(mart_db)}.mart_general_brand_metric
        WHERE source = %s
          AND (
              brand_name = %s
              OR brand_key = %s
              OR LOWER(REPLACE(brand_name, ' ', '')) = LOWER(REPLACE(%s, ' ', ''))
              OR LOWER(REPLACE(brand_key, ' ', '')) = LOWER(REPLACE(%s, ' ', ''))
          )
        ORDER BY atc4_code
        """,
        [source, brand, brand, brand, brand],
    )
    for row in rows:
        if atc4_code := str(row.get("atc4_code") or "").strip().upper():
            return atc4_code
    return None


def _general_atc_prefix(market_id: str | None) -> str | None:
    if not market_id:
        return None
    normalized = market_id.strip().upper()
    if not normalized:
        return None
    return f"{normalized}%"


def _strategic_market_filter(market_id: str) -> tuple[str, str]:
    normalized = market_id.strip()
    if normalized.startswith("ml_"):
        return "ml", normalized
    if normalized.startswith("cd_"):
        return "cd", normalized
    if normalized.startswith("strategy_"):
        return "ml", f"ml_{normalized.removeprefix('strategy_')}"
    raise DynamicMarketInputError(f"unsupported strategic market id: {market_id}")


def _strategic_atc_table(market_id: str | None) -> tuple[str, str]:
    if market_id and market_id.strip().startswith("cd_"):
        return "mart_strategic_cd_brand_metric", "cd_market_id"
    return "mart_strategic_ml_brand_metric", "ml_id"


def _strategic_market_table(market_id: str | None) -> tuple[str, str]:
    if market_id and market_id.strip().startswith("cd_"):
        return "mart_strategic_cd_market_metric", "cd_market_id"
    return "mart_strategic_ml_market_metric", "ml_id"
