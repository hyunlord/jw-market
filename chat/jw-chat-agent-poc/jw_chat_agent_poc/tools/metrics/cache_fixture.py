from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jw_chat_agent_poc.agentic import FilterEntry, validate_metric_filters
from jw_chat_agent_poc.tools.metrics.cache_cause_metrics import CauseMetricMixin
from jw_chat_agent_poc.tools.metrics.cache_helpers import CacheMetricHelperMixin
from jw_chat_agent_poc.tools.metrics.cache_live import (
    CausePayloadReader,
    CsdActivityReader,
    CsdActivityTarget,
    CsdActivityTargetReader,
    CsdActivityRow,
    MariaDbCsdActivityTargetReader,
    MariaDbCsdActivityReader,
    MetricsCacheReader,
    StaticCsdActivityReader,
    StaticCsdActivityTargetReader,
    TtlCausePayloadCache,
    TtlCsdActivityCache,
    TtlCsdActivityTargetCache,
    TtlMetricsCache,
    shared_cause_payload_cache,
    shared_metrics_cache,
)

if TYPE_CHECKING:
    from jw_chat_agent_poc.tools.query_layer import StrategicQueryLayer


class MetricsTool(CauseMetricMixin, CacheMetricHelperMixin):
    def __init__(
        self,
        fixture_path: Path | None = None,
        mode: str | None = None,
        cache_reader: MetricsCacheReader | None = None,
        cause_reader: CausePayloadReader | None = None,
        csd_activity_reader: CsdActivityReader | None = None,
        csd_activity_target_reader: CsdActivityTargetReader | None = None,
        query_layer: StrategicQueryLayer | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._mode = mode or os.environ.get("CHAT_METRICS_MODE", "fixture")
        path = fixture_path or Path(__file__).resolve().parents[2] / "fixtures" / "metrics_cache.json"
        self._data = json.loads(path.read_text(encoding="utf-8"))
        self._query_layer = query_layer
        self._legacy_cache_injected = cache_reader is not None or cause_reader is not None
        ttl = ttl_seconds or int(os.environ.get("CHAT_METRICS_TTL_SECONDS", "300"))
        self._cache = TtlMetricsCache(cache_reader, ttl_seconds=ttl) if cache_reader is not None else shared_metrics_cache(ttl)
        cause_ttl = int(os.environ.get("CHAT_CAUSE_TTL_SECONDS", str(ttl)))
        self._cause_cache = (
            TtlCausePayloadCache(cause_reader, ttl_seconds=cause_ttl)
            if cause_reader is not None
            else shared_cause_payload_cache(cause_ttl)
        )
        csd_ttl = int(os.environ.get("CHAT_CSD_ACTIVITY_TTL_SECONDS", str(ttl)))
        default_csd_reader: CsdActivityReader = MariaDbCsdActivityReader() if self._mode == "cache" else _fixture_csd_activity_reader()
        self._csd_activity_cache = TtlCsdActivityCache(csd_activity_reader or default_csd_reader, ttl_seconds=csd_ttl)
        default_csd_target_reader: CsdActivityTargetReader = (
            MariaDbCsdActivityTargetReader() if self._mode == "cache" else _fixture_csd_activity_target_reader()
        )
        self._csd_activity_target_cache = TtlCsdActivityTargetCache(
            csd_activity_target_reader or default_csd_target_reader,
            ttl_seconds=csd_ttl,
        )

    def get_market_landscape(self, market: str, level: str = "overall", view_type: str = "market_landscape") -> dict:
        if self._mode == "cache":
            return self._get_market_card_metric(market, level, view_type)
        item = self._data["markets"].get(market)
        if not item:
            raise LookupError(f"Unknown market fixture: {market}")
        return {
            "source": "cache",
            "tool": "get_market_landscape",
            "summary_text": (
                f"{item['label']} 시장은 {item['latest_period']} 기준 {item['market_size_krw']}원, "
                f"HHI {item['hhi']}입니다."
            ),
            "render_data": {
                "market": market,
                "level": level,
                "view_type": view_type,
                "series": item["market_size_series"],
                "cagr_5y_pct": item["cagr_5y_pct"],
                "hhi": item["hhi"],
            },
        }

    def get_brand_metric(
        self,
        brand: str,
        metric: str = "market_share",
        period: str = "2026-04",
        filter_entries: tuple[FilterEntry, ...] = (),
    ) -> dict:
        if self._mode == "cache":
            if self._query_layer is not None:
                if filter_entries:
                    plan = validate_metric_filters(filter_entries)
                    if plan.blocks_results:
                        return self._unsupported(brand, metric, "요청 필터는 d2 query-layer에서 지원하지 않습니다.")
                    if plan.channel is not None:
                        return self._query_layer.dimension_breakdown(
                            brand,
                            "channel",
                            source=plan.source or "",
                            period=plan.period_month or (str(plan.period_year) if plan.period_year else "latest"),
                        )
                    if plan.level is not None:
                        dimension = "product" if plan.level == "Brand" else plan.level.casefold().replace(" ", "_")
                        return self._query_layer.dimension_breakdown(
                            brand,
                            dimension,
                            source=plan.source or "",
                            period=plan.period_month or (str(plan.period_year) if plan.period_year else "latest"),
                        )
                if self._is_cause_metric(metric):
                    kind = self._cause_metric_kind(metric)
                    if kind in {"hhi", "momentum", "ei"}:
                        return self._query_layer.brand_derived_metric(brand, kind)
                if metric.casefold() == "growth_contribution":
                    return self._query_layer.brand_derived_metric(brand, "growth_contribution")
                return self._query_layer.brand_metric(brand, metric, period)
            if not self._legacy_cache_injected:
                raise LookupError("d2 query-layer is unavailable")
            return self._get_brand_card_metric(brand, metric, period, filter_entries)
        item = self._data["brands"].get(brand)
        if not item:
            raise LookupError(f"Unknown brand fixture: {brand}")
        if metric in {"hhi", "series", "trend"}:
            market_id = str(item.get("market_id") or "")
            if not market_id:
                raise LookupError(f"Fixture market is unresolved for {brand}")
            market = self._data["markets"].get(market_id, {})
            hhi = market.get("hhi")
            return {
                "source": "cache",
                "tool": "get_brand_metric",
                "summary_text": f"{brand} 시장의 fixture HHI는 {hhi}입니다.",
                "render_data": {
                    "brand": brand,
                    "metric": metric,
                    "period": market.get("latest_period"),
                    "hhi_recent": hhi,
                    "market_size_series": [
                        {"period": period, "value_krw": value, "value_억원": self._krw_to_eok(value)}
                        for period, value in sorted(market.get("market_size_series", {}).items())
                    ],
                },
            }
        value = item.get(metric)
        return {
            "source": "cache",
            "tool": "get_brand_metric",
            "summary_text": f"{brand}의 {period} {metric} 값은 {value}입니다.",
            "render_data": {"brand": brand, "metric": metric, "period": period, "value": value, **item},
        }

    def get_csd_activity_trend(self, brand: str, limit: int = 12) -> dict[str, Any]:
        target = self._csd_activity_target_cache.target_for_brand(brand)
        if target is None:
            return {
                "source": "cache",
                "tool": "csd_activity_trend",
                "status": "unsupported",
                "summary_text": f"{brand}는 현재 CSD ChannelDynamics aggregate 매핑이 없어 콜수/활동량을 조회하지 않습니다.",
                "render_data": {
                    "status": "unsupported",
                    "brand": brand,
                    "source_label": "CSD ChannelDynamics",
                    "message": "CSD aggregate 매핑 미보유",
                    "available_fields": _csd_available_fields(),
                    "unsupported_fields": _csd_unsupported_fields(),
                },
            }
        payload = self._csd_activity_cache.payload(target, limit=max(1, min(int(limit), 24)))
        if not payload.rows:
            return {
                "source": "cache",
                "tool": "csd_activity_trend",
                "status": "no_data",
                "summary_text": f"{brand}의 CSD ChannelDynamics aggregate 콜수/활동량 조회 결과가 없습니다.",
                "render_data": {
                    "status": "no_data",
                    "brand": brand,
                    "market": target.market,
                    "master_product": target.master_product,
                    "source_label": "CSD ChannelDynamics",
                    "data_grain": "월별 TOTAL 채널 aggregate 콜수/활동량(product_details 합계)",
                    "available_fields": _csd_available_fields(),
                    "unsupported_fields": _csd_unsupported_fields(),
                },
            }
        first = payload.rows[0]
        latest = payload.rows[-1]
        delta = latest.product_details - first.product_details
        delta_pct = (delta / first.product_details * 100) if first.product_details else None
        return {
            "source": "cache",
            "tool": "csd_activity_trend",
            "status": "ok",
            "summary_text": _csd_activity_summary(brand, payload.rows, delta, delta_pct),
            "render_data": {
                "status": "ok",
                "brand": brand,
                "market": target.market,
                "master_product": target.master_product,
                "source_label": "CSD ChannelDynamics",
                "data_grain": "월별 TOTAL 채널 aggregate 콜수/활동량(product_details 합계)",
                "available_fields": _csd_available_fields(),
                "unsupported_fields": _csd_unsupported_fields(),
                "series": [{"period": row.period_ym, "product_details": row.product_details} for row in payload.rows],
                "start_period": first.period_ym,
                "start_product_details": first.product_details,
                "latest_period": latest.period_ym,
                "latest_product_details": latest.product_details,
                "delta_product_details": delta,
                "delta_pct": delta_pct,
            },
        }

    def _get_brand_card_metric(
        self,
        brand: str,
        metric: str,
        period: str,
        filter_entries: tuple[FilterEntry, ...] = (),
    ) -> dict[str, Any]:
        if filter_entries:
            plan = validate_metric_filters(filter_entries)
            if plan.has_effective_filter:
                return self._get_filtered_cause_metric(brand, metric, filter_entries)
        if self._is_cause_metric(metric):
            return self._get_cause_metric(brand, metric)

        snapshot = self._cache.snapshot()
        bridge = self._find_brand_bridge(snapshot.cache_brands, brand)
        card = self._find_brand_card(snapshot.market_status, brand)
        front = card.get("front", {})
        back = card.get("back", {})
        extended = card.get("back_extended", {})
        period_recent = self._period_recent(snapshot.market_status, card)

        source_status = _source_status(front)
        value_blocked = _value_blocked(source_status)
        sales = None if value_blocked else front.get("value_recent")
        ms = None if value_blocked else front.get("ms_recent_pct")
        rank = None if value_blocked else card.get("rank")
        total = None if value_blocked else card.get("total_brands_in_market")
        market_size = extended.get("market_size_recent")
        brand_cagr = extended.get("brand_cagr_5y_pct", back.get("cagr_5y_pct"))
        market_cagr = extended.get("market_cagr_5y_pct")
        excess = extended.get("excess_growth_pct")
        source = front.get("default_source") or extended.get("source_label") or self._first_source(bridge)
        blocked_values = [_blocked_period_message(period_recent or period, source_status)] if value_blocked else []

        return {
            "source": "cache",
            "tool": "get_brand_metric",
            "summary_text": _brand_card_summary(
                brand=brand,
                period=period_recent,
                sales=self._format_krw(sales),
                share=self._format_pct(ms),
                rank=rank,
                total=total,
                market_size=self._format_krw(market_size),
                blocked_values=blocked_values,
            ),
            "render_data": {
                "brand": brand,
                "metric": metric,
                "period": period_recent or period,
                "market_id": card.get("market_id") or bridge.get("market_id"),
                "market_name": card.get("market_name") or bridge.get("market_name"),
                "source_label": source,
                "sales_krw": sales,
                "sales_억원": self._krw_to_eok(sales),
                "ms_recent_pct": ms,
                "rank": rank,
                "total_brands_in_market": total,
                "source_status": source_status,
                "blocked_metric_values": blocked_values,
                "market_size_recent_krw": market_size,
                "market_size_억원": self._krw_to_eok(market_size),
                "brand_cagr_5y_pct": brand_cagr,
                "market_cagr_5y_pct": market_cagr,
                "excess_growth_pct": excess,
                "gr_mom_pct": front.get("gr_mom_pct"),
                "gr_qoq_pct": front.get("gr_qoq_pct"),
                "gr_yoy_pct": front.get("gr_yoy_pct"),
                "gr_yoy_mat_pct": front.get("gr_yoy_mat_pct"),
                "gr_yoy_ym_pct": front.get("gr_yoy_ym_pct"),
            },
        }

    def _get_market_card_metric(self, market: str, level: str, view_type: str) -> dict[str, Any]:
        snapshot = self._cache.snapshot()
        market_id = "strategy_006" if market == "ml_006" else market
        cards = snapshot.market_status.get("brand_cards", [])
        if not isinstance(cards, list):
            raise TypeError("cache_market_status.brand_cards must be a list")
        card = next((item for item in cards if isinstance(item, dict) and item.get("market_id") == market_id), None)
        if card is None:
            raise LookupError(f"Unknown cache market: {market}")

        front = card.get("front", {})
        extended = card.get("back_extended", {})
        period_recent = self._period_recent(snapshot.market_status, card) or "latest"
        return {
            "source": "cache",
            "tool": "get_market_landscape",
            "summary_text": (
                f"{card.get('market_name', market_id)} 시장은 {period_recent} 기준 "
                f"{self._format_krw(extended.get('market_size_recent'))}입니다."
            ),
            "render_data": {
                "market": market_id,
                "level": level,
                "view_type": view_type,
                "period": period_recent,
                "market_size_recent_krw": extended.get("market_size_recent"),
                "market_size_억원": self._krw_to_eok(extended.get("market_size_recent")),
                "market_cagr_5y_pct": extended.get("market_cagr_5y_pct"),
                "source_label": front.get("default_source") or extended.get("source_label"),
            },
        }


def _source_status(front: dict[str, Any]) -> str:
    status = str(front.get("source_status", front.get("status")) or "OK")
    return status


def _value_blocked(status: str) -> bool:
    return status in {"query_failed", "mapping_failed", "incomplete_split", "missing", "error"}


def _blocked_period_message(period: str, status: str) -> dict[str, str]:
    return {
        "period": period,
        "status": status,
        "message": f"{period} 값은 조회 실패/시장 매핑 불완전으로 표시하지 않습니다.",
    }


def _brand_card_summary(
    *,
    brand: str,
    period: str,
    sales: str,
    share: str,
    rank: Any,
    total: Any,
    market_size: str,
    blocked_values: list[dict[str, str]],
) -> str:
    if blocked_values:
        message = blocked_values[0]["message"]
        return f"{brand}의 {period} 핵심 지표는 {message} 시장규모 {market_size}만 참고합니다."
    return (
        f"{brand}의 {period} 최신 매출은 {sales}, MS {share}, "
        f"순위 {rank}/{total}, 시장규모 {market_size}입니다. "
        f"성장률 파생 지표는 검산 피연산자가 있을 때만 표시합니다."
    )


def _csd_activity_summary(brand: str, rows: tuple[CsdActivityRow, ...], delta: int, delta_pct: float | None) -> str:
    first = rows[0]
    latest = rows[-1]
    pct = "" if delta_pct is None else f"({delta_pct:+.1f}%)"
    return (
        f"{brand}의 CSD ChannelDynamics aggregate 콜수/활동량은 "
        f"{first.period_ym} {first.product_details:,}건에서 {latest.period_ym} {latest.product_details:,}건으로 "
        f"{delta:+,}건 {pct} 변했습니다. impact level·HCP/의사별·기관별 세부는 이 데이터에 포함되지 않습니다."
    )


def _csd_available_fields() -> tuple[str, ...]:
    return ("period_ym", "market", "jw_channel", "master_product", "representing_company", "product_details")


def _csd_unsupported_fields() -> tuple[str, ...]:
    return ("impact level", "HCP/의사별", "기관별", "의사별", "활동일", "처방 lag", "비활동 대조군")


def _fixture_csd_activity_reader() -> StaticCsdActivityReader:
    return StaticCsdActivityReader(
        {
            ("LIVALO Market", "LIVALO"): (
                ("2026-03", 1389),
                ("2026-04", 1411),
                ("2026-05", 1769),
            ),
            ("LIVALOZET Market", "LIVALOZET"): (
                ("2026-03", 932),
                ("2026-04", 1018),
                ("2026-05", 1176),
            ),
        }
    )


def _fixture_csd_activity_target_reader() -> StaticCsdActivityTargetReader:
    return StaticCsdActivityTargetReader(
        (
            CsdActivityTarget("리바로", "LIVALO Market", "LIVALO"),
            CsdActivityTarget("리바로젯", "LIVALOZET Market", "LIVALOZET"),
        )
    )
