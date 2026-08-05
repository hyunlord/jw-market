from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias, cast

from jw_chat_agent_poc.service.file_sql_query import (
    SqlFileSource,
    SqlQueryOutcome,
    fetch_sql_schema_columns,
    query_uploaded_sql,
)
from jw_chat_agent_poc.tools.query_layer import StrategicQueryLayer
from jw_chat_agent_poc.tool_use.market_definition_registry import (
    MarketDefinitionRegistry,
)


CatalogToolResult: TypeAlias = dict[str, Any] | tuple[str, ...] | SqlQueryOutcome
CatalogArguments: TypeAlias = Mapping[str, object]


class MarketCatalogBackend(Protocol):
    def brand_metric(
        self,
        brand: str,
        metric: str,
        period: str,
        market: str | None = None,
        source: str = "",
        history_points: int = 10,
    ) -> dict[str, Any]: ...

    def market_scope(self, brand: str, market: str | None = None) -> dict[str, Any]: ...

    def dimension_breakdown(
        self,
        brand: str,
        dimension: str,
        source: str = "",
        period: str = "latest",
        limit: int = 10,
        market: str | None = None,
        metric: str = "sales",
    ) -> dict[str, Any]: ...

    def market_member_metric(
        self,
        brand: str,
        comparison: str,
        market: str | None = None,
        metric: str = "series",
    ) -> dict[str, Any]: ...


class FileCatalogBackend(Protocol):
    def get_schema(
        self,
        *,
        conversation_id: str,
        sources: Sequence[SqlFileSource],
    ) -> tuple[str, ...]: ...

    def query(
        self,
        *,
        question: str,
        conversation_id: str,
        sources: Sequence[SqlFileSource],
    ) -> SqlQueryOutcome: ...

class ExistingFileCatalogBackend:
    """Adapt the existing session-scoped read-only file implementation."""

    def get_schema(
        self,
        *,
        conversation_id: str,
        sources: Sequence[SqlFileSource],
    ) -> tuple[str, ...]:
        return fetch_sql_schema_columns(conversation_id, sources)

    def query(
        self,
        *,
        question: str,
        conversation_id: str,
        sources: Sequence[SqlFileSource],
    ) -> SqlQueryOutcome:
        return query_uploaded_sql(question, conversation_id, sources)

@dataclass(frozen=True, slots=True)
class InternalToolAdapter:
    name: str
    execute: Callable[[CatalogArguments], CatalogToolResult]


class InternalToolAdapterRegistry:
    """Catalog-only adapters; the active provider does not import this module."""

    def __init__(
        self,
        *,
        market_layer: MarketCatalogBackend | StrategicQueryLayer,
        definition_registry: MarketDefinitionRegistry | None = None,
        file_backend: FileCatalogBackend | None = None,
    ) -> None:
        self._market = market_layer
        self._definitions = definition_registry
        self._files = file_backend or ExistingFileCatalogBackend()
        self._adapters = {
            adapter.name: adapter
            for adapter in (
                InternalToolAdapter("market.get_brand_metric", self._brand_metric),
                InternalToolAdapter("market.get_market_size", self._market_size),
                InternalToolAdapter("market.get_market_members", self._market_members),
                InternalToolAdapter("market.get_timeseries", self._timeseries),
                InternalToolAdapter("market.get_channel_breakdown", self._channel_breakdown),
                InternalToolAdapter("market.get_hhi", self._hhi),
                InternalToolAdapter(
                    "market.get_growth_contribution",
                    self._growth_contribution,
                ),
                InternalToolAdapter("market.compare_brands", self._compare_brands),
                InternalToolAdapter("market.get_definition", self._market_definition),
                InternalToolAdapter("file.get_schema", self._file_schema),
                InternalToolAdapter("file.query", self._file_query),
            )
        }

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def execute(self, name: str, arguments: CatalogArguments) -> CatalogToolResult:
        if name == "market.get_definition":
            return self._adapters[name].execute(arguments)
        scoped_execute = getattr(self._market, "execute_catalog_tool", None)
        if name.startswith("market.") and callable(scoped_execute):
            return cast(CatalogToolResult, scoped_execute(name, arguments))
        try:
            adapter = self._adapters[name]
        except KeyError as exc:
            raise LookupError(f"unregistered internal catalog tool: {name}") from exc
        return adapter.execute(arguments)

    def _brand_metric(self, arguments: CatalogArguments) -> dict[str, Any]:
        return self._market.brand_metric(
            _required_text(arguments, "brand"),
            _required_text(arguments, "metric"),
            _text(arguments, "period", "latest"),
            market=_optional_text(arguments, "market"),
            source=_text(arguments, "source", ""),
            history_points=_integer(arguments, "history_points", 10),
        )

    def _market_size(self, arguments: CatalogArguments) -> dict[str, Any]:
        return self._market.market_scope(
            _required_text(arguments, "brand"),
            market=_optional_text(arguments, "market"),
        )

    def _market_members(self, arguments: CatalogArguments) -> dict[str, Any]:
        return self._market.market_scope(
            _required_text(arguments, "brand"),
            market=_optional_text(arguments, "market"),
        )

    def _timeseries(self, arguments: CatalogArguments) -> dict[str, Any]:
        return self._market.brand_metric(
            _required_text(arguments, "brand"),
            _text(arguments, "metric", "sales"),
            _text(arguments, "period", "latest"),
            market=_optional_text(arguments, "market"),
            source=_text(arguments, "source", ""),
            history_points=_integer(arguments, "history_points", 10),
        )

    def _channel_breakdown(self, arguments: CatalogArguments) -> dict[str, Any]:
        return self._market.dimension_breakdown(
            _required_text(arguments, "brand"),
            "channel",
            source=_text(arguments, "source", ""),
            period=_text(arguments, "period", "latest"),
            limit=_integer(arguments, "limit", 10),
            market=_optional_text(arguments, "market"),
            metric=_text(arguments, "metric", "sales"),
        )

    def _hhi(self, arguments: CatalogArguments) -> dict[str, Any]:
        return self._market.brand_metric(
            _required_text(arguments, "brand"),
            "hhi",
            _text(arguments, "period", "latest"),
            market=_optional_text(arguments, "market"),
            source=_text(arguments, "source", ""),
            history_points=_integer(arguments, "history_points", 10),
        )

    def _growth_contribution(self, arguments: CatalogArguments) -> dict[str, Any]:
        return self._market.brand_metric(
            _required_text(arguments, "brand"),
            "growth_contribution",
            _text(arguments, "period", "latest"),
            market=_optional_text(arguments, "market"),
            source=_text(arguments, "source", ""),
            history_points=_integer(arguments, "history_points", 10),
        )

    def _compare_brands(self, arguments: CatalogArguments) -> dict[str, Any]:
        return self._market.market_member_metric(
            _required_text(arguments, "brand"),
            _required_text(arguments, "comparison_brand"),
            market=_optional_text(arguments, "market"),
            metric=_text(arguments, "metric", "series"),
        )

    def _market_definition(self, arguments: CatalogArguments) -> dict[str, Any]:
        if self._definitions is None:
            raise RuntimeError("market definition registry is not configured")
        return self._definitions.get_definition(arguments)

    def _file_schema(self, arguments: CatalogArguments) -> tuple[str, ...]:
        return self._files.get_schema(
            conversation_id=_required_text(arguments, "conversation_id"),
            sources=_sources(arguments),
        )

    def _file_query(self, arguments: CatalogArguments) -> SqlQueryOutcome:
        return self._files.query(
            question=_required_text(arguments, "question"),
            conversation_id=_required_text(arguments, "conversation_id"),
            sources=_sources(arguments),
        )

def _required_text(arguments: CatalogArguments, key: str) -> str:
    value = _optional_text(arguments, key)
    if value is None:
        raise ValueError(f"{key} is required")
    return value


def _optional_text(arguments: CatalogArguments, key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _text(arguments: CatalogArguments, key: str, default: str) -> str:
    return _optional_text(arguments, key) or default


def _integer(arguments: CatalogArguments, key: str, default: int) -> int:
    value = arguments.get(key)
    if value is None:
        return default
    try:
        return int(str(value))
    except ValueError:
        return default


def _sources(arguments: CatalogArguments) -> Sequence[SqlFileSource]:
    value = arguments.get("sources")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("sources must be a sequence of SqlFileSource")
    if not all(isinstance(source, SqlFileSource) for source in value):
        raise ValueError("sources must contain only SqlFileSource values")
    return cast(Sequence[SqlFileSource], value)
