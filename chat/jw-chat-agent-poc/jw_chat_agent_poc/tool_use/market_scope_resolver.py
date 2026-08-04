from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from jw_chat_agent_poc.tool_use.market_scope_contract import (
    AmbiguousMarketError,
    GeneralCompositeUnavailableError,
    InvalidMarketLabelError,
    MarketScope,
    MarketScopeKind,
    NoStrategicMembershipError,
    ScopeResolution,
    UnsupportedSourceError,
)
from jw_chat_agent_poc.tool_use.market_scope_input import (
    ATC4_PATTERN,
    STRATEGIC_MARKET_PATTERN,
    SUPPORTED_SOURCES,
    normalize_source,
    normalize_view_arguments,
    optional_text,
    raise_unresolved_brand,
    required_text,
    scope_filters,
    text,
)
from jw_chat_agent_poc.tools.general_view_backend import AtcCandidate


class GeneralMembership(Protocol):
    def resolve(self, brand: str, source: str): ...


class ScopeResolver:
    """Resolve market identity before calculation without changing strategic math."""

    def __init__(
        self,
        *,
        strategic_memberships: Callable[[], Sequence[Mapping[str, str]]],
        general_membership: GeneralMembership,
        route_hint: str | None = None,
    ) -> None:
        self._strategic_memberships = strategic_memberships
        self._general_membership = general_membership
        self._route_hint = route_hint
        self._strategic_index: dict[str, tuple[str, ...]] | None = None

    def resolve(self, arguments: Mapping[str, object]) -> ScopeResolution:
        normalized, notes = normalize_view_arguments(arguments)
        brand = required_text(normalized, "brand")
        source = normalize_source(text(normalized, "source", ""))
        if source not in SUPPORTED_SOURCES:
            raise UnsupportedSourceError(source or "missing")

        explicit_scope = normalized.get("scope")
        if isinstance(explicit_scope, Mapping):
            return self._explicit_scope(normalized, notes, source, explicit_scope)

        view = optional_text(normalized, "view")
        market = optional_text(normalized, "market")
        strategic_markets = self._markets_for_brand(brand)
        if view == "strategic":
            if not strategic_markets:
                raise NoStrategicMembershipError(brand)
            return self._strategic_resolution(
                normalized, notes, source, strategic_markets, market
            )
        if view == "general":
            return self._general_resolution(
                normalized,
                notes,
                brand,
                source,
                market,
                fallback_reason=None,
            )
        if view is not None:
            raise InvalidMarketLabelError(view)

        if market is not None:
            if STRATEGIC_MARKET_PATTERN.fullmatch(market):
                return self._strategic_resolution(
                    normalized, notes, source, strategic_markets, market
                )
            if ATC4_PATTERN.fullmatch(market):
                return ScopeResolution(
                    MarketScope(MarketScopeKind.GENERAL_ATC4, atc4=(market.upper(),)),
                    source or self._general_source_for_brand(brand),
                    normalized,
                    notes,
                )
            raise InvalidMarketLabelError(market)

        if strategic_markets:
            return ScopeResolution(
                MarketScope(MarketScopeKind.STRATEGIC), source, normalized, notes
            )
        if self._route_hint == "existing":
            notes = (*notes, "route_hint:existing->general_membership_check")
        return self._general_resolution(
            normalized,
            notes,
            brand,
            source,
            None,
            fallback_reason="no_strategic_membership",
        )

    def _explicit_scope(
        self,
        arguments: dict[str, object],
        notes: tuple[str, ...],
        source: str,
        raw_scope: Mapping[str, object],
    ) -> ScopeResolution:
        kind_value = str(raw_scope.get("kind") or "").strip()
        try:
            kind = MarketScopeKind(kind_value)
        except ValueError as exc:
            raise InvalidMarketLabelError(kind_value or "missing scope kind") from exc
        raw_atc4 = raw_scope.get("atc4")
        atc4 = (
            tuple(
                dict.fromkeys(
                    str(code).strip().upper()
                    for code in raw_atc4
                    if str(code).strip()
                )
            )
            if isinstance(raw_atc4, Sequence)
            and not isinstance(raw_atc4, (str, bytes))
            else ()
        )
        market_id = str(raw_scope.get("market_id") or "").strip() or None
        filters = scope_filters(raw_scope.get("filters"))
        if filters:
            raise GeneralCompositeUnavailableError(
                "scope filters require composite execution"
            )
        scope = MarketScope(kind, market_id=market_id, atc4=atc4, filters=filters)
        if kind is MarketScopeKind.STRATEGIC:
            if market_id is None or STRATEGIC_MARKET_PATTERN.fullmatch(market_id) is None:
                raise InvalidMarketLabelError(market_id or "missing strategic market_id")
            memberships = self._markets_for_brand(required_text(arguments, "brand"))
            if market_id not in memberships:
                raise InvalidMarketLabelError(market_id)
            arguments = {**arguments, "market": market_id}
        elif not atc4 or any(ATC4_PATTERN.fullmatch(code) is None for code in atc4):
            raise InvalidMarketLabelError("general scope requires valid ATC4")
        if kind is MarketScopeKind.GENERAL_ATC4 and len(atc4) != 1:
            raise InvalidMarketLabelError("general_atc4 requires exactly one ATC4")
        return ScopeResolution(scope, source, arguments, notes)

    def _strategic_resolution(
        self,
        arguments: dict[str, object],
        notes: tuple[str, ...],
        source: str,
        memberships: tuple[str, ...],
        market: str | None,
    ) -> ScopeResolution:
        if not memberships:
            raise NoStrategicMembershipError(required_text(arguments, "brand"))
        if market is not None and market not in memberships:
            raise InvalidMarketLabelError(market)
        return ScopeResolution(
            MarketScope(MarketScopeKind.STRATEGIC, market_id=market),
            source,
            arguments,
            notes,
        )

    def _general_resolution(
        self,
        arguments: dict[str, object],
        notes: tuple[str, ...],
        brand: str,
        source: str,
        market: str | None,
        *,
        fallback_reason: str | None,
    ) -> ScopeResolution:
        if market is not None:
            if ATC4_PATTERN.fullmatch(market) is None:
                raise InvalidMarketLabelError(market)
            return ScopeResolution(
                MarketScope(MarketScopeKind.GENERAL_ATC4, atc4=(market.upper(),)),
                source or self._general_source_for_brand(brand),
                arguments,
                notes,
                fallback_reason,
            )
        selected_source, candidates = self._general_candidates(brand, source)
        if not candidates:
            raise_unresolved_brand(brand)
        if len(candidates) != 1:
            raise AmbiguousMarketError(
                f"{brand} maps to multiple ATC4 codes: "
                f"{','.join(candidate.code for candidate in candidates)}"
            )
        return ScopeResolution(
            MarketScope(MarketScopeKind.GENERAL_ATC4, atc4=(candidates[0].code.upper(),)),
            selected_source,
            arguments,
            notes,
            fallback_reason,
        )

    def _markets_for_brand(self, brand: str) -> tuple[str, ...]:
        if self._strategic_index is None:
            grouped: dict[str, set[str]] = {}
            for row in self._strategic_memberships():
                row_brand = str(row.get("brand") or "").strip()
                market = str(row.get("market_id") or "").strip()
                if row_brand and market:
                    grouped.setdefault(row_brand, set()).add(market)
            self._strategic_index = {
                row_brand: tuple(sorted(markets))
                for row_brand, markets in grouped.items()
            }
        return self._strategic_index.get(brand, ())

    def _general_candidates(
        self,
        brand: str,
        source: str,
    ) -> tuple[str, tuple[AtcCandidate, ...]]:
        sources = (source,) if source else ("ubist", "iqvia")
        for candidate_source in sources:
            resolution = self._general_membership.resolve(brand, candidate_source)
            if resolution is not None and resolution.candidates:
                return candidate_source, tuple(resolution.candidates)
        return source, ()

    def _general_source_for_brand(self, brand: str) -> str:
        source, candidates = self._general_candidates(brand, "")
        if not candidates:
            raise_unresolved_brand(brand)
        return source
