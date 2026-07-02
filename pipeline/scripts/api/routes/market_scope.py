"""Additive HTTP routes for market-scope options, resolve, and cause."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from pipeline.scripts.api import db
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.config import config
from pipeline.scripts.api.market_scope.catalog import MarketScopeCatalog
from pipeline.scripts.api.market_scope.fact_collector import (
    FactIdentityIncompleteError,
    OverlapWithoutFactIdentityError,
    StrategyFact,
    collect_strategy_facts_from_mart,
)
from pipeline.scripts.api.market_scope.resolvers import StrategyScopeResolver
from pipeline.scripts.api.market_scope.types import (
    MarketScopeOption,
    MarketScopeRequest,
    MarketScopeValidationError,
    ResolvedScope,
    ViewFamily,
)
from pipeline.scripts.api.models.market_scope import MarketScopeCauseRequest, MarketScopeResolveRequest
from pipeline.scripts.api.validators.query_params import validate_cause_query


router = APIRouter()


@router.get("/api/market-scope/options", include_in_schema=False)
def options(
    brand: str = Query(...),
    view_family: str = Query("strategy"),
    source: str | None = Query(None),
) -> dict[str, Any]:
    """Return selectable source-market/group options for one brand."""

    family = _view_family(view_family)
    _reject_general(family)
    catalog = MarketScopeCatalog.load_default()
    all_options = catalog.options_for_brand(brand, view_family=family)
    filtered = _filter_options_by_source(all_options, source)
    return {
        "brand": brand,
        "view_family": family.value,
        "source": source,
        "options": [option.to_dict() for option in filtered],
        "catalog_version": catalog.catalog_version,
    }


@router.post("/api/market-scope/resolve", include_in_schema=False)
def resolve(payload: MarketScopeResolveRequest) -> dict[str, Any]:
    """Resolve a market scope and echo per-request disjoint diagnostics."""

    _reject_general(_view_family(payload.view_family))
    try:
        return build_strategy_resolver().resolve(_to_engine_request(payload)).to_dict()
    except MarketScopeValidationError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_market_scope", "message": str(exc)}) from exc
    except OverlapWithoutFactIdentityError as exc:
        raise HTTPException(status_code=409, detail={"error": "overlap_without_fact_identity", "message": str(exc)}) from exc


@router.post("/api/market-scope/cause", include_in_schema=False)
def cause(payload: MarketScopeCauseRequest) -> dict[str, Any]:
    """Return a portal-compatible cause envelope for the resolved scope."""

    _reject_general(_view_family(payload.view_family))
    try:
        scoped = build_strategy_resolver().cause(_to_engine_request(payload))
        result = scoped.get("result")
        if not isinstance(result, dict):
            raise MarketScopeValidationError("market-scope cause result is not an object")
        return {"status": "SUCCESS", "result": result}
    except MarketScopeValidationError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_market_scope", "message": str(exc)}) from exc
    except (FactIdentityIncompleteError, OverlapWithoutFactIdentityError) as exc:
        raise HTTPException(status_code=409, detail={"error": "unsafe_scope_union", "message": str(exc)}) from exc


def build_strategy_resolver() -> StrategyScopeResolver:
    """Build the strategy resolver with read-only DB dependencies."""

    return StrategyScopeResolver(
        catalog=MarketScopeCatalog.load_default(),
        cache_reader=_read_cache_cause,
        fact_provider=_read_strategy_facts,
    )


def _read_cache_cause(request: MarketScopeRequest, resolved: ResolvedScope) -> dict[str, Any]:
    """Read the legacy single-market ``cache_cause`` row."""

    view, source, measure = validate_cause_query(request.view, request.source, request.measure)
    row = db.fetch_one(
        """
        SELECT response_json
        FROM cache_cause
        WHERE brand = %s
          AND view_type = %s
          AND source = %s
          AND measure = %s
          AND market_id = %s
        LIMIT 1
        """,
        [request.brand, view, source, measure, resolved.resolved_source_markets[0]],
    )
    if not row:
        raise MarketScopeValidationError("single-market cache_cause row was not found")
    payload = compose_cached_json(row["response_json"], measure=measure)
    if not isinstance(payload, dict):
        raise MarketScopeValidationError("single-market cache_cause payload is not an object")
    return payload


def _read_strategy_facts(request: MarketScopeRequest, resolved: ResolvedScope) -> tuple[StrategyFact, ...]:
    """Collect strategy mart facts for the resolved union scope."""

    return collect_strategy_facts_from_mart(
        db.fetch_all,
        mart_db=config.db_name,
        source_markets=resolved.resolved_source_markets,
        source=request.source,
        measure=request.measure,
    )


def _to_engine_request(payload: MarketScopeResolveRequest) -> MarketScopeRequest:
    """Convert a parsed API body to the Stage 2 engine contract."""

    view = payload.view if isinstance(payload, MarketScopeCauseRequest) else "market_landscape"
    return MarketScopeRequest(
        brand=payload.brand,
        view_family=_view_family(payload.view_family),
        source=payload.source,
        measure=payload.measure,
        option_ids=tuple(payload.option_ids),
        view=view,
    )


def _view_family(value: str) -> ViewFamily:
    """Parse the view-family query/body value."""

    try:
        return ViewFamily(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_view_family", "view_family": value}) from exc


def _reject_general(view_family: ViewFamily) -> None:
    """Keep the Stage 3 route honest about the deferred general resolver."""

    if view_family is ViewFamily.GENERAL:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={"error": "general_scope_not_ready", "message": "general market scope is deferred to a later stage"},
        )


def _filter_options_by_source(options: tuple[MarketScopeOption, ...], source: str | None) -> tuple[MarketScopeOption, ...]:
    """Filter options by source only when the caller asks for a source."""

    if source is None:
        return options
    normalized = source.strip().upper()
    source_label = "IQVIA" if normalized in {"NSA", "IQVIA_NSA"} else normalized
    return tuple(option for option in options if source_label in option.available_sources)
