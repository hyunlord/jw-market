from __future__ import annotations

from typing import Any, Mapping


def metric_name(metric: str) -> str:
    if metric in {"share", "market_share", "rank"}:
        return "market_share"
    if metric in {"trend", "series"}:
        return "series"
    return metric


def source_label(source: str) -> str:
    return "IQVIA" if str(source).lower().startswith("iqvia") else "UBIST"


def metric_summary(brand: str, data: Mapping[str, Any], label: str) -> str:
    return (
        f"{brand} {data.get('period')} {label} 전략 mart 지표: "
        f"매출 {float(data.get('sales_억원') or 0):,.2f}억원, "
        f"MS {float(data.get('ms_recent_pct') or 0):.2f}%, 순위 {data.get('rank')}위."
    )


def level_segments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        name = row.get("name") or row.get("brand")
        value = float(row.get("value") or 0.0)
        out.append(
            {
                "name": name,
                "brand": name,
                "rank": row.get("rank"),
                "ms_recent_pct": row.get("ms_recent_pct"),
                "value": value,
                "value_억원": round(value / 100_000_000, 2),
            }
        )
    return out


def result_rows_from_render_data(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in data.get("level_segments", []):
        if isinstance(item, dict):
            rows.append(dict(item))
    return rows
