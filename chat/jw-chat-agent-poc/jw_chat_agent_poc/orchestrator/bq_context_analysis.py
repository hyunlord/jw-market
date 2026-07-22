from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Final

Call = dict[str, Any]
Builder = Callable[[list[Call]], Call | None]


def build_context_analysis_call(contract_id: str, calls: list[Call]) -> Call | None:
    builder = _BUILDERS.get(contract_id)
    return builder(calls) if builder is not None else None


def _distribution_analysis(calls: list[Call]) -> Call | None:
    distributions = {
        dimension: _distribution(calls, dimension)
        for dimension in ("channel", "specialty")
    }
    distributions = {key: value for key, value in distributions.items() if value}
    if not distributions:
        return None
    is_volume = any(_data(call).get("measure") == "volume" for call in calls)
    metric_label = "처방량" if is_volume else "매출"
    charts = [
        {
            "chart_type": "bar", "title": f"{dimension}별 {metric_label} 구성", "source": "UBIST",
            "scope": "MARKET", "unit": "%", "evidence_refs": [f"UBIST.{dimension}.level_segments"],
            "labels": list(rows),
            "datasets": [{"label": f"{dimension}별 비중(%)", "unit": "%", "data": list(rows.values())}],
        }
        for dimension, rows in distributions.items()
    ]
    largest = [f"{axis} {max(rows, key=rows.get)} {max(rows.values()):.2f}%" for axis, rows in distributions.items()]
    return _analysis(
        "C2", "axis_distribution", [" · ".join(largest)], distributions=distributions,
        axes_are_not_aggregated=True, chart_payloads=charts, source_labels=["UBIST"],
    )


def _activity_analysis(calls: list[Call]) -> Call | None:
    activity = next((call for call in calls if call.get("tool") == "csd_activity_trend"), None)
    rows = _series(activity)
    known = [(str(row.get("period") or ""), _decimal(row.get("product_details"))) for row in rows]
    known = [(period, value) for period, value in known if period and value is not None]
    if len(known) < 2:
        return None
    start, end = known[0], known[-1]
    delta = end[1] - start[1]
    rate = None if start[1] == 0 else delta / start[1]
    insight = f"{start[0]}~{end[0]} TOTAL CSD 활동은 {start[1]:,.0f}건에서 {end[1]:,.0f}건으로 {delta:,.0f}건({_pct(rate)}) 변했습니다."
    chart = {
        "chart_type": "line", "title": "CSD TOTAL 활동 추이", "source": "IQVIA CSD",
        "scope": "MARKET", "unit": "건", "evidence_refs": ["CSD.render_data.series"],
        "labels": [period for period, _ in known],
        "datasets": [{"label": "CSD 활동(건)", "unit": "건", "data": [float(value) for _, value in known]}],
    }
    return _analysis(
        "D1", "activity_trend", [insight], activity_delta=float(delta),
        activity_change_rate_pct=_percent(rate), region="TOTAL", market2_excluded=True,
        topic_status="unsupported_by_current_csd_tool", chart_payloads=[chart], source_labels=["CSD"],
    )


def _news_analysis(calls: list[Call]) -> Call | None:
    refs: list[dict[str, str]] = []
    for call in calls:
        data = _data(call)
        items = data.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            ref = {key: str(item.get(key) or "").strip() for key in ("title", "date", "source", "url")}
            if all(ref.values()):
                refs.append(ref)
    if not refs:
        return None
    insight = f"제목·날짜·매체·URL이 모두 확인된 관련 기사 {len(refs)}건만 분석 근거로 사용합니다."
    return _analysis("E1", "brand_relevance", [insight], news_refs=refs, source_labels=["NEWS"])


def _distribution(calls: list[Call], dimension: str) -> dict[str, float]:
    call = next((item for item in calls if _data(item).get("requested_dimension") == dimension), None)
    rows = _data(call).get("level_segments") if call else None
    if not isinstance(rows, list):
        return {}
    values = [(str(row.get("name") or ""), _decimal(row.get("value"))) for row in rows if isinstance(row, dict)]
    total = sum((value for _, value in values if value is not None), Decimal("0"))
    return {name: float(value / total * 100) for name, value in values if name and value is not None and total > 0}


def _series(call: Call | None) -> list[dict[str, Any]]:
    rows = _data(call).get("series")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _data(call: Call | None) -> dict[str, Any]:
    value = call.get("render_data") if isinstance(call, dict) else None
    return value if isinstance(value, dict) else {}


def _analysis(contract_id: str, calculation: str, insights: list[str], **data: Any) -> Call:
    return {"source": "BQ deterministic evidence", "tool": "bq_analysis", "summary_text": " ".join(insights), "render_data": {"contract_id": contract_id, "calculation": calculation, "insights": insights, **data}}


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _percent(value: Decimal | None) -> float | None:
    return None if value is None else float(value * 100)


def _pct(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


_BUILDERS: Final[dict[str, Builder]] = {"C2": _distribution_analysis, "D1": _activity_analysis, "E1": _news_analysis}
