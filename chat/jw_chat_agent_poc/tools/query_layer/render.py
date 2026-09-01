from __future__ import annotations

from typing import Any, Mapping


MISSING_VALUE_LABEL = "—"


def metric_name(metric: str) -> str:
    if metric in {"share", "market_share", "rank"}:
        return "market_share"
    if metric in {"trend", "series"}:
        return "series"
    return metric


def source_label(source: str) -> str:
    return "IQVIA" if str(source).lower().startswith("iqvia") else "UBIST"


def metric_summary(brand: str, data: Mapping[str, Any], label: str) -> str:
    if data.get("measure") == "volume" or data.get("metric") == "prescription_volume":
        value = data.get("prescription_volume", data.get("value"))
        rendered = f"{float(value):,.0f} Rx" if isinstance(value, int | float) else MISSING_VALUE_LABEL
        share = format_pct(data.get("ms_recent_pct"))
        rank = format_rank(data.get("rank"))
        return (
            f"{brand} {data.get('period')} {label} 전략 mart 지표: "
            f"처방량 {rendered}, 처방량 점유율 {share}, 순위 {rank}."
        )
    sales = format_eok(data.get("sales_억원"))
    share = format_pct(data.get("ms_recent_pct"))
    rank = format_rank(data.get("rank"))
    return (
        f"{brand} {data.get('period')} {label} 전략 mart 지표: "
        f"매출 {sales}, MS {share}, 순위 {rank}."
    )


def level_segments(rows: list[dict[str, Any]], *, measure: str = "sales") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        name = row.get("name") or row.get("brand")
        raw_value = row.get("value")
        value = float(raw_value) if isinstance(raw_value, int | float) else None
        item = {
                "name": name,
                "brand": name,
                "rank": row.get("rank"),
                "ms_recent_pct": row.get("ms_recent_pct"),
                "value": value,
                "measure": measure,
                "unit_label": "Rx" if measure == "volume" else "KRW",
                "value_label": "처방량" if measure == "volume" else "매출",
            }
        if measure == "sales":
            item["value_억원"] = round(value / 100_000_000, 2) if value is not None else None
        else:
            item["prescription_volume"] = value
        out.append(item)
    return out


def result_rows_from_render_data(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in data.get("level_segments", []):
        if isinstance(item, dict):
            rows.append(dict(item))
    return rows


def format_eok(value: Any) -> str:
    return f"{float(value):,.2f}억원" if isinstance(value, int | float) else MISSING_VALUE_LABEL


def format_pct(value: Any) -> str:
    return f"{float(value):.2f}%" if isinstance(value, int | float) else MISSING_VALUE_LABEL


def format_rank(value: Any) -> str:
    return f"{int(value)}위" if isinstance(value, int | float) else MISSING_VALUE_LABEL
