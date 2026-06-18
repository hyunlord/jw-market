"""Market-scope resolver orchestration for Stage 0-2."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol

from pipeline.scripts.api.market_scope.cache import ScopeHashCache
from pipeline.scripts.api.market_scope.catalog import MarketScopeCatalog
from pipeline.scripts.api.market_scope.fact_collector import StrategyFact, deduplicate_or_guard_disjoint
from pipeline.scripts.api.market_scope.normalize import normalize_market_scope, normalize_measure, normalize_source
from pipeline.scripts.api.market_scope.recompute import recompute_strategy_payload
from pipeline.scripts.api.market_scope.types import DedupDiagnostics, MarketScopeRequest, ResolvedScope, ViewFamily


CacheReader = Callable[[MarketScopeRequest, ResolvedScope], dict[str, Any]]
FactProvider = Callable[[MarketScopeRequest, ResolvedScope], Iterable[StrategyFact]]


class MarketScopeResolver(Protocol):
    """Common resolver interface for strategy and future general scopes."""

    def resolve(self, request: MarketScopeRequest) -> ResolvedScope:
        """Return the resolved scope with final guard diagnostics."""

    def cause(self, request: MarketScopeRequest) -> dict[str, Any]:
        """Return a cause-compatible payload plus ``resolved_scope``."""


class StrategyScopeResolver:
    """Resolve strategy market options and recompute multi/group scopes."""

    def __init__(
        self,
        *,
        catalog: MarketScopeCatalog,
        cache_reader: CacheReader,
        fact_provider: FactProvider,
        scope_cache: ScopeHashCache | None = None,
    ) -> None:
        """Wire catalog, cache fast-path, and fact-provider dependencies."""

        self._catalog = catalog
        self._cache_reader = cache_reader
        self._fact_provider = fact_provider
        self._scope_cache = scope_cache or ScopeHashCache()

    def cause(self, request: MarketScopeRequest) -> dict[str, Any]:
        """Resolve a strategy request with fast-path or recompute semantics."""

        canonical_request, resolved = self._base_resolve(request)
        if _is_single_source_fast_path(resolved):
            final_scope = resolved.with_dedup(_cache_fast_path_diagnostics())
            return {"result": self._cache_reader(canonical_request, final_scope), "resolved_scope": final_scope.to_dict()}

        facts = tuple(self._fact_provider(canonical_request, resolved))
        deduped, diagnostics = deduplicate_or_guard_disjoint(facts)
        final_scope = resolved.with_dedup(diagnostics)
        cached = self._scope_cache.read(final_scope.scope_hash)
        if cached is not None:
            return {"result": cached["result"], "resolved_scope": final_scope.to_dict()}

        result = recompute_strategy_payload(
            deduped,
            focus_brand_key=canonical_request.brand,
            source=canonical_request.source,
            measure=canonical_request.measure,
        )
        result["market_id"] = f"scope:{final_scope.scope_hash[:12]}"
        response = {"result": result, "resolved_scope": final_scope.to_dict()}
        self._scope_cache.write(final_scope.scope_hash, response)
        return response

    def resolve(self, request: MarketScopeRequest) -> ResolvedScope:
        """Resolve options and run the same per-request disjoint guard."""

        canonical_request, resolved = self._base_resolve(request)
        if _is_single_source_fast_path(resolved):
            return resolved.with_dedup(_cache_fast_path_diagnostics())
        facts = tuple(self._fact_provider(canonical_request, resolved))
        _, diagnostics = deduplicate_or_guard_disjoint(facts)
        return resolved.with_dedup(diagnostics)

    def _base_resolve(self, request: MarketScopeRequest) -> tuple[MarketScopeRequest, ResolvedScope]:
        """Canonicalize a request and expand its option selection."""

        canonical_request = _canonical_request(request)
        options = self._catalog.options_for_brand(canonical_request.brand, view_family=canonical_request.view_family)
        return canonical_request, normalize_market_scope(canonical_request, options)


class GeneralScopeResolver:
    """Forward-compatible stub for the later general-view stage."""

    def resolve(self, request: MarketScopeRequest) -> ResolvedScope:
        """Reject general-view resolution until the mart data gate is complete."""

        del request
        raise NotImplementedError("general market scope is deferred to a later stage")

    def cause(self, request: MarketScopeRequest) -> dict[str, Any]:
        """Reject general-view execution until the mart data gate is complete."""

        del request
        raise NotImplementedError("general market scope is deferred to a later stage")


def _is_single_source_fast_path(resolved: ResolvedScope) -> bool:
    """Return true when the legacy cache_cause row is still a 1:1 answer."""

    return (
        resolved.view_family is ViewFamily.STRATEGY
        and len(resolved.selected_option_ids) == 1
        and resolved.selected_option_ids[0].startswith("source:")
        and len(resolved.resolved_source_markets) == 1
    )


def _canonical_request(request: MarketScopeRequest) -> MarketScopeRequest:
    """Return a request with canonical source and measure labels."""

    source = normalize_source(request.source)
    return MarketScopeRequest(
        brand=request.brand,
        view_family=request.view_family,
        source=source,
        measure=normalize_measure(source, request.measure),
        option_ids=request.option_ids,
        view=request.view,
    )


def _cache_fast_path_diagnostics() -> DedupDiagnostics:
    """Return diagnostics for a legacy single-market cache hit."""

    return DedupDiagnostics(
        dedup_strategy="legacy_cache_fast_path_v1",
        dedup_key_version="cache_cause_pk_v1",
        candidate_fact_count=0,
        deduped_fact_count=0,
        dropped_duplicate_count=0,
        disjoint=True,
        overlap_brand_key_count=0,
    )
