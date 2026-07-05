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
from jw_chat_agent_poc.tools.query_layer.market_structure import market_structure
from jw_chat_agent_poc.tools.query_layer.spec import as_list, bounded_limit, level_name, parse_spec, validate_spec
from jw_chat_agent_poc.tools.query_layer.store import (
    MariaDbStrategicMartReader,
    MartRecord,
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
        record = snapshot.record(market, brand, source)
        requested_period = _actual_period(snapshot, market, source, period)
        actual_period = _display_period(snapshot, record, requested_period, period)
        structure = market_structure(snapshot, market, source)
        if actual_period is None:
            return _failed_metric_call(brand, metric, requested_period, source, market=market, market_structure=structure)
        if snapshot.value_or_none(record, actual_period) is None:
            return _failed_metric_call(
                brand,
                metric,
                actual_period,
                source,
                snapshot.value_status(record, actual_period),
                market=market,
                market_structure=structure,
            )
        render_data = metric_render_data(snapshot, market, source, record, metric, actual_period)
        if structure:
            render_data["market_structure"] = structure
        if actual_period != requested_period:
            render_data["requested_period"] = requested_period
            render_data["fallback_period"] = actual_period
            render_data["blocked_metric_values"] = [_blocked_period_message(requested_period, snapshot.value_status(record, requested_period))]
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
        structure = market_structure(snapshot, market, source)
        render_data: dict[str, Any] = {
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
        }
        if structure:
            render_data["market_structure"] = structure
        return {
            "source": source_label(source),
            "tool": "get_market_landscape",
            "summary_text": f"{brand}이 속한 {market} 시장의 상위 브랜드를 전략 mart에서 조회했습니다.",
            "render_data": render_data,
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
        structure = market_structure(snapshot, market, source)
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
        if structure:
            data["market_structure"] = structure
        data["query_result_id"] = self._results.put(ranked)
        data["query_spec"] = {"source": source, "view": "market_landscape", "market": market, "group_by": ["product"], "sort": "sales_desc", "limit": limit}
        return {"source": source_label(source), "tool": "get_brand_metric", "summary_text": f"{brand} 시장 상위 브랜드를 전략 mart에서 조회했습니다.", "render_data": data}

    def competitor_molecule_candidates(self, brand: str, limit: int = 5) -> list[dict[str, Any]]:
        snapshot = self._snapshot()
        market = _required_market(snapshot, brand)
        source = snapshot.source_for_market(market)
        latest = snapshot.latest_period(market, source)
        anchor_record = snapshot.record(market, brand, source)
        anchor_molecule = anchor_record.molecule()
        rows: list[dict[str, Any]] = []
        for row in snapshot.ranked_brands(market, latest, source):
            candidate_brand = str(row.get("brand") or "")
            if not candidate_brand or candidate_brand == brand:
                continue
            record = snapshot.record(market, candidate_brand, source)
            molecule = record.molecule()
            if not molecule or molecule == anchor_molecule:
                continue
            rows.append(
                {
                    "rank": len(rows) + 1,
                    "molecule": molecule,
                    "brand": candidate_brand,
                    "source": source_label(source),
                    "market": market,
                    "period": latest,
                    "sales": f"{float(row.get('value') or 0.0) / 100_000_000:,.2f}억원",
                    "market_share": f"{float(row.get('ms_recent_pct') or 0.0):.2f}%",
                }
            )
            if len(rows) >= max(1, min(limit, 10)):
                break
        return rows

    def portfolio_decline_analysis(self, brands: tuple[Mapping[str, Any], ...], *, lookback_points: int = 5) -> dict[str, Any]:
        """Return strategic-brand market-share decliners with same-market gain candidates."""

        snapshot = self._snapshot()
        rows: list[dict[str, Any]] = []
        for item in brands:
            brand = str(item.get("brand") or "").strip()
            if not brand:
                continue
            row = _portfolio_decline_row(snapshot, brand, str(item.get("market_id") or ""), str(item.get("market_name") or ""), lookback_points)
            if row is not None:
                rows.append(row)
        rows.sort(key=lambda row: float(row.get("share_delta_pctp") or 0.0))
        result_id = self._results.put(rows)
        sources = tuple(dict.fromkeys(str(row.get("source") or "") for row in rows if row.get("source")))
        source = "/".join(source_label(source) for source in sources) if sources else "UBIST/IQVIA NSA"
        period = _portfolio_period_label(rows)
        summary = _portfolio_summary(rows, period)
        return {
            "source": source,
            "tool": "portfolio_decline_analysis",
            "summary_text": summary,
            "render_data": {
                "brand": "JW 주요 브랜드",
                "metric": "portfolio_market_share_decline",
                "scope_label": "JW 주요 브랜드",
                "period": period,
                "source_label": source,
                "decliners": rows,
                "decliner_count": len(rows),
                "lookback_points": lookback_points,
                "query_result_id": result_id,
                "interpretation_guardrail": "시장점유율 이동 후보이며 처방 이동 또는 인과를 직접 단정하지 않습니다.",
            },
        }

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
        structure = market_structure(snapshot, market, source)
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
        if structure:
            data["market_structure"] = structure
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
        structure = market_structure(snapshot, market, source)
        if structure:
            data["market_structure"] = structure
        data["query_result_id"] = self._results.put(result_rows_from_render_data(data))
        return {"source": source_label(source), "tool": "get_brand_metric", "summary_text": f"query(spec) {data['query_result_id']}를 전략 mart에서 실행했습니다.", "render_data": data}


def _actual_period(snapshot: MartSnapshot, market: str, source: str, period: str) -> str:
    if period in {"", "latest"}:
        return snapshot.latest_period(market, source)
    return period


def _display_period(snapshot: MartSnapshot, record: MartRecord, requested_period: str, raw_period: str) -> str | None:
    if raw_period in {"", "latest"}:
        return snapshot.latest_valid_period(record)
    if snapshot.value_or_none(record, requested_period) is not None:
        return requested_period
    previous = tuple(
        period
        for period in sorted(record.metric_history)
        if period < requested_period and snapshot.value_or_none(record, period) is not None
    )
    return previous[-1] if previous else None


def _blocked_period_message(period: str, status: str) -> dict[str, str]:
    return {
        "period": period,
        "status": status,
        "message": f"{period} 값은 조회 실패/시장 매핑 불완전으로 표시하지 않습니다.",
    }


def _failed_metric_call(
    brand: str,
    metric: str,
    period: str,
    source: str,
    status: str = "missing",
    *,
    market: str | None = None,
    market_structure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message = f"{period} 값은 조회 실패/시장 매핑 불완전으로 표시하지 않습니다."
    render_data: dict[str, Any] = {
        "brand": brand,
        "metric": metric_name(metric),
        "period": period,
        "status": "query_failed",
        "message": message,
        "source_status": status,
    }
    if market:
        render_data["market_id"] = market
        render_data["market_name"] = market
    if market_structure:
        render_data["market_structure"] = market_structure
    return {
        "source": source_label(source),
        "tool": "query_failed",
        "summary_text": message,
        "render_data": render_data,
    }


def _required_market(snapshot: MartSnapshot, brand: str) -> str:
    market = snapshot.market_id_for_brand(brand)
    if market is None:
        raise LookupError(f"strategic mart has no market for brand: {brand}")
    return market


def _portfolio_decline_row(
    snapshot: MartSnapshot,
    brand: str,
    raw_market_id: str,
    market_name: str,
    lookback_points: int,
) -> dict[str, Any] | None:
    for market in _market_candidates(raw_market_id, snapshot.market_id_for_brand(brand)):
        for source in _source_candidates(snapshot, market):
            try:
                record = snapshot.record(market, brand, source)
            except LookupError:
                continue
            periods = tuple(sorted(period for period in record.metric_history if period))
            if len(periods) < 2:
                continue
            start, end = _portfolio_period_pair(periods, lookback_points)
            start_ms = snapshot.share(market, record, start, source)
            end_ms = snapshot.share(market, record, end, source)
            delta = round(end_ms - start_ms, 4)
            if delta >= 0:
                return None
            return {
                "brand": brand,
                "market_id": market,
                "market_name": market_name or market,
                "source": source,
                "period_from": start,
                "period_to": end,
                "from_ms_pct": start_ms,
                "to_ms_pct": end_ms,
                "share_delta_pctp": delta,
                "from_sales_krw": snapshot.value(record, start),
                "to_sales_krw": snapshot.value(record, end),
                "rank": snapshot.rank(market, brand, end, source),
                "top_gainers": _portfolio_gainers(snapshot, market, source, start, end, brand),
            }
    return None


def _market_candidates(*values: str | None) -> tuple[str, ...]:
    candidates: list[str] = []
    for value in values:
        if not value:
            continue
        text = str(value)
        for candidate in (text, text.replace("strategy_", "ml_")):
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    return tuple(candidates)


def _source_candidates(snapshot: MartSnapshot, market: str) -> tuple[str, ...]:
    candidates: list[str] = []
    preferred = snapshot.source_for_market(market)
    for source in (preferred, "ubist", "iqvia_nsa"):
        if source and source not in candidates:
            candidates.append(source)
    return tuple(candidates)


def _portfolio_period_pair(periods: tuple[str, ...], lookback_points: int) -> tuple[str, str]:
    end = periods[-1]
    start_index = max(0, len(periods) - max(2, lookback_points))
    return periods[start_index], end


def _portfolio_gainers(
    snapshot: MartSnapshot,
    market: str,
    source: str,
    start: str,
    end: str,
    declined_brand: str,
) -> list[dict[str, Any]]:
    gainers: list[dict[str, Any]] = []
    for record in snapshot.market_records(market, source):
        if record.brand_name == declined_brand or start not in record.metric_history or end not in record.metric_history:
            continue
        start_ms = snapshot.share(market, record, start, source)
        end_ms = snapshot.share(market, record, end, source)
        delta = round(end_ms - start_ms, 4)
        if delta <= 0:
            continue
        gainers.append(
            {
                "brand": record.brand_name,
                "from_ms_pct": start_ms,
                "to_ms_pct": end_ms,
                "share_delta_pctp": delta,
                "from_sales_krw": snapshot.value(record, start),
                "to_sales_krw": snapshot.value(record, end),
                "rank": snapshot.rank(market, record.brand_name, end, source),
            }
        )
    gainers.sort(key=lambda row: float(row.get("share_delta_pctp") or 0.0), reverse=True)
    return gainers[:3]


def _portfolio_period_label(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    starts = tuple(dict.fromkeys(str(row.get("period_from") or "") for row in rows if row.get("period_from")))
    ends = tuple(dict.fromkeys(str(row.get("period_to") or "") for row in rows if row.get("period_to")))
    if len(starts) == 1 and len(ends) == 1:
        return f"{starts[0]}→{ends[0]}"
    return "최근 관측기간"


def _portfolio_summary(rows: list[dict[str, Any]], period: str) -> str:
    if not rows:
        return "JW 주요 브랜드의 최근 시장점유율 하락 브랜드는 확정 mart 기준으로 확인되지 않았습니다."
    leading = rows[:3]
    labels = ", ".join(f"{row['brand']} {float(row['share_delta_pctp']):.2f}%p" for row in leading)
    suffix = f"({period})" if period else ""
    return (
        f"JW 주요 브랜드 중 최근 시장점유율 하락 브랜드는 {labels} {suffix}입니다. "
        "동시장 상승 브랜드는 점유율 이동 후보로만 제시하며 직접 인과나 처방 이동은 단정하지 않습니다."
    )
