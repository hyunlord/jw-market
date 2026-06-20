from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
import hashlib

from .models import BrandDescription, JsonValue, KeywordRow
from .privacy import estimate_tokens
from .market_groups import scope_metadata_from_group_map


JW_ANCHORS = {
    "LIVALO",
    "LIVALOZET",
    "LIVALO V",
    "THRUPAS",
    "FERINJECT",
    "VENOFERRUM",
    "WINUF",
    "WINUF PERI",
    "WINUF A PLUS",
    "JAQBO",
}


def deterministic_sample(rows: Sequence[KeywordRow], *, limit: int, seed: str) -> list[KeywordRow]:
    """Select a deterministic subset by source hash so dry-runs are reproducible."""
    ranked = sorted(rows, key=lambda row: hashlib.sha256(f"{seed}:{row.stage_row_sha256}:{row.row_id}".encode("utf-8")).hexdigest())
    return sorted(ranked[:limit], key=lambda row: row.row_id)


def choose_sample_brands(rows: Sequence[KeywordRow], *, known_anchors: set[str], max_brands: int) -> tuple[str, ...]:
    """Pick up to seven measured brands, preferring top volume, JW anchor, and competitors."""
    counts = Counter(row.brand for row in rows)
    if not counts:
        return ()
    limit = max(1, min(7, max_brands, len(counts)))
    selected: list[str] = [counts.most_common(1)[0][0]]
    anchor_candidates = [brand for brand in counts if brand in known_anchors and brand not in selected]
    if anchor_candidates and len(selected) < limit:
        selected.append(max(anchor_candidates, key=lambda brand: counts[brand]))
    for brand, _count in counts.most_common():
        if len(selected) >= limit:
            break
        if brand not in selected:
            selected.append(brand)
    return tuple(selected)


def build_market_samples(
    rows: Sequence[KeywordRow],
    markets: Sequence[str],
    descriptions: dict[str, BrandDescription],
    *,
    axis_per_brand: int,
    axis_rows_cap: int,
    brand_rows: int,
    brands_per_market: int,
    full_rows: bool = False,
    group_map: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """Create deterministic market-axis and brand-share samples for final MI/CSD markets."""
    rows_by_market: defaultdict[str, list[KeywordRow]] = defaultdict(list)
    for row in rows:
        rows_by_market[row.atc4].append(row)
    known_anchors = _known_anchors(descriptions)
    axis_samples: dict[str, list[KeywordRow]] = {}
    brand_samples: dict[str, list[KeywordRow]] = {}
    selected: dict[str, tuple[str, ...]] = {}
    selected_pairs: dict[str, tuple[tuple[str, str], ...]] = {}
    scope_metadata = scope_metadata_from_group_map(group_map or {})
    scope_keys = tuple(scope_metadata) if scope_metadata else tuple(markets)
    for scope_key in scope_keys:
        atc4_values = _scope_atc4_values(scope_key, scope_metadata)
        market_rows = _rows_for_scope(rows_by_market, atc4_values)
        if not market_rows:
            continue
        brands = choose_sample_brands(market_rows, known_anchors=known_anchors, max_brands=brands_per_market)
        selected[scope_key] = brands
        selected_pairs[scope_key] = tuple((_brand_atc4(market_rows, brand), brand) for brand in brands)
        if full_rows:
            axis_samples[scope_key] = sorted(market_rows, key=lambda row: (row.atc4, row.brand, row.row_id))
        else:
            axis_rows: list[KeywordRow] = []
            for brand in brands:
                axis_rows.extend(deterministic_sample([row for row in market_rows if row.brand == brand], limit=axis_per_brand, seed=f"axis:{scope_key}:{brand}"))
            capped_rows = deterministic_sample(axis_rows, limit=min(axis_rows_cap, len(axis_rows)), seed=f"axis-cap:{scope_key}") if axis_rows_cap else axis_rows
            axis_samples[scope_key] = sorted(capped_rows, key=lambda row: (row.atc4, row.brand, row.row_id))
        for brand in brands:
            source_rows = [row for row in market_rows if row.brand == brand]
            atc4 = _brand_atc4(source_rows, brand)
            sample_key = _brand_sample_key(scope_key, atc4, brand)
            brand_samples[sample_key] = sorted(source_rows, key=lambda row: row.row_id) if full_rows else deterministic_sample(source_rows, limit=brand_rows, seed=f"brand:{scope_key}:{atc4}:{brand}")
    return {
        "axis_samples": axis_samples,
        "brand_samples": brand_samples,
        "selected_brands": selected,
        "selected_brand_pairs": {key: [list(pair) for pair in pairs] for key, pairs in selected_pairs.items()},
        "scope_metadata": scope_metadata,
        "sample_summary": _sample_summary(axis_samples, brand_samples, selected, full_rows=full_rows),
    }


def large_markets_by_row_count(rows: Sequence[KeywordRow], *, limit: int) -> tuple[str, ...]:
    """Return the highest-row ATC4 markets for Pro/Lite recheck calls."""
    counts = Counter(row.atc4 for row in rows)
    return tuple(market for market, _count in counts.most_common(limit))


def large_scopes_by_row_count(axis_samples: dict[str, list[KeywordRow]], *, limit: int) -> tuple[str, ...]:
    """Return the highest-row final scopes for Pro/Lite recheck calls."""
    counts = sorted(((scope_key, len(rows)) for scope_key, rows in axis_samples.items()), key=lambda item: (-item[1], item[0]))
    return tuple(scope_key for scope_key, _count in counts[:limit])


def _known_anchors(descriptions: dict[str, BrandDescription]) -> set[str]:
    """Combine alias JW flags with the explicit known-anchor fallback list."""
    anchors = set(JW_ANCHORS)
    anchors.update(description.brand for description in descriptions.values() if description.is_jw)
    return anchors


def _scope_atc4_values(scope_key: str, scope_metadata: dict[str, dict[str, JsonValue]]) -> tuple[str, ...]:
    """Return source ATC4 values for one final scope."""
    row = scope_metadata.get(scope_key, {})
    values = row.get("atc4_values")
    return tuple(str(value) for value in values) if isinstance(values, list) else (scope_key,)


def _rows_for_scope(rows_by_market: dict[str, list[KeywordRow]], atc4_values: tuple[str, ...]) -> list[KeywordRow]:
    """Return all source rows that belong to a final market scope."""
    rows: list[KeywordRow] = []
    for atc4 in atc4_values:
        rows.extend(rows_by_market.get(atc4, []))
    return rows


def _brand_atc4(rows: Sequence[KeywordRow], brand: str) -> str:
    """Return the preserved source ATC4 for one sampled brand."""
    counts = Counter(row.atc4 for row in rows if row.brand == brand)
    return counts.most_common(1)[0][0] if counts else ""


def _brand_sample_key(scope_key: str, atc4: str, brand: str) -> str:
    """Build a brand-share key that preserves both final scope and source ATC4."""
    return f"{scope_key}:{atc4}:{brand}" if scope_key.startswith("group:") else f"{atc4}:{brand}"


def _sample_summary(axis_samples: dict[str, list[KeywordRow]], brand_samples: dict[str, list[KeywordRow]], selected: dict[str, tuple[str, ...]], *, full_rows: bool) -> dict[str, JsonValue]:
    """Build sanitized sample counts and token estimates for audit."""
    return {
        "mode": "full_rows" if full_rows else "deterministic_capped",
        "axis": {market: {"row_count": len(rows), "estimated_input_tokens": sum(estimate_tokens(row.keyword_text) for row in rows), "brands": list(selected.get(market, ())) } for market, rows in axis_samples.items()},
        "brand": {key: {"row_count": len(rows), "estimated_input_tokens": sum(estimate_tokens(row.keyword_text) for row in rows)} for key, rows in brand_samples.items()},
    }
