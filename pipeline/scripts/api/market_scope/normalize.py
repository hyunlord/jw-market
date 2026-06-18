"""Market-scope option normalization and canonical hashing."""

from __future__ import annotations

import hashlib
import json
from typing import Final

from pipeline.scripts.api.market_scope.types import (
    ALGORITHM_VERSION,
    CONTRACT_VERSION,
    DEDUP_KEY_VERSION,
    DedupDiagnostics,
    MarketScopeMember,
    MarketScopeOption,
    MarketScopeRequest,
    MarketScopeValidationError,
    OptionType,
    ResolvedScope,
    ViewFamily,
)


VALID_MEASURES_BY_SOURCE: Final[dict[str, frozenset[str]]] = {
    "UBIST": frozenset({"sales", "volume"}),
    "IQVIA": frozenset({"sales", "unit", "dosage_unit", "counting_unit"}),
}


def normalize_market_scope(
    request: MarketScopeRequest,
    options: tuple[MarketScopeOption, ...],
) -> ResolvedScope:
    """Expand options, reject invalid combinations, and compute scope hash."""

    source = normalize_source(request.source)
    measure = normalize_measure(source, request.measure)
    selected_ids = _selected_option_ids(request.option_ids)
    options_by_id = {option.option_id: option for option in options}
    selected = tuple(_option(options_by_id, option_id) for option_id in selected_ids)
    _validate_selected(request.view_family, source, selected)

    excluded: list[MarketScopeMember] = []
    source_markets: set[str] = set()
    atc4_set: set[str] = set()
    for option in selected:
        if option.option_type is OptionType.GROUP_UNION:
            for member in option.members:
                if member.member_status == "absent_in_csd":
                    excluded.append(member)
                    continue
                if member.source_market:
                    source_markets.add(member.source_market)
                atc4_set.update(member.atc4_set)
            continue
        source_markets.update(option.source_markets)
        atc4_set.update(option.atc4_set)

    resolved_source_markets = tuple(sorted(source_markets))
    resolved_atc4_set = tuple(sorted(atc4_set))
    catalog_version = selected[0].catalog_version
    scope_hash = _scope_hash(
        request=request,
        normalized_source=source,
        normalized_measure=measure,
        selected_option_ids=selected_ids,
        resolved_source_markets=resolved_source_markets,
        resolved_atc4_set=resolved_atc4_set,
        excluded_members=tuple(excluded),
        catalog_version=catalog_version,
    )
    return ResolvedScope(
        scope_hash=scope_hash,
        view_family=request.view_family,
        selected_option_ids=selected_ids,
        resolved_source_markets=resolved_source_markets,
        resolved_atc4_set=resolved_atc4_set,
        excluded_members=tuple(excluded),
        dedup=DedupDiagnostics.empty(),
        catalog_version=catalog_version,
    )


def normalize_source(value: str) -> str:
    """Normalize source aliases to contract source labels."""

    text = value.strip().upper()
    aliases = {"IQVIA_NSA": "IQVIA", "NSA": "IQVIA"}
    source = aliases.get(text, text)
    if source not in VALID_MEASURES_BY_SOURCE:
        raise MarketScopeValidationError(f"unsupported source: {value}")
    return source


def normalize_measure(source: str, value: str) -> str:
    """Validate a measure against a normalized source."""

    measure = value.strip().lower()
    if measure not in VALID_MEASURES_BY_SOURCE[source]:
        raise MarketScopeValidationError(f"unsupported measure for {source}: {value}")
    return measure


def _selected_option_ids(option_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Trim, dedupe, and sort selected option ids."""

    normalized = tuple(sorted({option_id.strip() for option_id in option_ids if option_id.strip()}))
    if not normalized:
        raise MarketScopeValidationError("option_ids must contain at least one value")
    return normalized


def _option(options_by_id: dict[str, MarketScopeOption], option_id: str) -> MarketScopeOption:
    """Look up one selected option or raise a contract error."""

    try:
        return options_by_id[option_id]
    except KeyError as exc:
        raise MarketScopeValidationError(f"unknown option_id: {option_id}") from exc


def _validate_selected(
    request_family: ViewFamily,
    source: str,
    selected: tuple[MarketScopeOption, ...],
) -> None:
    """Apply mixed-family and source-availability validation."""

    families = {option.view_family for option in selected}
    if len(families) > 1 or families != {request_family}:
        found = ", ".join(sorted(family.value for family in families))
        raise MarketScopeValidationError(f"mixed view_family selection is not allowed: {found}")
    for option in selected:
        if source not in option.available_sources:
            raise MarketScopeValidationError(f"{source} is not available for option_id={option.option_id}")


def _scope_hash(
    *,
    request: MarketScopeRequest,
    normalized_source: str,
    normalized_measure: str,
    selected_option_ids: tuple[str, ...],
    resolved_source_markets: tuple[str, ...],
    resolved_atc4_set: tuple[str, ...],
    excluded_members: tuple[MarketScopeMember, ...],
    catalog_version: str,
) -> str:
    """Compute sha256 from a deterministic canonical scope document."""

    canonical = {
        "algorithm_version": ALGORITHM_VERSION,
        "brand": request.brand.strip(),
        "catalog_version": catalog_version,
        "contract_version": CONTRACT_VERSION,
        "dedup_key_version": DEDUP_KEY_VERSION,
        "excluded_members": [member.to_dict() for member in excluded_members],
        "measure": normalized_measure,
        "resolved_atc4_set": list(resolved_atc4_set),
        "resolved_source_markets": list(resolved_source_markets),
        "selected_option_ids": list(selected_option_ids),
        "source": normalized_source,
        "view_family": request.view_family.value,
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

