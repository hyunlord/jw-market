from __future__ import annotations

from typing import Any

from jw_chat_agent_poc.orchestrator.markdown_formatting import (
    TABLE_LIMIT,
    cell,
    eok_value,
    items,
    latest_series_eok,
    number_value,
    pct_value,
    rank_value,
    table,
)
from jw_chat_agent_poc.orchestrator.markdown_metric_helpers import metric_filter_rows, metric_level_segments_md
from jw_chat_agent_poc.orchestrator.markdown_news import news_md


def call_data_md(call: dict[str, Any]) -> str:
    tool = str(call.get("tool") or "")
    render_data = call.get("render_data")
    if not isinstance(render_data, dict):
        return ""
    if tool == "deep_analysis_related_news":
        return news_md(render_data)
    if tool in {"get_brand_metric", "get_market_landscape", "unsupported_metric", "agent_calculation"}:
        return metrics_md(tool, render_data)
    if tool.startswith("hira_disease") or str(call.get("source")) == "hira_disease":
        return hira_md(tool, render_data)
    if "clinical" in tool:
        return clinical_md(render_data)
    if "patent" in tool or "orangebook" in tool:
        return patent_md(render_data)
    if tool.startswith("external_api"):
        return generic_external_md(tool, render_data)
    if tool == "document_rag":
        return document_md(render_data)
    return generic_external_md(tool, render_data)


def metrics_md(tool: str, data: dict[str, Any]) -> str:
    if data.get("status") == "unsupported":
        blocks = [table(f"### {cell(tool)}", ("지표", "값"), (("상태", data.get("message")),))]
        filter_rows = metric_filter_rows(data)
        if filter_rows:
            blocks.append(table("### 지표 필터", ("구분", "값"), filter_rows))
        return "\n\n".join(blocks)
    rows: list[tuple[str, Any]] = []
    scope_label = data.get("scope_label")
    if scope_label:
        rows.append(("범위", scope_label))
    view_label = data.get("view_label")
    if view_label:
        rows.append(("시장 기준", view_label))
    anchor_brand = data.get("anchor_brand")
    if anchor_brand:
        rows.append(("기준 브랜드", anchor_brand))
    period = data.get("period")
    if period:
        rows.append(("기간", period))
    sales = eok_value(data.get("sales_억원"), data.get("sales_krw"))
    if sales:
        rows.append(("매출", sales))
    market_share = pct_value(data.get("ms_recent_pct", data.get("market_share")))
    if market_share:
        rows.append(("시장점유율", market_share))
    rank = rank_value(data.get("rank"), data.get("total_brands_in_market"))
    if rank:
        rows.append(("순위", rank))
    market_size = eok_value(data.get("market_size_억원"), data.get("market_size_recent_krw")) or latest_series_eok(data.get("series"))
    if market_size:
        rows.append(("시장규모", market_size))
    rows.extend(_metric_scalar_rows(data))
    blocks = [table("### 지표", ("지표", "값"), tuple(rows))] if rows else []
    series_table = series_md(data)
    if series_table:
        blocks.append(series_table)
    level_table = metric_level_segments_md(data)
    if level_table:
        blocks.append(level_table)
    filter_rows = metric_filter_rows(data)
    if filter_rows:
        blocks.append(table("### 지표 필터", ("구분", "값"), filter_rows))
    return "\n\n".join(blocks)


def _metric_scalar_rows(data: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    specs = (
        ("브랜드 CAGR", "brand_cagr_5y_pct", "pct"),
        ("시장 CAGR", "market_cagr_5y_pct", "pct"),
        ("Excess growth", "excess_growth_pct", "pct"),
        ("HHI", "hhi_recent", "num2"),
        ("HHI", "hhi", "num2"),
        ("Momentum", "momentum_score", "num"),
        ("EI", "ei", "num"),
        ("기준 점유율", "from_ms_pct", "pct"),
        ("비교 점유율", "to_ms_pct", "pct"),
        ("점유율 변화", "ms_delta_pct", "pct"),
        ("기준 매출", "from_sales_krw", "eok"),
        ("비교 매출", "to_sales_krw", "eok"),
        ("매출 변화", "sales_delta_krw", "eok"),
        ("매출 변화율", "sales_delta_pct", "pct"),
    )
    for label, key, kind in specs:
        value = data.get(key)
        if value is None:
            continue
        if kind == "pct":
            rows.append((label, pct_value(value)))
        elif kind == "eok":
            rows.append((label, eok_value(None, value)))
        elif kind == "num2" and isinstance(value, int | float):
            rows.append((label, f"{float(value):,.2f}"))
        else:
            rows.append((label, number_value(value)))
    return rows


def series_md(data: dict[str, Any]) -> str:
    hhi_series = data.get("hhi_series_5y")
    if isinstance(hhi_series, list):
        rows = tuple(
            (item.get("period") or item.get("year"), number_value(item.get("hhi")))
            for item in hhi_series[:TABLE_LIMIT]
            if isinstance(item, dict)
        )
        return table("### HHI 추이", ("기간", "HHI"), rows)
    brand_series = data.get("brand_value_series_10pt")
    if isinstance(brand_series, list):
        rows = tuple(
            (item.get("period"), eok_value(item.get("value_억원"), item.get("value_krw")), pct_value(item.get("ms_pct")))
            for item in brand_series[-TABLE_LIMIT:]
            if isinstance(item, dict)
        )
        return table("### 브랜드 시계열", ("기간", "매출", "MS"), rows)
    market_series = data.get("market_size_series")
    if isinstance(market_series, list):
        rows = tuple(
            (item.get("period"), eok_value(item.get("value_억원"), item.get("value_krw")), pct_value(item.get("yoy_growth_pct")))
            for item in market_series[-TABLE_LIMIT:]
            if isinstance(item, dict)
        )
        return table("### 시장 시계열", ("기간", "시장규모", "YoY"), rows)
    return ""




def hira_md(tool: str, data: dict[str, Any]) -> str:
    if tool == "hira_disease_mapping":
        total = data.get("mapping_total")
        title = "### HIRA 질병 매핑"
        if isinstance(total, int) and total > 1:
            title = f"{title} {data.get('mapping_index')}/{total}"
        rows = (("대표 질병", data.get("disease_name")), ("KCD", data.get("sickCd")), ("근거", data.get("basis")))
        return table(title, ("구분", "값"), rows)
    if tool == "hira_disease_mapping_unsuitable":
        rows = (
            ("상태", "질병통계 부적합"),
            ("브랜드", data.get("brand")),
            ("사유", data.get("reason_label")),
            ("근거", data.get("basis")),
        )
        return table("### HIRA 질병 매핑", ("구분", "값"), rows)
    if tool == "hira_disease_mapping_unresolved":
        rows = (("상태", data.get("reason")), ("브랜드", data.get("brand")))
        return table("### HIRA 질병 매핑", ("구분", "값"), rows)
    rows = _hira_stat_rows(data)
    return table("### HIRA 질병통계", ("구분", "질병코드", "질병명", "환자수"), tuple(rows))


def _hira_stat_rows(data: dict[str, Any]) -> list[tuple[Any, Any, Any, Any]]:
    rows = [_hira_item_row(item) for item in items(data)]
    if rows:
        return rows
    calls = data.get("calls")
    if not isinstance(calls, list):
        return []
    nested_rows: list[tuple[Any, Any, Any, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        render_data = call.get("render_data")
        if not isinstance(render_data, dict):
            continue
        nested_rows.extend(_hira_item_row(item) for item in items(render_data))
    return nested_rows[:TABLE_LIMIT]


def _hira_item_row(item: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    label = item.get("inpatOpat") or item.get("age") or item.get("grade") or item.get("lcName") or item.get("sickEngNm")
    return (label, item.get("sickCd"), item.get("sickNm"), item.get("ptntCnt") or item.get("specCnt") or "-")


def clinical_md(data: dict[str, Any]) -> str:
    nct_ids = data.get("nct_ids")
    if isinstance(nct_ids, list):
        title = data.get("briefTitle")
        rows = tuple((nct, title if index == 0 else "-", "-") for index, nct in enumerate(nct_ids[:TABLE_LIMIT]))
        return table("### 임상시험", ("ID", "제목", "상태"), rows)
    rows = tuple(
        (
            item.get("NCTId") or item.get("CLNC_TEST_SN") or item.get("id") or "-",
            item.get("briefTitle") or item.get("GOODS_NAME") or item.get("title") or "-",
            item.get("overallStatus") or item.get("CLINIC_STEP_NAME") or "-",
        )
        for item in items(data)
    )
    return table("### 임상시험", ("ID", "제목", "상태"), rows)


def patent_md(data: dict[str, Any]) -> str:
    rows = tuple(
        (
            item.get("DOMESTIC_PATENT_NO") or item.get("KOR_PAT_NO") or "-",
            item.get("ITEM_NAME") or item.get("PRT_NAME") or item.get("INGR_NAME") or "-",
            item.get("DOMESTIC_END_DATE") or item.get("KOR_EXP_DATE") or "-",
        )
        for item in items(data)
    )
    return table("### 특허", ("특허번호", "대상", "만료일"), rows)


def generic_external_md(tool: str, data: dict[str, Any]) -> str:
    rows = []
    total = data.get("totalCount")
    if total is not None:
        rows.append(("totalCount", total))
    for key, value in list(data.items())[:TABLE_LIMIT]:
        if key not in {"items", "payload", "totalCount"} and isinstance(value, str | int | float):
            rows.append((key, value))
    return table(f"### {cell(tool)}", ("항목", "값"), tuple(rows))


def document_md(data: dict[str, Any]) -> str:
    chunks = data.get("chunks")
    if not isinstance(chunks, list):
        return ""
    rows = tuple((item.get("document"), item.get("quote")) for item in chunks[:TABLE_LIMIT] if isinstance(item, dict))
    return table("### 문서 근거", ("문서", "발췌"), rows)
