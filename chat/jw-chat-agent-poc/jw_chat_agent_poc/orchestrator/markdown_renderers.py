from __future__ import annotations

import re
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
from jw_chat_agent_poc.orchestrator.external_item_projection import public_external_rows
from jw_chat_agent_poc.orchestrator.markdown_metric_helpers import metric_filter_rows, metric_level_segments_md
from jw_chat_agent_poc.orchestrator.markdown_news import news_md
from jw_chat_agent_poc.orchestrator.surface_policy import can_surface_derived_value, cagr_operands_from_data, surface_year


def call_data_md(call: dict[str, Any]) -> str:
    tool = str(call.get("tool") or "")
    render_data = call.get("render_data")
    if not isinstance(render_data, dict):
        return ""
    if tool == "deep_analysis_related_news":
        return news_md(render_data)
    if tool == "portfolio_decline_analysis":
        return portfolio_decline_md(render_data)
    if tool == "csd_activity_trend":
        return csd_activity_md(render_data)
    if tool in {"get_brand_metric", "get_market_landscape", "unsupported_metric", "query_failed", "agent_calculation"}:
        return metrics_md(tool, render_data)
    if tool == "get_market_members":
        return market_members_md(render_data)
    if tool.startswith("hira_disease") or str(call.get("source")) == "hira_disease":
        return hira_md(tool, render_data)
    if tool.startswith("hira_procedure") or tool == "get_procedure_stats" or str(call.get("source")) == "hira_procedure":
        return hira_procedure_md(render_data)
    if tool == "web_search" or str(call.get("source")) == "web_search":
        return web_search_md(render_data)
    if "clinical" in tool:
        return clinical_md(render_data)
    if "patent" in tool or "orangebook" in tool:
        return patent_md(render_data)
    if tool == "search_drug_info":
        return drug_info_md(render_data)
    if tool.startswith("external_api"):
        return generic_external_md(tool, render_data)
    if tool == "document_rag":
        return document_md(render_data)
    return generic_external_md(tool, render_data)


def market_members_md(data: dict[str, Any]) -> str:
    members = data.get("member_brands")
    member_names = tuple(str(member) for member in members) if isinstance(members, (list, tuple)) else ()
    total = int(data.get("total_brands_in_market") or 0)
    displayed = int(data.get("displayed_brand_count") or len(member_names))
    other_members_only = bool(data.get("other_members_only"))
    has_other_total = "other_member_count" in data
    other_total = int(data.get("other_member_count") or 0)
    requested = data.get("requested_limit")
    if other_members_only and has_other_total:
        display_scope = f"기타 {other_total:,}개 중 {displayed:,}개 표시"
    elif isinstance(requested, int):
        all_returned = displayed == total and requested >= total
        display_scope = (
            f"전체 {total:,}개 · 요청 {requested:,}개 · 표시 {displayed:,}개"
            + (" (전체 제공)" if all_returned else "")
        )
    elif data.get("requested_all"):
        display_scope = f"전체 {total:,}개 · 전체 요청 · 표시 {displayed:,}개"
    else:
        display_scope = f"총 {total:,}개 중 {displayed:,}개 표시"
    overview_rows: list[tuple[str, Any]] = [
        ("시장", data.get("market_name") or data.get("market") or "-"),
        ("기준기간", data.get("period") or "-"),
        ("표시 범위", display_scope),
    ]
    other_share = data.get("other_total_share_pct")
    if other_members_only and isinstance(other_share, int | float):
        overview_rows.append(("기타 합계 점유율", f"{float(other_share):.2f}%"))
    overview = table(
        "### 시장 구성",
        ("항목", "내용"),
        tuple(overview_rows),
    )
    if not member_names:
        if other_members_only and has_other_total:
            return f"{overview}\n\n상위 5개 외 기타 브랜드가 없습니다."
        return overview
    start_rank = 6 if other_members_only else 1
    member_rows = tuple((start_rank + index, member) for index, member in enumerate(member_names))
    return "\n\n".join((overview, table("### 구성 브랜드", ("순위", "브랜드"), member_rows)))


def metrics_md(tool: str, data: dict[str, Any]) -> str:
    value_header = "수치(단위 포함)" if data.get("_semantic_value_header") else "값"
    is_volume = data.get("measure") == "volume"
    if data.get("status") in {"error", "query_failed"}:
        blocks = [table(f"### {cell(tool)}", ("지표", value_header), (("상태", data.get("message")),))]
        filter_rows = metric_filter_rows(data)
        if filter_rows:
            blocks.append(table("### 지표 필터", ("구분", "값"), filter_rows))
        return "\n\n".join(blocks)
    if data.get("status") == "unsupported":
        blocks = [table(f"### {cell(tool)}", ("지표", value_header), (("상태", data.get("message")),))]
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
        rows.append((_period_row_label(data), period))
    sales = eok_value(data.get("sales_억원"), data.get("sales_krw"))
    if sales:
        rows.append(("매출", sales))
    prescription_volume = number_value(data.get("prescription_volume"))
    if prescription_volume:
        rows.append(("처방량", f"{prescription_volume} Rx"))
    market_share = pct_value(data.get("ms_recent_pct", data.get("market_share")))
    if market_share:
        rows.append(("처방량 점유율" if is_volume else "시장점유율", market_share))
    rank = rank_value(data.get("rank"), data.get("total_brands_in_market"))
    if rank:
        rows.append(("순위", rank))
    rows.extend(_blocked_metric_rows(data))
    market_size = eok_value(data.get("market_size_억원"), data.get("market_size_recent_krw")) or latest_series_eok(data.get("series"))
    if market_size:
        rows.append(("시장규모", market_size))
    rows.extend(_metric_scalar_rows(data))
    if isinstance(data.get("series_insight"), dict):
        value_header = "수치(단위 포함)"
    blocks = [table("### 지표", ("지표", value_header), tuple(rows))] if rows else []
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


def csd_activity_md(data: dict[str, Any]) -> str:
    rows: list[tuple[str, Any]] = [
        ("출처", data.get("source_label") or "CSD ChannelDynamics"),
        ("범위", data.get("data_grain") or "월별 TOTAL 채널 aggregate 콜수/활동량"),
        ("시장", data.get("market") or ""),
        ("제품", data.get("master_product") or data.get("brand") or ""),
    ]
    unsupported = data.get("unsupported_fields")
    if isinstance(unsupported, (list, tuple)):
        rows.append(("미포함 필드", ", ".join(str(item) for item in unsupported if item)))
    blocks = [table("### CSD 영업활동 aggregate", ("항목", "값"), tuple(row for row in rows if row[1]))]
    series = data.get("series")
    if isinstance(series, list):
        series_rows = tuple(
            (item.get("period"), number_value(item.get("product_details")))
            for item in series[-TABLE_LIMIT:]
            if isinstance(item, dict)
        )
        if series_rows:
            blocks.append(table("### CSD 월별 aggregate 콜수/활동량", ("기간", "product_details"), series_rows))
    return "\n\n".join(blocks)


def _blocked_metric_rows(data: dict[str, Any]) -> list[tuple[str, str]]:
    blocked = data.get("blocked_metric_values")
    if not isinstance(blocked, list):
        return []
    rows: list[tuple[str, str]] = []
    for item in blocked:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if message:
            rows.append(("조회 차단", message))
    return rows


def _period_row_label(data: dict[str, Any]) -> str:
    requested_period = str(data.get("requested_period") or "").strip()
    fallback_period = str(data.get("fallback_period") or "").strip()
    period = str(data.get("period") or "").strip()
    if requested_period and fallback_period and fallback_period == period and requested_period != fallback_period:
        return "사용 가능한 최신 기준"
    return "기간"


def portfolio_decline_md(data: dict[str, Any]) -> str:
    rows: list[tuple[Any, Any, Any, Any, Any, Any, Any]] = []
    for item in data.get("decliners", [])[:TABLE_LIMIT]:
        if not isinstance(item, dict):
            continue
        rows.append(
            (
                item.get("brand"),
                item.get("market_name") or item.get("market_id"),
                _period_range(item),
                _pct_point_path(item),
                _pct_point_delta(item.get("share_delta_pctp")),
                eok_value(None, item.get("to_sales_krw")),
                _gainer_summary(item.get("top_gainers")),
            )
        )
    if not rows:
        return table(
            "### JW 주요 브랜드 점유율 하락 분석",
            ("상태", "근거"),
            (("하락 브랜드 미확인", data.get("interpretation_guardrail") or "확정 mart 기준으로 하락 브랜드가 확인되지 않았습니다."),),
        )
    blocks = [
        table(
            "### JW 주요 브랜드 점유율 하락 분석",
            ("브랜드", "시장", "기간", "MS 경로", "MS 변화", "최신 매출", "동시장 상승 후보"),
            tuple(rows),
        )
    ]
    guardrail = data.get("interpretation_guardrail")
    if guardrail:
        blocks.append(table("### 해석 범위", ("항목", "값"), (("주의", guardrail),)))
    return "\n\n".join(blocks)


def _period_range(item: dict[str, Any]) -> str:
    start = str(item.get("period_from") or "")
    end = str(item.get("period_to") or "")
    return f"{start}→{end}" if start and end else start or end


def _pct_point_path(item: dict[str, Any]) -> str:
    start = pct_value(item.get("from_ms_pct"))
    end = pct_value(item.get("to_ms_pct"))
    if start and end:
        return f"{start} → {end}"
    return end or start


def _pct_point_delta(value: Any) -> str:
    rendered = pct_value(value)
    return f"{rendered}p" if rendered else ""


def _gainer_summary(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value[:3]:
        if not isinstance(item, dict) or not item.get("brand"):
            continue
        delta = _pct_point_delta(item.get("share_delta_pctp"))
        rank = rank_value(item.get("rank"), None)
        rank_label = f"{rank}위 " if rank else ""
        parts.append(f"{rank_label}{item['brand']} {delta}".strip())
    return ", ".join(parts)


def _metric_scalar_rows(data: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    cagr_keys = {"brand_cagr_5y_pct", "market_cagr_5y_pct", "excess_growth_pct"}
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
        if key in cagr_keys and not can_surface_derived_value(value, cagr_operands=cagr_operands_from_data(data, key)):
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
        if data.get("measure") == "volume":
            rows = tuple(
                (
                    item.get("period"),
                    pct_value(item.get("ms_pct")) or "—",
                    number_value(item.get("value")),
                )
                for item in brand_series[-10:]
                if isinstance(item, dict)
            )
            return table(
                "### 브랜드 처방량 시계열",
                ("기간", "처방량 점유율(%)", "처방량(Rx)"),
                rows,
            )
        market_series = data.get("market_size_series")
        market_by_period = {
            item.get("period"): item
            for item in (market_series if isinstance(market_series, list) else [])
            if isinstance(item, dict) and item.get("period")
        }
        rows = tuple(
            (
                item.get("period"),
                pct_value(item.get("ms_pct")) or "—",
                eok_value(item.get("value_억원"), item.get("value_krw")) or "—",
                eok_value(
                    market_by_period.get(item.get("period"), {}).get("value_억원"),
                    market_by_period.get(item.get("period"), {}).get("value_krw"),
                )
                or "—",
            )
            for item in brand_series[-10:]
            if isinstance(item, dict)
        )
        return table(
            "### 브랜드 시계열",
            ("기간", "시장점유율(%)", "처방조제액(억원)", "시장규모(억원)"),
            rows,
        )
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
    if tool == "hira_disease_code_ambiguous":
        raw_candidates = data.get("candidates")
        candidates = raw_candidates if isinstance(raw_candidates, list) else []
        rows = tuple(
            (candidate.get("sickCd"), candidate.get("sickNm"))
            for candidate in candidates
            if isinstance(candidate, dict)
        )
        guidance = "어느 것으로 조회할까요? 후보의 상병코드나 설명을 알려주세요."
        if data.get("candidates_truncated") is True:
            total = data.get("candidate_total")
            guidance = (
                f"후보 {total}건 중 앞의 {len(rows)}건만 표시했습니다. "
                "조회할 정확한 상병코드를 알려주세요."
            )
        return (
            table("### HIRA 상병코드 후보", ("상병코드", "설명"), rows)
            + f"\n\n{guidance}\n\n정확한 상병코드로 다시 물어보실 수도 있습니다."
        )
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
    if rows:
        return table("### HIRA 질병통계", ("구분", "질병코드", "질병명", "기준연도", "환자수"), tuple(rows))
    if _has_unsurfaced_hira_patient_counts(data):
        return table("### HIRA 질병통계", ("상태",), (("기준기간 미확인으로 환자수 표시 보류",),))
    return table("### HIRA 질병통계", ("구분", "질병코드", "질병명", "기준연도", "환자수"), tuple(rows))


def _hira_stat_rows(data: dict[str, Any]) -> list[tuple[Any, Any, Any, Any, Any]]:
    rows = [_hira_item_row(item, data) for item in items(data)]
    visible = [row for row in rows if row is not None]
    if visible:
        return visible
    calls = data.get("calls")
    if not isinstance(calls, list):
        return []
    nested_rows: list[tuple[Any, Any, Any, Any, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        render_data = call.get("render_data")
        if not isinstance(render_data, dict):
            continue
        nested_rows.extend(row for item in items(render_data) if (row := _hira_item_row(item, render_data)) is not None)
    return nested_rows[:TABLE_LIMIT]


def _hira_item_row(item: dict[str, Any], data: dict[str, Any] | None = None) -> tuple[Any, Any, Any, Any, Any] | None:
    source = data or {}
    label = item.get("inpatOpat") or item.get("age") or item.get("grade") or item.get("lcName") or item.get("sickEngNm")
    patient_count = item.get("ptntCnt") or item.get("specCnt") or "-"
    year = surface_year(source, item)
    if not can_surface_derived_value(patient_count, required_period=year):
        return None
    return (label, item.get("sickCd"), item.get("sickNm"), year, patient_count)


def _has_unsurfaced_hira_patient_counts(data: dict[str, Any]) -> bool:
    for item in items(data):
        if item.get("ptntCnt") or item.get("specCnt"):
            return True
    calls = data.get("calls")
    if not isinstance(calls, list):
        return False
    for call in calls:
        render_data = call.get("render_data") if isinstance(call, dict) else None
        if isinstance(render_data, dict) and _has_unsurfaced_hira_patient_counts(render_data):
            return True
    return False


def hira_procedure_md(data: dict[str, Any]) -> str:
    rows = _hira_procedure_rows(data)
    if rows:
        return table(
            "### HIRA 진료행위통계",
            ("구분", "행위코드", "행위명", "기준연도", "환자수", "명세서", "총사용량"),
            tuple(rows[:TABLE_LIMIT]),
        )
    message = data.get("message") or _nested_status_message(data) or "HIRA 진료행위 통계 조회 결과 없음"
    return table("### HIRA 진료행위통계", ("상태",), ((message,),))


def _hira_procedure_rows(data: dict[str, Any]) -> list[tuple[Any, Any, Any, Any, Any, Any, Any]]:
    rows = [_hira_procedure_item_row(item, data) for item in items(data)]
    visible = [row for row in rows if row is not None]
    if visible:
        return visible
    calls = data.get("calls")
    if not isinstance(calls, list):
        return []
    nested_rows: list[tuple[Any, Any, Any, Any, Any, Any, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        render_data = call.get("render_data")
        if not isinstance(render_data, dict):
            continue
        nested_rows.extend(row for item in items(render_data) if (row := _hira_procedure_item_row(item, render_data)) is not None)
    return nested_rows[:TABLE_LIMIT]


def _hira_procedure_item_row(item: dict[str, Any], data: dict[str, Any] | None = None) -> tuple[Any, Any, Any, Any, Any, Any, Any] | None:
    source = data or {}
    label = (
        item.get("inpatOpat")
        or item.get("ipatOpat")
        or item.get("ipatOpatDgsTpCdNm")
        or item.get("sexCdNm")
        or item.get("ageCdNm")
        or item.get("diagCdNm")
        or item.get("ykihoCdNm")
        or item.get("sex")
        or item.get("age")
        or item.get("grade")
        or item.get("lcName")
        or item.get("locNm")
    )
    code = item.get("st5Cd") or item.get("ST5_CD") or item.get("itemCd") or item.get("mdlrtActCd") or _request_st5_cd(source)
    name = item.get("st5Nm") or item.get("st5CdNm") or item.get("ST5_NM") or item.get("itemNm") or item.get("mdlrtActNm") or item.get("korNm") or "-"
    patient_count = item.get("ptntCnt") or item.get("PTNT_CNT") or "-"
    spec_count = item.get("specCnt") or item.get("SPEC_CNT") or "-"
    use_qty = item.get("useQty") or item.get("USE_QTY") or item.get("totUseQty") or "-"
    year = surface_year(source, item)
    if not can_surface_derived_value(patient_count, required_period=year):
        return None
    return (label or "-", code or "-", name, year, patient_count, spec_count, use_qty)


def _request_st5_cd(data: dict[str, Any]) -> str:
    request = data.get("request")
    if isinstance(request, dict):
        return str(request.get("st5Cd") or "")
    return ""


def web_search_md(data: dict[str, Any]) -> str:
    rows = tuple(
        (
            item.get("title") or "-",
            item.get("url") or "-",
            item.get("snippet") or "-",
        )
        for item in _web_search_rows(data)
    )
    if rows:
        return table("### 웹 검색 결과(미검증)", ("제목", "URL", "스니펫"), rows)
    message = data.get("message") or _nested_status_message(data) or "웹 검색 결과 없음"
    return table("### 웹 검색 결과(미검증)", ("상태", "설명"), ((data.get("provider") or "web_search", message),))


def _web_search_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = list(items(data))
    if rows:
        return rows[:TABLE_LIMIT]
    calls = data.get("calls")
    if not isinstance(calls, list):
        return []
    nested_rows: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        render_data = call.get("render_data")
        if isinstance(render_data, dict):
            nested_rows.extend(items(render_data))
    return nested_rows[:TABLE_LIMIT]


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


def drug_info_md(data: dict[str, Any]) -> str:
    basic_rows = _drug_info_basic_rows(data)
    if basic_rows:
        blocks = [
            _safe_table(
                "### MFDS 허가정보",
                ("품목명", "업체", "허가일", "구분", "저장법", "유효기간"),
                tuple(basic_rows),
            )
        ]
        ingredient_rows = _drug_info_ingredient_rows(data)
        if ingredient_rows:
            blocks.append(
                _safe_table(
                    "### MFDS 성분 상세",
                    ("총량", "성분명", "분량", "단위", "규격", "비고"),
                    tuple(ingredient_rows),
                )
            )
        return "\n\n".join(blocks)
    message = data.get("message") or _nested_status_message(data) or "MFDS 허가정보 조회 실패, 근거 생성 안 함"
    return table("### MFDS 허가정보", ("상태",), ((message,),))


def _drug_info_basic_rows(data: dict[str, Any]) -> list[tuple[Any, Any, Any, Any, Any, Any]]:
    detail_items = _nested_items(data, "mfds_permission_detail")
    rows: list[tuple[Any, Any, Any, Any, Any, Any]] = []
    for item in detail_items[:TABLE_LIMIT]:
        rows.append(
            (
                _mfds_clean_value(item.get("ITEM_NAME") or "-"),
                _mfds_clean_value(item.get("ENTP_NAME") or item.get("ENTP_NM") or "-"),
                _format_mfds_date(item.get("ITEM_PERMIT_DATE") or item.get("PERMIT_DATE") or "-"),
                _mfds_clean_value(item.get("ETC_OTC_CODE") or item.get("PERMIT_KIND_NAME") or "-"),
                _mfds_clean_value(item.get("STORAGE_METHOD") or "-"),
                _mfds_clean_value(item.get("VALID_TERM") or "-"),
            )
        )
    return rows


def _drug_info_ingredient_rows(data: dict[str, Any]) -> list[tuple[Any, Any, Any, Any, Any, Any]]:
    rows: list[tuple[Any, Any, Any, Any, Any, Any]] = []
    for item in _nested_items(data, "mfds_permission_detail")[:TABLE_LIMIT]:
        material = (
            item.get("MATERIAL_NAME")
            or item.get("MAIN_INGR_ENG")
            or item.get("ITEM_INGR_NAME")
            or ""
        )
        rows.extend(_parse_mfds_ingredients(material))
    return rows


def _parse_mfds_ingredients(raw: Any) -> list[tuple[str, str, str, str, str, str]]:
    text = _mfds_clean_value(raw)
    if not text or text == "-":
        return []
    tokens = [
        token.strip()
        for token in re.split(r"\s*(?:\\+|\||\r?\n)\s*", str(raw))
        if token.strip()
    ]
    current: dict[str, str] = {}
    rows: list[tuple[str, str, str, str, str, str]] = []
    for token in tokens:
        key, separator, value = token.partition(":")
        if not separator:
            key, separator, value = token.partition("：")
        if not separator:
            continue
        normalized_key = _mfds_clean_value(key)
        normalized_value = _mfds_clean_value(value)
        if normalized_key == "총량" and current:
            rows.append(_ingredient_row(current))
            current = {}
        current[normalized_key] = normalized_value
    if current:
        rows.append(_ingredient_row(current))
    if rows:
        return rows
    return [("-", text, "-", "-", "-", "-")]


def _ingredient_row(parts: dict[str, str]) -> tuple[str, str, str, str, str, str]:
    return (
        parts.get("총량") or "-",
        parts.get("성분명") or parts.get("성분정보") or "-",
        parts.get("분량") or "-",
        parts.get("단위") or "-",
        parts.get("규격") or "-",
        parts.get("비고") or "-",
    )


def _format_mfds_date(raw: Any) -> str:
    value = _mfds_clean_value(raw)
    if re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def _mfds_clean_value(raw: Any) -> str:
    text = "-" if raw is None or raw == "" else str(raw)
    return re.sub(r"\s+", " ", text.replace("\\", " ").strip()) or "-"


def _safe_table(title: str, headers: tuple[str, ...], rows: tuple[tuple[Any, ...], ...]) -> str:
    if all(len(row) == len(headers) for row in rows):
        return table(title, headers, rows)
    fallback_rows = tuple(
        (
            f"{title.lstrip('# ').strip()} 행 {index}",
            " / ".join(_mfds_clean_value(value) for value in row),
        )
        for index, row in enumerate(rows, 1)
    )
    return table(title, ("항목", "값"), fallback_rows)


def _nested_items(data: dict[str, Any], tool_name: str) -> list[dict[str, Any]]:
    calls = data.get("calls")
    if not isinstance(calls, list):
        return []
    nested: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict) or call.get("tool") != tool_name or call.get("status") in {"error", "no_data"}:
            continue
        render_data = call.get("render_data")
        if not isinstance(render_data, dict):
            continue
        nested.extend(items(render_data))
    return nested


def _nested_status_message(data: dict[str, Any]) -> str:
    calls = data.get("calls")
    if not isinstance(calls, list):
        return ""
    for call in calls:
        if isinstance(call, dict) and call.get("status") in {"error", "no_data"}:
            render_data = call.get("render_data")
            if isinstance(render_data, dict):
                message = render_data.get("message")
                if message:
                    return str(message)
            summary = call.get("summary_text")
            if summary:
                return str(summary)
    return ""


def generic_external_md(tool: str, data: dict[str, Any]) -> str:
    rows = public_external_rows(data, limit=TABLE_LIMIT)
    return table(f"### {cell(tool)}", ("항목", "내용"), rows)


def document_md(data: dict[str, Any]) -> str:
    chunks = data.get("chunks")
    if not isinstance(chunks, list):
        return ""
    rows = tuple((item.get("document"), item.get("quote")) for item in chunks[:TABLE_LIMIT] if isinstance(item, dict))
    return table("### 문서 근거", ("문서", "발췌"), rows)
