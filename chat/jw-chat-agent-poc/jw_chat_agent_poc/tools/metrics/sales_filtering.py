from __future__ import annotations

from dataclasses import replace
from typing import Any

from jw_chat_agent_poc.agentic import MetricFilterPlan
from jw_chat_agent_poc.tools.metrics.cache_live import CausePayloadKey
from jw_chat_agent_poc.tools.metrics.relative_periods import resolve_relative_periods
from jw_chat_agent_poc.tools.metrics.sales_filter_values import format_krw, format_pct, krw_to_eok, num
from jw_chat_agent_poc.tools.metrics.sales_series import brand_series, latest_period_label, market_series, period_label, resolved_year, select_periods


def filtered_metric_result(
    brand: str,
    metric: str,
    key: CausePayloadKey,
    payload: dict[str, Any],
    plan: MetricFilterPlan,
) -> dict[str, Any]:
    data = _payload_data(payload)
    if plan.blocks_results:
        return unsupported_metric(brand, metric, "요청한 매출 필터 중 현재 데이터가 지원하지 않는 조건이 있습니다.", plan)
    if plan.level is not None:
        return _level_result(brand, metric, key, data, plan)
    if plan.channel is not None:
        return _channel_result(brand, metric, key, data, plan)
    return _period_result(brand, metric, key, data, plan)


def unsupported_metric(
    brand: str,
    metric: str,
    message: str,
    plan: MetricFilterPlan,
    *,
    data_basis: dict[str, str] | None = None,
    interpretation_notes: tuple[dict[str, str], ...] = (),
) -> dict[str, Any]:
    transparency = _transparency_fields(plan, data_basis=data_basis, interpretation_notes=interpretation_notes)
    return {
        "source": "cache",
        "tool": "unsupported_metric",
        "summary_text": message,
        **transparency,
        "render_data": {
            "brand": brand,
            "metric": metric,
            "status": "unsupported",
            "message": message,
            **transparency,
        },
    }


def _period_result(
    brand: str,
    metric: str,
    key: CausePayloadKey,
    data: dict[str, Any],
    plan: MetricFilterPlan,
) -> dict[str, Any]:
    market_source = market_series(data)
    brand_source = brand_series(data, brand)
    relative = resolve_relative_periods(plan, _period_keys(market_source, brand_source))
    if relative is not None and relative.unsupported:
        blocked = replace(plan, unsupported=plan.unsupported + relative.unsupported)
        return unsupported_metric(brand, metric, "요청한 상대 날짜를 월 단위 데이터 기간으로 해석할 수 없습니다.", blocked, data_basis=relative.data_basis, interpretation_notes=relative.interpretation_notes)
    selected = relative.months if relative is not None else ()
    market_rows = select_periods(market_source, plan, selected)
    brand_rows = select_periods(brand_source, plan, selected)
    requested_period = _requested_period(plan, selected)
    fallback_period = ""
    if not market_rows and not brand_rows:
        fallback_period = _latest_previous_period(market_source, brand_source, requested_period)
        if not fallback_period:
            return unsupported_metric(brand, metric, "요청 기간에 해당하는 시계열 데이터가 없습니다.", plan)
        market_rows = _rows_for_period(market_source, fallback_period)
        brand_rows = _rows_for_period(brand_source, fallback_period)
    target_year = resolved_year(market_rows, brand_rows, plan=plan)
    brand_sum = sum(num(row.get("value_krw")) or 0.0 for row in brand_rows)
    market_sum = sum(num(row.get("value_krw")) or 0.0 for row in market_rows)
    ms_pct = round(brand_sum / market_sum * 100, 4) if market_sum else None
    label = fallback_period or (relative.label if relative is not None else period_label(market_rows, brand_rows, plan, target_year))
    transparency = _transparency_fields(
        plan,
        resolved_year=target_year,
        latest_period=latest_period_label(market_source, brand_source),
        extra_applied=relative.applied_filters if relative is not None else None,
        interpretation_notes=relative.interpretation_notes if relative is not None else (),
        data_basis=relative.data_basis if relative is not None else None,
    )
    blocked_values = _blocked_metric_values(requested_period, fallback_period)
    return {
        "source": "cache",
        "tool": "get_brand_metric",
        "summary_text": (
            f"{brand}의 {key.source} {_period_summary_label(label, fallback_period)} {metric}는 {format_krw(brand_sum)}입니다. "
            f"동기간 시장규모는 {format_krw(market_sum)}, MS {format_pct(ms_pct)}입니다."
        ),
        **transparency,
        "render_data": {
            "brand": brand,
            "metric": metric,
            "period": label,
            **({"requested_period": requested_period, "fallback_period": fallback_period} if fallback_period else {}),
            "market_id": key.market_id,
            "source_label": key.source,
            "measure": key.measure,
            "sales_krw": brand_sum,
            "sales_억원": krw_to_eok(brand_sum),
            "market_size_filtered_krw": market_sum,
            "market_size_recent_krw": market_sum,
            "market_size_억원": krw_to_eok(market_sum),
            "ms_recent_pct": ms_pct,
            "market_size_series": market_rows,
            "brand_value_series_10pt": brand_rows,
            **({"blocked_metric_values": blocked_values} if blocked_values else {}),
            **transparency,
        },
    }


def _requested_period(plan: MetricFilterPlan, selected: tuple[str, ...]) -> str:
    if plan.period_month:
        return plan.period_month
    if len(selected) == 1:
        return selected[0]
    return ""


def _latest_previous_period(row_group_a: list[dict[str, Any]], row_group_b: list[dict[str, Any]], requested_period: str) -> str:
    if not requested_period:
        return ""
    periods = sorted({str(row.get("period")) for rows in (row_group_a, row_group_b) for row in rows if row.get("period")})
    previous = [period for period in periods if period < requested_period]
    return previous[-1] if previous else ""


def _rows_for_period(rows: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("period") == period]


def _blocked_metric_values(requested_period: str, fallback_period: str) -> list[dict[str, str]]:
    if not requested_period or not fallback_period or requested_period == fallback_period:
        return []
    return [
        {
            "period": requested_period,
            "status": "query_failed",
            "message": f"{requested_period} 값은 조회 실패/시장 매핑 불완전으로 표시하지 않습니다.",
        }
    ]


def _period_summary_label(label: str, fallback_period: str) -> str:
    if fallback_period:
        return f"사용 가능한 최신 기준 {label}"
    return label


def _channel_result(
    brand: str,
    metric: str,
    key: CausePayloadKey,
    data: dict[str, Any],
    plan: MetricFilterPlan,
) -> dict[str, Any]:
    channel = plan.channel or ""
    brand_block = _level_data(data, "Brand")
    value_row = _brand_row(_channel_rows(brand_block, "by_channel", channel), brand)
    ms_row = _brand_row(_channel_rows(brand_block, "ms_by_channel", channel), brand)
    sales = _first_number(value_row, ("value_recent", "raw_value", "sales_krw", "value_krw", "value"))
    ms_pct = _first_number(ms_row, ("ms_recent_pct", "recent_share_pct", "ms_pct", "share_pct"))
    if ms_pct is None:
        ms_pct = _first_number(value_row, ("ms_recent_pct", "recent_share_pct", "ms_pct", "share_pct"))
    if sales is None and ms_pct is None:
        return unsupported_metric(brand, metric, f"{channel} 채널의 {brand} cache 값이 없습니다.", plan)
    transparency = _transparency_fields(plan)
    return {
        "source": "cache",
        "tool": "get_brand_metric",
        "summary_text": f"{brand}의 {channel} 채널 MS는 {format_pct(ms_pct)}, 매출은 {format_krw(sales)}입니다.",
        **transparency,
        "render_data": {
            "brand": brand,
            "metric": metric,
            "period": "latest",
            "market_id": key.market_id,
            "source_label": key.source,
            "channel": channel,
            "sales_krw": sales,
            "sales_억원": krw_to_eok(sales),
            "ms_recent_pct": ms_pct,
            **transparency,
        },
    }


def _level_result(
    brand: str,
    metric: str,
    key: CausePayloadKey,
    data: dict[str, Any],
    plan: MetricFilterPlan,
) -> dict[str, Any]:
    level = plan.level or ""
    block = _level_data(data, level)
    segments = _segments(block)
    if not segments:
        return unsupported_metric(brand, metric, f"{level}별로 표시할 현재 데이터가 없습니다.", plan)
    transparency = _transparency_fields(plan)
    return {
        "source": "cache",
        "tool": "get_brand_metric",
        "summary_text": f"{brand} 시장의 {level}별 점유율 구간 {len(segments)}개를 현재 데이터에서 확인했습니다.",
        **transparency,
        "render_data": {
            "brand": brand,
            "metric": metric,
            "period": "latest",
            "market_id": key.market_id,
            "source_label": key.source,
            "level": level,
            "level_segments": segments,
            **transparency,
        },
    }


def _transparency_fields(
    plan: MetricFilterPlan,
    *,
    resolved_year: int | None = None,
    latest_period: str = "",
    extra_applied: dict[str, Any] | None = None,
    interpretation_notes: tuple[dict[str, str], ...] = (),
    data_basis: dict[str, str] | None = None,
) -> dict[str, Any]:
    unsupported = [item.to_dict() for item in plan.unsupported]
    applied = plan.applied_filters(resolved_year=resolved_year)
    if extra_applied:
        applied.update(extra_applied)
    basis = {
        "source": "mart_direct",
        "view_type": "market_landscape",
        "period_grain": "cache period",
        "latest_period": latest_period or "-",
    }
    if data_basis:
        basis.update(data_basis)
    return {
        "applied_filters": applied,
        "unsupported_filters": unsupported,
        "unsupported": unsupported,
        "interpretation_notes": list(interpretation_notes),
        "unparsed_constraints": [],
        "data_basis": basis,
    }


def _payload_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _period_keys(*row_groups: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(str(row.get("period")) for rows in row_groups for row in rows if row.get("period"))


def _level_data(data: dict[str, Any], level: str) -> dict[str, Any]:
    levels = (data.get("analysis_levels") or {}).get("data") if isinstance(data.get("analysis_levels"), dict) else None
    if isinstance(levels, dict) and isinstance(levels.get(level), dict):
        return levels[level]
    clone = (data.get("analysis_level_market_status") or {}).get("data") if isinstance(data.get("analysis_level_market_status"), dict) else None
    return clone.get(level) if isinstance(clone, dict) and isinstance(clone.get(level), dict) else {}


def _channel_rows(block: dict[str, Any], key: str, channel: str) -> list[Any]:
    rows = block.get(key, {}).get(channel) if isinstance(block.get(key), dict) else None
    return rows if isinstance(rows, list) else []


def _segments(block: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _segment_rows(block)
    segments: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name") or row.get("label") or row.get("brand")
        if not name or name == "전체" or row.get("is_overall") is True:
            continue
        ms_pct = _first_number(row, ("ms_recent_pct", "recent_share_pct", "ms_pct", "share_pct", "value_pct"))
        value = _first_number(row, ("value_recent", "raw_value", "sales_krw", "value_krw"))
        if value is None and ms_pct is None:
            value = num(row.get("value"))
        if ms_pct is None:
            ms_pct = num(row.get("value"))
        segments.append({"name": name, "rank": _rank_number(row), "value": value, "ms_recent_pct": ms_pct})
    return sorted(segments, key=_segment_sort_key)


def _segment_rows(block: dict[str, Any]) -> list[Any]:
    candidates = (
        block.get("ms_segments"),
        _channel_rows(block, "ms_by_channel", "전체"),
        _channel_rows(block, "by_channel", "전체"),
        block.get("segments"),
    )
    for rows in candidates:
        if isinstance(rows, list) and any(isinstance(row, dict) for row in rows):
            return rows
    return []


def _first_number(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = num(row.get(key))
        if value is not None:
            return value
    return None


def _rank_number(row: dict[str, Any]) -> int | float | None:
    value = _first_number(row, ("rank",))
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _segment_sort_key(segment: dict[str, Any]) -> tuple[float, float]:
    rank = segment.get("rank")
    if isinstance(rank, int | float):
        return (float(rank), 0.0)
    ms_pct = segment.get("ms_recent_pct")
    return (float("inf"), -float(ms_pct) if isinstance(ms_pct, int | float) else 0.0)


def _brand_row(rows: list[Any], brand: str) -> dict[str, Any]:
    for row in rows:
        if isinstance(row, dict) and (row.get("brand") == brand or row.get("name") == brand):
            return row
    return {}
