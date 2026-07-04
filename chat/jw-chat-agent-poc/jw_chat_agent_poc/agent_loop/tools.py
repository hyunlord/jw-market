from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
import logging
from typing import Any, Mapping

from jw_chat_agent_poc.agent_loop.external_tools import clinical_call, disease_stats_call, drug_info_call, patent_call, procedure_stats_call, search_news_call, web_search_call
from jw_chat_agent_poc.agent_loop.periods import AgentPeriodGrounding, build_period_grounding, display_period, require_available_period, resolve_relative_expression
from jw_chat_agent_poc.agent_loop.query_tools import BRAND_TOOLS, PERIOD_TOOLS, brand_metric, catalog_for, compare_series, query_spec, top_brands
from jw_chat_agent_poc.agent_loop.schemas import tool_schemas
from jw_chat_agent_poc.agent_loop.tool_helpers import closest_allowed_brand, ground_news_query, market_members, metric_measure, period_filters, system_current_month
from jw_chat_agent_poc.resolver import BrandResolver, UnsupportedBrandError
from jw_chat_agent_poc.tools.deep_analysis import DeepAnalysisNewsTool
from jw_chat_agent_poc.tools.external import ExternalApiClient
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.query_layer import StrategicQueryLayer
from jw_chat_agent_poc.tools.query_layer.spec import parse_spec


logger = logging.getLogger(__name__)
QUERY_FAILED_STATUS = "query_failed"
UNSUPPORTED_STATUS = "unsupported"


@dataclass(frozen=True, slots=True)
class ToolExecution:
    status: str
    preview: str
    call: dict[str, Any]
    arguments: Mapping[str, str]


class AgentToolFacade:
    def __init__(
        self,
        *,
        metrics: MetricsTool,
        resolver: BrandResolver,
        current_month: Callable[[], str] | None = None,
        allowed_brands: tuple[str, ...] = (),
        period_grounding: AgentPeriodGrounding | None = None,
        news: DeepAnalysisNewsTool | None = None,
        external: ExternalApiClient | None = None,
        query_layer: StrategicQueryLayer | None = None,
    ) -> None:
        self._metrics = metrics
        self._resolver = resolver
        self._current_month = current_month or system_current_month
        self._allowed_brands = allowed_brands
        self._periods = period_grounding or build_period_grounding("", self._current_month)
        self._news = news or DeepAnalysisNewsTool()
        self._external = external or ExternalApiClient()
        self._query_layer = query_layer

    def schemas(self) -> tuple[dict[str, Any], ...]:
        return tool_schemas(self._allowed_brands, self._periods.schema_periods, self._query_catalog())

    def execute(self, name: str, arguments: Mapping[str, str]) -> ToolExecution:
        try:
            grounded_arguments = self.ground_arguments(name, arguments)
        except (LookupError, TypeError, ValueError, UnsupportedBrandError) as exc:
            return _tool_error(name, arguments, str(exc), status=UNSUPPORTED_STATUS, error=exc)
        try:
            if name == "get_metric":
                return self._metric(grounded_arguments)
            if name == "get_market_scope":
                return self._market_scope(grounded_arguments)
            if name == "resolve_relative_date":
                return self._relative_date(grounded_arguments)
            if name == "search_news":
                return self._search_news(grounded_arguments)
            if name == "get_disease_stats":
                return self._disease_stats(grounded_arguments)
            if name == "get_procedure_stats":
                return self._procedure_stats(grounded_arguments)
            if name == "search_clinical":
                return self._clinical(grounded_arguments)
            if name == "search_patent":
                return self._patent(grounded_arguments)
            if name == "search_drug_info":
                return self._drug_info(grounded_arguments)
            if name == "web_search":
                return self._web_search(grounded_arguments)
            if name == "get_brand_sales":
                return self._query_metric(grounded_arguments, "sales")
            if name == "get_brand_share":
                return self._query_metric(grounded_arguments, "market_share")
            if name == "get_brand_series":
                return self._query_metric(grounded_arguments, "series")
            if name == "compare_brands_series":
                return self._compare_brands_series(grounded_arguments)
            if name == "get_top_brands":
                return self._top_brands(grounded_arguments)
            if name == "query":
                return self._query_spec(grounded_arguments)
        except UnsupportedBrandError as exc:
            return _tool_error(name, arguments, str(exc), status=UNSUPPORTED_STATUS, error=exc)
        except (LookupError, TypeError, ValueError) as exc:
            return _tool_error(
                name,
                arguments,
                _query_failed_message(),
                status=QUERY_FAILED_STATUS,
                error=exc,
                render_data_extra=self._error_render_context(name, grounded_arguments),
            )
        return _tool_error(
            name,
            grounded_arguments,
            f"지원하지 않는 agent tool: {name}",
            status=UNSUPPORTED_STATUS,
            error=None,
        )

    def ground_arguments(self, name: str, arguments: Mapping[str, str]) -> Mapping[str, str]:
        grounded = {str(key): str(value) for key, value in arguments.items()}
        if name in BRAND_TOOLS:
            grounded["brand"] = self._brand(grounded)
        if name == "search_news":
            grounded["query"] = ground_news_query(grounded.get("query", ""), grounded["brand"])
        if name in PERIOD_TOOLS:
            period = require_available_period(grounded.get("period"), self._periods)
            if period is not None:
                grounded["period"] = period
        if name == "query" and "brand" not in grounded and self._allowed_brands:
            grounded["brand"] = self._allowed_brands[0]
        return grounded

    def _metric(self, arguments: Mapping[str, str]) -> ToolExecution:
        brand = self._brand(arguments)
        measure = metric_measure(arguments.get("measure", "sales"))
        period_arg = arguments.get("period")
        if self._query_layer is not None:
            try:
                call = self._query_layer.brand_metric(brand, measure, display_period(period_arg, self._periods))
            except (LookupError, TypeError, ValueError):
                pass
            else:
                return ToolExecution("ok", f"{brand} {measure} query-layer", call, arguments)
        call = self._metrics.get_brand_metric(brand, metric=measure, period=display_period(period_arg, self._periods), filter_entries=period_filters(period_arg))
        data = call.get("render_data", {})
        period = data.get("period") if isinstance(data, dict) else ""
        if isinstance(data, dict) and self._metrics._mode != "cache":
            data["period"] = display_period(period_arg, self._periods)
            if data.get("ms_recent_pct") is None and data.get("market_share") is not None:
                data["ms_recent_pct"] = data.get("market_share")
            call["summary_text"] = f"{brand}의 {data['period']} {measure} 값을 fixture cache에서 확인했습니다."
        return ToolExecution("ok", f"{brand} {measure} {period}", call, arguments)

    def _market_scope(self, arguments: Mapping[str, str]) -> ToolExecution:
        brand = self._brand(arguments)
        if self._query_layer is not None:
            try:
                call = self._query_layer.market_scope(brand)
            except (LookupError, TypeError, ValueError):
                pass
            else:
                data = call.setdefault("render_data", {})
                if isinstance(data, dict):
                    data["view_type"] = arguments.get("view", "market_landscape")
                return ToolExecution("ok", f"{brand} query-layer market scope", call, arguments)
        if self._metrics._mode != "cache":
            return self._fixture_market_scope(brand, arguments)
        snapshot = self._metrics._cache.snapshot()
        bridge = self._metrics._find_brand_bridge(snapshot.cache_brands, brand)
        market_id = str(bridge.get("market_id") or "")
        call = self._metrics.get_market_landscape(market_id, view_type=arguments.get("view", "market_landscape"))
        members = market_members(snapshot.cache_brands, market_id)
        data = call.setdefault("render_data", {})
        data["anchor_brand"] = brand
        data["member_brands"] = members
        return ToolExecution("ok", f"{brand} market={market_id} members={','.join(members)}", call, arguments)

    def _fixture_market_scope(self, brand: str, arguments: Mapping[str, str]) -> ToolExecution:
        market_id = "ml_006" if brand in {"리바로", "리바로젯"} else "mock_market"
        call = self._metrics.get_market_landscape(market_id, view_type=arguments.get("view", "market_landscape"))
        data = call.setdefault("render_data", {})
        data["anchor_brand"] = brand
        data["member_brands"] = tuple(self._metrics._data.get("brands", {}).keys())
        return ToolExecution("ok", f"{brand} market={market_id}", call, arguments)

    def _relative_date(self, arguments: Mapping[str, str]) -> ToolExecution:
        expression = arguments.get("expression", "")
        current = self._current_month()
        period = resolve_relative_expression(expression, current, self._periods)
        call = {
            "source": "cache",
            "tool": "resolve_relative_date",
            "summary_text": f"{expression}은 현재 {current} 기준 {period}입니다. 데이터 가용 기간은 {self._periods.first_period}~{self._periods.latest_period}입니다.",
            "render_data": {
                "expression": expression,
                "period": period,
                "current_month": current,
                "basis": f"현재 {current} 기준 계산",
                "available_periods": {"first": self._periods.first_period, "latest": self._periods.latest_period},
            },
        }
        return ToolExecution("ok", f"{expression}->{period}", call, arguments)

    def _search_news(self, arguments: Mapping[str, str]) -> ToolExecution:
        brand = self._brand(arguments)
        query = arguments.get("query", "")
        call = search_news_call(self._news, brand, query)
        return ToolExecution("ok", f"{brand} news query={query}", call, arguments)

    def _disease_stats(self, arguments: Mapping[str, str]) -> ToolExecution:
        brand = self._brand(arguments)
        resolution = self._resolver.resolve(brand, allow_default=False)
        call = disease_stats_call("", resolution, self._external)
        call["render_data"]["brand"] = brand
        return ToolExecution("ok", f"{brand} HIRA disease stats", call, arguments)

    def _procedure_stats(self, arguments: Mapping[str, str]) -> ToolExecution:
        brand = self._brand(arguments)
        resolution = self._resolver.resolve(brand, allow_default=False)
        question = arguments.get("query") or ""
        call = procedure_stats_call(question, resolution, self._external)
        call["render_data"]["brand"] = brand
        return ToolExecution("ok", f"{brand} HIRA procedure stats", call, arguments)

    def _clinical(self, arguments: Mapping[str, str]) -> ToolExecution:
        brand = self._brand(arguments)
        resolution = self._resolver.resolve(brand, allow_default=False)
        call = clinical_call(resolution, self._external)
        call["render_data"]["brand"] = brand
        return ToolExecution("ok", f"{brand} clinical", call, arguments)

    def _patent(self, arguments: Mapping[str, str]) -> ToolExecution:
        brand = self._brand(arguments)
        resolution = self._resolver.resolve(brand, allow_default=False)
        call = patent_call(resolution, self._external)
        call["render_data"]["brand"] = brand
        query = arguments.get("query", "")
        if self._query_layer is not None and _asks_competitor_ingredients(query):
            _attach_competitor_patent_context(call, brand, resolution.molecule_en, self._query_layer, self._external)
        return ToolExecution("ok", f"{brand} patent", call, arguments)

    def _drug_info(self, arguments: Mapping[str, str]) -> ToolExecution:
        brand = self._brand(arguments)
        resolution = self._resolver.resolve(brand, allow_default=False)
        call = drug_info_call(resolution, self._external)
        call["render_data"]["brand"] = brand
        return ToolExecution("ok", f"{brand} MFDS permission", call, arguments)

    def _web_search(self, arguments: Mapping[str, str]) -> ToolExecution:
        brand = self._brand(arguments)
        resolution = self._resolver.resolve(brand, allow_default=False)
        question = arguments.get("query") or ""
        call = web_search_call(question, resolution, self._external)
        call["render_data"]["brand"] = brand
        return ToolExecution(call.get("status", "ok"), f"{brand} web search", call, arguments)

    def _query_metric(self, arguments: Mapping[str, str], metric: str) -> ToolExecution:
        brand = self._brand(arguments)
        period = display_period(arguments.get("period"), self._periods)
        result = brand_metric(self._query_layer, brand, metric, period)
        return ToolExecution("ok", result.preview, result.call, arguments)

    def _compare_brands_series(self, arguments: Mapping[str, str]) -> ToolExecution:
        brand = self._brand(arguments)
        result = compare_series(self._query_layer, brand, arguments.get("comparison_brand", ""))
        return ToolExecution("ok", result.preview, result.call, arguments)

    def _top_brands(self, arguments: Mapping[str, str]) -> ToolExecution:
        brand = self._brand(arguments)
        result = top_brands(self._query_layer, brand, arguments.get("limit"))
        return ToolExecution("ok", result.preview, result.call, arguments)

    def _query_spec(self, arguments: Mapping[str, str]) -> ToolExecution:
        fallback_brand = arguments.get("brand") or (self._allowed_brands[0] if self._allowed_brands else "")
        result = query_spec(self._query_layer, arguments, fallback_brand)
        return ToolExecution("ok", result.preview, result.call, arguments)

    def _brand(self, arguments: Mapping[str, str]) -> str:
        raw = arguments.get("brand", "")
        if not raw:
            raise LookupError("brand argument is required")
        try:
            canonical = self._resolver.resolve(raw, allow_default=False).canonical_brand
        except UnsupportedBrandError:
            matched = closest_allowed_brand(raw, self._allowed_brands)
            if matched is not None:
                return matched
            raise UnsupportedBrandError(f"Invalid brand argument '{raw}'. Use only allowed canonical brand enum: {', '.join(self._allowed_brands) or 'none'}.") from None
        if not self._allowed_brands or canonical in self._allowed_brands:
            return canonical
        raise UnsupportedBrandError(f"Brand argument '{canonical}' is outside the allowed canonical brand enum: {', '.join(self._allowed_brands)}.")

    def _query_catalog(self):
        brand = self._allowed_brands[0] if self._allowed_brands else None
        return catalog_for(self._query_layer, brand)

    def _error_render_context(self, name: str, arguments: Mapping[str, str]) -> dict[str, Any]:
        if name != "query":
            return {}
        catalog = self._query_catalog()
        if catalog is None or not catalog.market_structure:
            return {}
        try:
            spec = parse_spec(arguments.get("spec", ""))
        except (SyntaxError, TypeError, ValueError):
            spec = {}
        market = str(spec.get("market") or catalog.market)
        if market != catalog.market:
            return {}
        return {
            "market_id": catalog.market,
            "market_name": catalog.market,
            "view": catalog.view,
            "source_label": catalog.source,
            "market_structure": catalog.market_structure,
        }


def _tool_error(
    name: str,
    arguments: Mapping[str, str],
    message: str,
    *,
    status: str,
    error: BaseException | None,
    render_data_extra: Mapping[str, Any] | None = None,
) -> ToolExecution:
    if status == QUERY_FAILED_STATUS:
        _log_tool_execution_failure(name, arguments, error)
    tool = "query_failed" if status == QUERY_FAILED_STATUS else "unsupported_metric"
    render_data = {
        "status": status,
        "message": message,
        "tool_name": name,
        "arguments": _safe_arguments(arguments),
    }
    if error is not None:
        render_data["error_type"] = type(error).__name__
    if render_data_extra:
        render_data.update(render_data_extra)
    call = {
        "source": "cache",
        "tool": tool,
        "summary_text": message,
        "render_data": render_data,
    }
    return ToolExecution("error", message, call, arguments)


def _query_failed_message() -> str:
    return "요청한 지표 조회 실행이 실패했습니다. 데이터가 없다는 뜻은 아니며, 수치를 추정하지 않습니다."


def _log_tool_execution_failure(name: str, arguments: Mapping[str, str], error: BaseException | None) -> None:
    logger.warning(
        "agent_tool_execution_failed",
        extra={
            "tool_name": name,
            "exception_type": type(error).__name__ if error is not None else "",
            "exception_message": str(error) if error is not None else "",
            "arguments": _safe_arguments(arguments),
        },
    )


def _safe_arguments(arguments: Mapping[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in arguments.items():
        key_text = str(key)
        if any(token in key_text.lower() for token in ("key", "secret", "password", "token")):
            safe[key_text] = "[redacted]"
            continue
        value_text = str(value)
        safe[key_text] = value_text if len(value_text) <= 500 else f"{value_text[:500]}..."
    return safe


def _asks_competitor_ingredients(query: str) -> bool:
    return any(token in query for token in ("경쟁 성분", "경쟁성분", "경쟁 molecule", "경쟁 Molecule", "경쟁 성분의"))


def _attach_competitor_patent_context(
    call: dict[str, Any],
    brand: str,
    anchor_molecules: tuple[str, ...],
    query_layer: StrategicQueryLayer,
    external: ExternalApiClient,
) -> None:
    data = call.setdefault("render_data", {})
    try:
        candidates = query_layer.competitor_molecule_candidates(brand, limit=5)
    except (LookupError, TypeError, ValueError):
        candidates = []
    data["competitor_ingredient_candidates"] = candidates
    data["competitor_patent_coverage"] = {
        "status": "attempted" if candidates else "no_candidate",
        "message": "경쟁 성분 후보별 MFDS/OrangeBook 조회를 시도했습니다." if candidates else "같은 시장 경쟁 성분 후보를 mart에서 확인하지 못했습니다.",
        "sources": "MFDS 의약품특허목록, FDA OrangeBook",
        "scope": "현재 특허 DB에서 확인되는 항목만 표시하며, 전체 독점권을 단정하지 않습니다.",
    }
    calls = data.setdefault("calls", [])
    if not isinstance(calls, list):
        return
    anchor_set = {molecule.casefold() for molecule in anchor_molecules if molecule}
    for candidate in candidates:
        molecule = str(candidate.get("molecule") or "").strip()
        if not molecule or molecule.casefold() in anchor_set:
            continue
        calls.append(asdict(external.mfds_patent(molecule)))
        calls.append(asdict(external.mfds_fda_orangebook(molecule)))
