from __future__ import annotations

from decimal import Decimal
from typing import Any

Call = dict[str, Any]


def aligned_endpoints(
    call: Call,
) -> tuple[str, str, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal] | None:
    brand = {str(row.get("period")): row for row in brand_rows(call) if row.get("period")}
    market = {str(row.get("period")): row for row in market_rows(call) if row.get("period")}
    aligned = [period for period in brand if period in market]
    valid = [
        period
        for period in aligned
        if all(
            decimal_value(value) is not None
            for value in (
                brand[period].get("value_krw"),
                market[period].get("value_krw"),
                brand[period].get("ms_pct"),
            )
        )
    ]
    if len(valid) < 2:
        return None
    first, last = valid[0], valid[-1]
    values = tuple(
        decimal_value(value)
        for value in (
            brand[first]["value_krw"],
            brand[last]["value_krw"],
            market[first]["value_krw"],
            market[last]["value_krw"],
            brand[first]["ms_pct"],
            brand[last]["ms_pct"],
        )
    )
    if any(value is None for value in values):
        return None
    return first, last, *(value for value in values if value is not None)


def source_line_charts(calls: list[Call]) -> list[dict[str, Any]]:
    charts: list[dict[str, Any]] = []
    for call in source_series_calls(calls):
        rows = brand_rows(call)
        label = source_label(call)
        charts.append(
            {
                "chart_type": "line",
                "title": f"{label} 브랜드 매출 추이",
                "source": label,
                "scope": "MARKET",
                "unit": "KRW",
                "evidence_refs": [f"{label}.brand_value_series"],
                "labels": [row.get("period") for row in rows],
                "datasets": [
                    {
                        "label": f"{label} 매출(KRW)",
                        "unit": "KRW",
                        "data": [row.get("value_krw") for row in rows],
                    }
                ],
            }
        )
    return charts


def analysis(contract_id: str, calculation: str, insights: list[str], **data_values: Any) -> Call:
    return {
        "source": "BQ deterministic evidence",
        "tool": "bq_analysis",
        "summary_text": " ".join(insights),
        "render_data": {
            "contract_id": contract_id,
            "calculation": calculation,
            "insights": insights,
            **data_values,
        },
    }


def source_series_calls(calls: list[Call]) -> list[Call]:
    return [call for call in calls if source_key(call) in {"ubist", "iqvia_nsa"} and brand_rows(call)]


def top_call(calls: list[Call], source: str = "") -> Call | None:
    return next((call for call in top_calls(calls) if not source or source_key(call) == source), None)


def top_calls(calls: list[Call]) -> list[Call]:
    return [call for call in calls if data(call).get("metric") == "market_top_brands"]


def top_trend(calls: list[Call], source: str = "") -> list[dict[str, Any]]:
    return top_trend_from_call(top_call(calls, source))


def top_trend_from_call(top: Call | None) -> list[dict[str, Any]]:
    rows = data(top).get("level_top5_trend_series") if top else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def segments(call: Call) -> list[dict[str, Any]]:
    rows = data(call).get("level_segments")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def distribution(calls: list[Call], dimension: str) -> dict[str, float]:
    call = next((item for item in calls if data(item).get("requested_dimension") == dimension), None)
    rows = segments(call) if call else []
    values = [(str(row.get("name") or ""), decimal_value(row.get("value"))) for row in rows]
    total = sum((value for _, value in values if value is not None), Decimal("0"))
    return {
        name: float(value / total * 100)
        for name, value in values
        if name and value is not None and total > 0
    }


def brand_rows(call: Call) -> list[dict[str, Any]]:
    rows = data(call).get("brand_value_series_10pt")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def market_rows(call: Call) -> list[dict[str, Any]]:
    rows = data(call).get("market_size_series")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def data(call: Call | None) -> dict[str, Any]:
    value = call.get("render_data") if isinstance(call, dict) else None
    return value if isinstance(value, dict) else {}


def source_key(call: Call) -> str:
    spec = data(call).get("query_spec")
    return str(spec.get("source") or "") if isinstance(spec, dict) else ""


def source_label(call: Call) -> str:
    return "IQVIA NSA" if source_key(call) == "iqvia_nsa" else "UBIST"


def decimal_value(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def rate(start: Decimal, end: Decimal) -> Decimal | None:
    return None if start == 0 else end / start - Decimal("1")


def year_span(start: str, end: str) -> Decimal:
    try:
        start_year, start_month = (int(value) for value in start[:7].split("-"))
        end_year, end_month = (int(value) for value in end[:7].split("-"))
    except (TypeError, ValueError):
        return Decimal("1")
    return Decimal((end_year - start_year) * 12 + end_month - start_month) / Decimal("12")


def to_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def percent(value: Decimal | None) -> float | None:
    return None if value is None else float(value * 100)


def percentage_text(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def percentage_point_text(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%p"


def decimal_text(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:.3f}"
