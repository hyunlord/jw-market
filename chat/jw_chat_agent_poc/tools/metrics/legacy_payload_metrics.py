from __future__ import annotations

import json
import logging
from typing import Any

from jw_chat_agent_poc.agentic import FilterEntry, validate_metric_filters
from jw_chat_agent_poc.tools.metrics.legacy_payload_series import brand_series_10pt, market_size_series, top_brand_trend_series
from jw_chat_agent_poc.tools.metrics.cache_live import CausePayloadKey
from jw_chat_agent_poc.tools.metrics.sales_filtering import filtered_metric_result, unsupported_metric


LOGGER = logging.getLogger(__name__)


class CauseMetricMixin:
    def _get_filtered_cause_metric(self, brand: str, metric: str, filter_entries: tuple[FilterEntry, ...]) -> dict[str, Any]:
        snapshot = self._cache.snapshot()
        bridge = self._find_brand_bridge(snapshot.cache_brands, brand)
        plan = validate_metric_filters(filter_entries)
        source = plan.source or self._first_source(bridge) or "UBIST"
        measure = plan.measure or "sales"
        key = CausePayloadKey(
            brand=brand,
            view_type="market_landscape",
            source=source,
            measure=measure,
            market_id=str(bridge.get("market_id") or "strategy_006"),
        )
        try:
            payload = self._cause_cache.payload(key).payload
        except (LookupError, TypeError, ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning("legacy payload filtered metric lookup failed", exc_info=exc)
            return unsupported_metric(
                brand,
                metric,
                f"{brand}의 필터 적용 지표는 현재 운영 데이터에서 확정 경로를 찾지 못했습니다.",
                plan,
            )
        return filtered_metric_result(brand, metric, key, payload, plan)

    def _get_cause_metric(self, brand: str, metric: str) -> dict[str, Any]:
        snapshot = self._cache.snapshot()
        bridge = self._find_brand_bridge(snapshot.cache_brands, brand)
        metric_kind = self._cause_metric_kind(metric)
        key = CausePayloadKey(
            brand=brand,
            view_type="market_landscape",
            source=self._first_source(bridge) or "UBIST",
            measure="sales",
            market_id=str(bridge.get("market_id") or "strategy_006"),
        )
        try:
            payload = self._cause_cache.payload(key).payload
        except (LookupError, TypeError, ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning("legacy payload metric lookup failed", exc_info=exc)
            return self._unsupported(
                brand=brand,
                metric=metric,
                message=f"{brand}의 {metric} 지표는 현재 운영 데이터에서 확정 경로를 찾지 못했습니다.",
            )

        data = payload.get("data", {})
        if not isinstance(data, dict):
            return self._unsupported(brand=brand, metric=metric, message=f"{brand}의 {metric} 지표 payload 구조가 비어 있습니다.")

        if metric_kind == "hhi":
            return self._cause_hhi_result(brand, key, data)
        if metric_kind == "series":
            return self._cause_series_result(brand, key, data)
        if metric_kind == "momentum":
            return self._cause_scalar_result(
                brand=brand,
                key=key,
                data=data,
                metric="momentum",
                field="momentum_score",
                value=self._kpi_value(data, "target_momentum"),
                label="Momentum Score",
            )
        if metric_kind == "ei":
            return self._cause_scalar_result(
                brand=brand,
                key=key,
                data=data,
                metric="ei",
                field="ei",
                value=self._kpi_value(data, "target_ei", "ei"),
                label="EI",
            )
        return self._unsupported(brand=brand, metric=metric, message=f"{metric} 지표는 아직 지원하지 않습니다.")

    def _cause_hhi_result(self, brand: str, key: CausePayloadKey, data: dict[str, Any]) -> dict[str, Any]:
        sources = data.get("sources_data", {})
        if not isinstance(sources, dict):
            sources = {}
        hhi_recent = self._number(self._kpi_value(data, "hhi_recent", default=sources.get("hhi_recent")))
        hhi_series = sources.get("hhi_series_5y")
        if hhi_recent is None or not isinstance(hhi_series, list):
            return self._unsupported(brand=brand, metric="hhi", message=f"{brand}의 HHI 확정 경로가 payload에 없습니다.")
        years = ", ".join(
            f"{item.get('period') or item.get('year')} {self._format_number(item.get('hhi'))}"
            for item in hhi_series
            if isinstance(item, dict)
        )
        return {
            "source": "cache",
            "tool": "get_brand_metric",
            "summary_text": f"{brand} 시장의 최신 HHI는 {hhi_recent:.2f}입니다. 5년 HHI는 {years}입니다.",
            "render_data": {
                "brand": brand,
                "metric": "hhi",
                "market_id": key.market_id,
                "source_label": key.source,
                "hhi_recent": hhi_recent,
                "hhi_series_5y": hhi_series,
            },
        }

    def _cause_series_result(self, brand: str, key: CausePayloadKey, data: dict[str, Any]) -> dict[str, Any]:
        sources = data.get("sources_data", {})
        if not isinstance(sources, dict):
            sources = {}
        market_series = market_size_series(sources.get("market_size_series"), self._number, self._krw_to_eok)
        brand_series = brand_series_10pt(data, brand, self._krw_to_eok)
        top_brand_series = top_brand_trend_series(data, self._krw_to_eok, include_brands=(brand,))
        if not market_series and not brand_series and not top_brand_series:
            return self._unsupported(brand=brand, metric="series", message=f"{brand}의 월별 시계열 확정 경로가 payload에 없습니다.")

        latest_brand = brand_series[-1] if brand_series else {}
        latest_market = market_series[-1] if market_series else {}
        brand_part = (
            f"{brand} 최근 10포인트 매출은 {brand_series[0]['period']} {self._format_krw(brand_series[0]['value_krw'])}"
            f"에서 {latest_brand.get('period')} {self._format_krw(latest_brand.get('value_krw'))}로 이어집니다."
            if brand_series
            else f"{brand} 브랜드 시계열은 payload에 없습니다."
        )
        market_part = (
            f"시장 최신월 {latest_market.get('period')} 규모는 {self._format_krw(latest_market.get('value_krw'))}, "
            f"YoY {self._format_pct(latest_market.get('yoy_growth_pct'))}입니다."
            if market_series
            else "시장 시계열은 payload에 없습니다."
        )
        return {
            "source": "cache",
            "tool": "get_brand_metric",
            "summary_text": f"{brand_part} {market_part}",
            "render_data": {
                "brand": brand,
                "metric": "series",
                "market_id": key.market_id,
                "source_label": key.source,
                "brand_value_series_10pt": brand_series,
                "level_top5_trend_series": top_brand_series,
                "market_size_series": market_series,
            },
        }

    def _cause_scalar_result(
        self,
        brand: str,
        key: CausePayloadKey,
        data: dict[str, Any],
        metric: str,
        field: str,
        value: Any,
        label: str,
    ) -> dict[str, Any]:
        number = self._number(value)
        if number is None:
            return self._unsupported(brand=brand, metric=metric, message=f"{brand}의 {label} 확정 경로가 payload에 없습니다.")
        kpi = data.get("kpi", {})
        if not isinstance(kpi, dict):
            kpi = {}
        suffix = ""
        if metric == "ei":
            suffix = f" 기준은 {kpi.get('ei_basis') or 'N/A'}, 기간 {kpi.get('ei_period_years') or 'N/A'}년입니다."
        return {
            "source": "cache",
            "tool": "get_brand_metric",
            "summary_text": f"{brand}의 {label}는 {self._format_number(number)}입니다.{suffix}",
            "render_data": {
                "brand": brand,
                "metric": metric,
                "market_id": key.market_id,
                "source_label": key.source,
                field: number,
                "ei_basis": kpi.get("ei_basis"),
                "ei_period_years": kpi.get("ei_period_years"),
            },
        }

    @staticmethod
    def _is_cause_metric(metric: str) -> bool:
        lowered = metric.lower()
        return any(token in lowered for token in ("hhi", "series", "trend", "momentum", "ei", "시계열", "추이"))

    @staticmethod
    def _cause_metric_kind(metric: str) -> str:
        lowered = metric.lower()
        if "hhi" in lowered:
            return "hhi"
        if "momentum" in lowered:
            return "momentum"
        if lowered == "ei" or "ei" in lowered:
            return "ei"
        if any(token in lowered for token in ("series", "trend", "시계열", "추이")):
            return "series"
        return lowered

    @staticmethod
    def _kpi_value(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
        kpi = data.get("kpi", {})
        if not isinstance(kpi, dict):
            return default
        for key in keys:
            value = kpi.get(key)
            if value is not None:
                return value
        return default

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, int | float):
            return float(value)
        return None

    @staticmethod
    def _format_number(value: Any) -> str:
        if isinstance(value, int | float):
            return f"{float(value):.4f}".rstrip("0").rstrip(".")
        return "N/A"
