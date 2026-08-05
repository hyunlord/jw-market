from __future__ import annotations

from jw_chat_agent_poc.tool_use.market_scope_contract import (
    GeneralMetricUnavailableError,
    InvalidMarketLabelError,
    ScopeResolution,
    UnknownBrandError,
)
from jw_chat_agent_poc.tools.general_view_backend import GeneralMarket


def attach_normalization_trace(
    result: dict[str, object],
    resolution: ScopeResolution,
) -> dict[str, object]:
    if not resolution.normalizations:
        return result
    traced = dict(result)
    traced["scope_trace"] = {
        "normalizations": resolution.normalizations,
        "requested_view": "strategic",
    }
    return traced


def general_scope_result(
    tool_name: str,
    market: GeneralMarket,
    resolution: ScopeResolution,
) -> dict[str, object]:
    render_data = _general_base(market, _general_trace(resolution))
    render_data.update(
        {
            "scope": "market",
            "scope_label": "시장 전체",
            "market_size_recent_krw": market.market_size,
            "market_size_억원": (
                market.market_size / 100_000_000
                if market.market_size is not None
                else None
            ),
            "hhi_recent": rounded_hhi(market.hhi_recent),
            "hhi_period": market.hhi_period,
            "market_size_period": market.market_size_period,
            "active_members": tuple(row.brand for row in market.active_members),
            "active_member_count": len(market.active_members),
            "active_members_period": market.period,
            "display_members": tuple(row.brand for row in market.display_members),
            "display_member_count": len(market.display_members),
            "display_projection": "top_5",
        }
    )
    if market.member_population is not None:
        render_data["member_population"] = market.member_population
        render_data["member_population_count"] = len(market.member_population)
    return {
        "source": market.source,
        "tool": (
            "get_market_members"
            if tool_name.endswith("members")
            else "get_market_landscape"
        ),
        "summary_text": f"{market.atc4_code} 일반뷰 시장을 조회했습니다.",
        "render_data": render_data,
    }


def general_metric_result(
    market: GeneralMarket,
    metric: str,
    value: object,
    resolution: ScopeResolution,
) -> dict[str, object]:
    render_data = _general_base(market, _general_trace(resolution))
    render_data.update(
        {
            "metric": metric,
            "value": value,
            "unit_label": _general_unit(market, metric),
            "market_size_recent_krw": market.market_size,
            "brand_sales_krw": market.brand_value,
            "market_share": market.brand_share_pct,
            "rank": market.brand_rank,
            "hhi_recent": rounded_hhi(market.hhi_recent),
            "hhi_period": market.hhi_period,
            "market_size_period": market.market_size_period,
        }
    )
    return {
        "source": market.source,
        "tool": "get_brand_metric",
        "summary_text": (
            f"{market.brand or market.atc4_code} {metric}을 "
            "일반뷰 mart에서 조회했습니다."
        ),
        "render_data": render_data,
    }


def general_timeseries_result(
    market: GeneralMarket,
    metric: str,
    resolution: ScopeResolution,
) -> dict[str, object]:
    if not market.brand_metric_series:
        raise GeneralMetricUnavailableError("general brand timeseries is unavailable")
    key = metric.casefold()
    series = tuple(
        {
            "period": point.period,
            "value": (
                point.share_pct
                if key in {"share", "market_share"}
                else point.rank
                if key == "rank"
                else point.value
            ),
        }
        for point in market.brand_metric_series
    )
    result = general_metric_result(
        market,
        metric,
        series[-1]["value"] if series else None,
        resolution,
    )
    result["render_data"]["series"] = series
    return result


def general_comparison_result(
    anchor: GeneralMarket,
    comparison: GeneralMarket,
    metric: str,
    resolution: ScopeResolution,
) -> dict[str, object]:
    result = general_metric_result(
        anchor,
        metric,
        general_metric_value(anchor, metric),
        resolution,
    )
    result["render_data"].update(
        {
            "comparison_brand": comparison.brand,
            "comparison_value": general_metric_value(comparison, metric),
            "comparison_series": tuple(
                {"period": point.period, "value": point.value}
                for point in comparison.brand_metric_series
            ),
        }
    )
    return result


def general_metric_value(market: GeneralMarket, metric: str) -> object:
    normalized = metric.casefold()
    if normalized in {"sales", "revenue", "volume", "prescription_volume"}:
        return market.brand_value
    if normalized in {"share", "market_share"}:
        return market.brand_share_pct
    if normalized == "rank":
        return market.brand_rank
    if normalized == "hhi":
        return rounded_hhi(market.hhi_recent)
    if normalized == "growth_contribution" and market.growth_contribution is not None:
        return market.growth_contribution
    raise GeneralMetricUnavailableError(metric)


def assert_general_scope(
    market: GeneralMarket,
    atc4: tuple[str, ...],
    brand: str,
    *,
    filters: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> None:
    actual_atc4 = market.atc4_codes or (market.atc4_code.upper(),)
    if market.view_type != "general_view" or actual_atc4 != tuple(code.upper() for code in atc4):
        raise InvalidMarketLabelError("general backend returned a different scope")
    if filters and market.scope_filters != filters:
        raise InvalidMarketLabelError("general backend returned different composite filters")
    if market.brand is not None and market.brand != brand:
        raise UnknownBrandError(f"general backend returned a different brand for {brand}")


def rounded_hhi(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def _general_trace(resolution: ScopeResolution) -> dict[str, object]:
    return {
        "scope_kind": resolution.scope.kind.value,
        "atc4": resolution.scope.atc4,
        "source": resolution.source,
        "normalizations": resolution.normalizations,
        "fallback_reason": resolution.fallback_reason,
    }


def _general_base(
    market: GeneralMarket,
    trace: dict[str, object],
) -> dict[str, object]:
    atc4_codes = market.atc4_codes or (market.atc4_code,)
    return {
        "brand": market.brand,
        "period": market.period,
        "market": market.atc4_code,
        "market_id": market.atc4_code,
        "market_name": f"ATC4 {market.atc4_code}",
        "view_type": market.view_type,
        "market_basis": market.market_basis,
        "atc4_codes": atc4_codes,
        "scope_filters": market.scope_filters,
        "dashboard_tables": market.dashboard_tables,
        "source_label": market.source,
        "selected_data_path": market.selected_data_path,
        "scope_trace": trace,
        "query_spec": {
            "source": market.source,
            "view": "general",
            "market": market.atc4_code,
            "filters": {
                "atc4": list(atc4_codes),
                "brand": market.brand,
                "analysis_level": {
                    name: list(values) for name, values in market.scope_filters
                },
            },
        },
    }


def _general_unit(market: GeneralMarket, metric: str) -> str:
    normalized = metric.casefold()
    if normalized in {"share", "market_share"}:
        return "%"
    if normalized == "rank":
        return "rank"
    if normalized == "hhi":
        return "index"
    return market.unit
