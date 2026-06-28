from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from jw_chat_agent_poc.tools.query_layer.catalog import QueryCatalog, default_catalog
from jw_chat_agent_poc.tools.query_layer.compute import (
    brand_average_share_data,
    brand_yoy_data,
    grouped_rows,
    grouped_trends,
    metric_render_data,
    top_trend,
)
from jw_chat_agent_poc.tools.query_layer.render import (
    level_segments,
    metric_name,
    metric_summary,
    result_rows_from_render_data,
    source_label,
)
from jw_chat_agent_poc.tools.query_layer.spec import as_list, bounded_limit, level_name, parse_spec, validate_spec
from jw_chat_agent_poc.tools.query_layer.store import (
    MariaDbStrategicMartReader,
    MartSnapshot,
    StrategicMartReader,
    TtlStrategicMartStore,
)


@dataclass(slots=True)
class QueryResultStore:
    """Small per-agent result handle registry for query outputs."""

    _items: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _counter: int = 0

    def put(self, rows: list[dict[str, Any]]) -> str:
        self._counter += 1
        result_id = f"qr_{self._counter:04d}"
        self._items[result_id] = rows
        return result_id

    def get(self, result_id: str) -> list[dict[str, Any]]:
        return self._items[result_id]


class StrategicQueryLayer:
    """Query-spec layer backed by mart_strategic_ml_brand_metric."""

    def __init__(
        self,
        *,
        reader: StrategicMartReader | None = None,
        result_store: QueryResultStore | None = None,
        ttl_seconds: int = 300,
    ) -> None:
        self._store = TtlStrategicMartStore(reader or MariaDbStrategicMartReader(), ttl_seconds=ttl_seconds)
        self._results = result_store or QueryResultStore()

    def catalog_for_brand(self, brand: str | None) -> QueryCatalog:
        snapshot = self._snapshot()
        market = snapshot.market_id_for_brand(brand or "")
        if market is None:
            return default_catalog()
        return QueryCatalog.from_snapshot(snapshot, market, snapshot.source_for_market(market))

    def brand_metric(self, brand: str, metric: str, period: str) -> dict[str, Any]:
        snapshot = self._snapshot()
        market = _required_market(snapshot, brand)
        source = snapshot.source_for_market(market)
        actual_period = _actual_period(snapshot, market, source, period)
        record = snapshot.record(market, brand, source)
        render_data = metric_render_data(snapshot, market, source, record, metric, actual_period)
        rows = result_rows_from_render_data(render_data)
        result_id = self._results.put(rows)
        render_data["query_result_id"] = result_id
        render_data["query_spec"] = {
            "source": source,
            "view": "market_landscape",
            "market": market,
            "filters": {"brand": brand, "period": actual_period},
            "metrics": [metric_name(metric)],
        }
        label = source_label(source)
        return {
            "source": label,
            "tool": "get_brand_metric",
            "summary_text": metric_summary(brand, render_data, label),
            "render_data": render_data,
        }

    def market_scope(self, brand: str) -> dict[str, Any]:
        snapshot = self._snapshot()
        market = _required_market(snapshot, brand)
        source = snapshot.source_for_market(market)
        latest = snapshot.latest_period(market, source)
        ranked = snapshot.ranked_brands(market, latest, source)
        rows = ranked[:10]
        result_id = self._results.put(rows)
        return {
            "source": source_label(source),
            "tool": "get_market_landscape",
            "summary_text": f"{brand}이 속한 {market} 시장의 상위 브랜드를 전략 mart에서 조회했습니다.",
            "render_data": {
                "market": market,
                "market_id": market,
                "market_name": market,
                "level": "Brand",
                "view_type": "market_landscape",
                "period": latest,
                "anchor_brand": brand,
                "member_brands": tuple(row["brand"] for row in ranked),
                "market_size_recent_krw": snapshot.market_value(market, latest, source),
                "market_size_억원": round(snapshot.market_value(market, latest, source) / 100_000_000, 2),
                "level_segments": level_segments(rows),
                "source_label": source_label(source),
                "query_result_id": result_id,
                "query_spec": {"source": source, "view": "market_landscape", "market": market, "group_by": ["product"], "sort": "sales_desc"},
            },
        }

    def market_member_metric(self, anchor_brand: str, member_brand: str) -> dict[str, Any]:
        snapshot = self._snapshot()
        market = _required_market(snapshot, anchor_brand)
        source = snapshot.source_for_market(market)
        latest = snapshot.latest_period(market, source)
        record = snapshot.record(market, member_brand, source)
        data = metric_render_data(snapshot, market, source, record, "series", latest)
        data["metric"] = "market_member_series"
        data["market_member_source_brand"] = anchor_brand
        data["data_scope"] = "mart_level_top5_trend"
        result_id = self._results.put(result_rows_from_render_data(data))
        data["query_result_id"] = result_id
        return {
            "source": source_label(source),
            "tool": "get_brand_metric",
            "summary_text": metric_summary(member_brand, data, source_label(source)),
            "render_data": data,
        }

    def top_brands(self, brand: str, limit: int = 5) -> dict[str, Any]:
        snapshot = self._snapshot()
        market = _required_market(snapshot, brand)
        source = snapshot.source_for_market(market)
        latest = snapshot.latest_period(market, source)
        ranked = snapshot.ranked_brands(market, latest, source)[: max(1, min(limit, 20))]
        data = {
            "brand": brand,
            "metric": "market_top_brands",
            "period": latest,
            "market_id": market,
            "market_name": market,
            "source_label": source_label(source),
            "level": "Brand",
            "level_segments": level_segments(ranked),
            "level_top5_trend_series": top_trend(snapshot, market, source, latest, brand, limit=max(limit, 5)),
            "market_size_recent_krw": snapshot.market_value(market, latest, source),
            "market_size_억원": round(snapshot.market_value(market, latest, source) / 100_000_000, 2),
        }
        data["query_result_id"] = self._results.put(ranked)
        data["query_spec"] = {"source": source, "view": "market_landscape", "market": market, "group_by": ["product"], "sort": "sales_desc", "limit": limit}
        return {"source": source_label(source), "tool": "get_brand_metric", "summary_text": f"{brand} 시장 상위 브랜드를 전략 mart에서 조회했습니다.", "render_data": data}

    def query(self, raw_spec: str | Mapping[str, Any], fallback_brand: str) -> dict[str, Any]:
        spec = parse_spec(raw_spec)
        snapshot = self._snapshot()
        market = str(spec.get("market") or snapshot.market_id_for_brand(fallback_brand) or default_catalog().market)
        source = str(spec.get("source") or snapshot.source_for_market(market))
        catalog = QueryCatalog.from_snapshot(snapshot, market, source)
        validate_spec(spec, catalog)
        limit = bounded_limit(spec.get("limit"), 10)
        derive = set(as_list(spec.get("derive")))
        if "yoy" in derive:
            return self._derived_query(snapshot, market, source, spec, fallback_brand, "yoy")
        if "average" in derive:
            return self._derived_query(snapshot, market, source, spec, fallback_brand, "average")
        rows = grouped_rows(snapshot, market, source, spec, limit)
        result_id = self._results.put(rows)
        filters = spec.get("filters") if isinstance(spec.get("filters"), dict) else {}
        subject_brand = str(filters.get("brand") or fallback_brand)
        data = {
            "brand": subject_brand,
            "metric": "query_spec",
            "period": snapshot.latest_period(market, source),
            "market_id": market,
            "market_name": market,
            "level": level_name(spec),
            "level_segments": level_segments(rows),
            "source_label": source_label(source),
            "query_result_id": result_id,
            "query_spec": spec,
            "applied_filters": filters,
            "schema_ok": True,
        }
        group_by = tuple(as_list(spec.get("group_by")))
        if "period" in group_by or "trend" in derive:
            trends = grouped_trends(snapshot, market, source, spec, limit)
            data["level_top5_trend_series"] = trends
        return {"source": source_label(source), "tool": "get_brand_metric", "summary_text": f"query(spec) {result_id}를 전략 mart에서 실행했습니다.", "render_data": data}

    def _snapshot(self) -> MartSnapshot:
        return self._store.snapshot()

    def _derived_query(self, snapshot: MartSnapshot, market: str, source: str, spec: Mapping[str, Any], fallback_brand: str, kind: str) -> dict[str, Any]:
        filters = spec.get("filters") if isinstance(spec.get("filters"), dict) else {}
        brand = str(filters.get("brand") or fallback_brand)
        if kind == "yoy":
            data = brand_yoy_data(snapshot, market, source, brand)
        else:
            count = bounded_limit(filters.get("periods"), 6)
            data = brand_average_share_data(snapshot, market, source, brand, count)
        data.update(
            {
                "market_id": market,
                "market_name": market,
                "source_label": source_label(source),
                "query_spec": spec,
                "applied_filters": filters,
                "schema_ok": True,
            }
        )
        data["query_result_id"] = self._results.put(result_rows_from_render_data(data))
        return {"source": source_label(source), "tool": "get_brand_metric", "summary_text": f"query(spec) {data['query_result_id']}를 전략 mart에서 실행했습니다.", "render_data": data}


def _actual_period(snapshot: MartSnapshot, market: str, source: str, period: str) -> str:
    if period in {"", "latest"}:
        return snapshot.latest_period(market, source)
    return period


def _required_market(snapshot: MartSnapshot, brand: str) -> str:
    market = snapshot.market_id_for_brand(brand)
    if market is None:
        raise LookupError(f"strategic mart has no market for brand: {brand}")
    return market
