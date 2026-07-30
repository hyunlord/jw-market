from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Final

from jw_chat_agent_poc.orchestrator.markdown_formatting import (
    eok_value,
    precise_eok_value,
    rank_value,
)
from jw_chat_agent_poc.orchestrator.provenance_calls import provenance_rows_from_calls
from jw_chat_agent_poc.orchestrator.provenance_model import (
    MISSING_LABEL,
    ProvenanceRow,
    public_value,
    render_provenance_table,
)


_INTERNAL_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9_])(?:ml|cd|strategy|competitive)_\d+(?![A-Za-z0-9_])"
    r"|\b(?:market_landscape|competitive_dynamics|nedrug_mcp|query_spec|tool_call_\d+)\b",
    re.IGNORECASE,
)
_SOURCE_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"(?m)^##\s+출처\s*$")
_ANSWER_MARKET_RE: Final[re.Pattern[str]] = re.compile(r"(?m)^- 시장:\s*(?P<market>.+?)\s*$")
_STRATEGIC_VIEW_MARKET_RE: Final[re.Pattern[str]] = re.compile(
    r"^전략뷰\s*\([^)]+\)\s*·\s*(?P<market>.+?)\s*$"
)
_YEAR_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_RANK_REQUEST_RE: Final[re.Pattern[str]] = re.compile(
    r"순위|몇\s*위|랭킹|rank",
    re.IGNORECASE,
)
_CAUSAL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<claim>[^\n.!?]*(?:때문|이므로|따라서)[^\n.!?]*[.!?]?)"
)
_STRATEGY_MARKET_SIZE_GOLDENS: Final[dict[tuple[str, str], tuple[Decimal, int | None]]] = {
    ("ml_006", "2025-04"): (Decimal("2106.71557456"), None),
}


def is_actionable_upstream_guidance(answer: str) -> bool:
    """Return whether an upstream failure includes a concrete official lookup path."""

    return "검색어:" in answer and (
        "직접 확인:" in answer or "공식 확인 경로" in answer
    )


def enforce_market_answer_contract(
    question: str,
    answer: str,
    tool_calls: Sequence[Mapping[str, Any]],
) -> str:
    """Validate market answers against requested slots and structured tool facts."""

    calls = tuple(call for call in tool_calls if isinstance(call, Mapping))
    if calls and all(_is_file_tool(call) for call in calls):
        return answer
    relevant_calls = _calls_matching_question(question, calls)
    successful_relevant_calls = tuple(call for call in relevant_calls if not _call_failed(call))
    contract_calls = successful_relevant_calls or relevant_calls
    postcheck_answer = _strategy_market_size_postcheck(question, relevant_calls)
    if postcheck_answer:
        return _public_language(question, postcheck_answer)
    preserves_identity_mismatch = any(
        str(_render_data(call).get("error_code") or "").upper() == "IDENTITY_MISMATCH"
        for call in relevant_calls or calls
    ) and "제품 또는 성분 구성이 요청한 브랜드와 일치하지 않아" in answer
    preserves_upstream_guidance = any(
        str(_render_data(call).get("error_code") or "").upper()
        == "UPSTREAM_UNAVAILABLE"
        for call in relevant_calls or calls
    ) and is_actionable_upstream_guidance(answer)
    status_answer = (
        ""
        if preserves_identity_mismatch or preserves_upstream_guidance
        else _status_answer(question, relevant_calls or calls)
    )
    contracted = status_answer
    unresolved_answer = ""
    if not contracted:
        unresolved_answer = _unresolved_entity_answer(question, answer, calls)
        contracted = unresolved_answer
    if not contracted:
        contracted = _restrained_interpretation_answer(question, contract_calls)
    if not contracted:
        contracted = _strategy_market_answer(question, contract_calls)
    if not contracted:
        contracted = _brand_market_size_answer(question, contract_calls)
    if not contracted:
        contracted = _concentration_answer(question, contract_calls)
    if not contracted:
        contracted = _channel_ranking_answer(question, contract_calls)
    if not contracted:
        contracted = _top_brand_volume_answer(question, contract_calls)
    if not contracted:
        contracted = _dimension_answer(question, contract_calls)
    if not contracted:
        contracted = _brand_comparison_answer(question, contract_calls)
    if not contracted:
        contracted = _historical_brand_metric_answer(question, contract_calls)
    if not contracted:
        contracted = _hira_trend_answer(question, contract_calls)
    if not contracted:
        contracted = _trend_answer(question, contract_calls)
    if not contracted:
        contracted = answer
    contracted = _public_language(question, contracted)
    return _replace_provenance(
        question,
        contracted,
        contract_calls,
        status_only=bool(status_answer or unresolved_answer),
    )


def market_ambiguity_message(brand: str, markets: Sequence[str]) -> str:
    """Render the shared fail-closed response for a 1:N market membership."""

    labels = "·".join(dict.fromkeys(str(market) for market in markets if market))
    return f"{brand}는 {labels} 여러 시장에 속합니다. 어느 시장 기준으로 볼지 지정해 주세요."


def market_membership_mismatch_message(
    brand: str,
    requested_market: str,
    markets: Sequence[str],
) -> str:
    """Render the fail-closed response for an explicit brand-market mismatch."""

    labels = "·".join(dict.fromkeys(str(market) for market in markets if market))
    available = f" 확인된 소속 시장: {labels}." if labels else ""
    return (
        f"{brand}는 요청한 {requested_market}에 포함되지 않습니다."
        f"{available} 브랜드 또는 시장을 확인해 주세요."
    )


def render_same_market_sales_answer(tool_calls: Sequence[Mapping[str, Any]]) -> str:
    """Render a verified same-market total from structured market-scope evidence."""

    return _brand_market_size_from_calls(
        tuple(call for call in tool_calls if isinstance(call, Mapping))
    )


def _strategy_market_size_postcheck(
    question: str,
    calls: Sequence[Mapping[str, Any]],
) -> str:
    market_match = re.search(r"\b(ml_\d+)\b", question, re.IGNORECASE)
    period_match = re.search(r"\b(20\d{2}-\d{2})\b", question)
    if market_match is None or period_match is None or not re.search(r"시장\s*규모", question):
        return ""
    key = (market_match.group(1).lower(), period_match.group(1))
    approved = _STRATEGY_MARKET_SIZE_GOLDENS.get(key)
    if approved is None:
        return ""
    expected_amount, expected_denominator = approved
    for call in calls:
        data = _render_data(call)
        if not any(field in data for field in ("market_size_억원", "market_size_recent_krw")):
            continue
        market_id = str(data.get("market_id") or data.get("market") or "").lower()
        period = str(data.get("period") or data.get("requested_period") or "")
        if market_id != key[0] or period != key[1]:
            continue
        amount = _decimal(data.get("market_size_억원"))
        if amount is None:
            amount = _krw_to_eok(data.get("market_size_recent_krw"))
        try:
            denominator = int(data.get("total_brands_in_market"))
        except (TypeError, ValueError):
            denominator = None
        valid = (
            amount is not None
            and abs(amount - expected_amount) <= Decimal("0.0000005")
            and denominator == expected_denominator
        )
        if valid:
            return ""
        return f"승인된 {key[1]} 전략 시장 기준값과 일치하지 않아 수치를 표시하지 않습니다."
    return ""


def _status_answer(question: str, calls: Sequence[Mapping[str, Any]]) -> str:
    compact = re.sub(r"\s+", " ", question).strip()
    if "지역" in compact and "재구매율" in compact:
        return "현재 DB는 지역별 재구매율을 지원하지 않습니다."
    if (
        "시장" in compact
        and any(token in compact for token in ("고지혈증", "이상지질혈증", "당뇨", "빈혈"))
        and not _has_grounded_market_evidence(calls)
    ):
        return "현재 지원되지 않는 시장 매핑입니다. 브랜드 또는 ATC4 시장을 지정해 주세요."
    if re.fullmatch(r"매출\s*(?:알려\s*줘|알려주세요)?[?.!]?", compact):
        return "브랜드·시장·기간을 지정해 주세요."

    statuses = {_call_status(call) for call in calls}
    failed = tuple(call for call in calls if _call_failed(call))
    successful = tuple(call for call in calls if not _call_failed(call) and _has_usable_call_data(call))
    if not successful:
        unresolved_market = next(
            (
                _render_data(call)
                for call in failed
                if str(_render_data(call).get("reason_code") or "").strip().casefold() == "market_unresolved"
                and str(_render_data(call).get("brand") or "").strip()
            ),
            None,
        )
        if unresolved_market is not None:
            brand = str(unresolved_market["brand"]).strip()
            return (
                f"{brand}는 현재 전략 시장 분류에 연결되어 있지 않아 경쟁 분석을 제공할 수 없습니다. "
                "다른 브랜드 또는 ATC4 시장을 지정해 주세요."
            )
    if failed and not successful and statuses & {"error", "query_failed", "timeout", "failed"}:
        return "데이터 존재 여부를 확인하지 못했습니다. 조회 오류입니다."
    if failed and not successful and statuses & {"not_found", "mapping_failed"}:
        return "브랜드 목록에서 일치 항목을 찾지 못했습니다."

    requested_years = [int(value) for value in _YEAR_RE.findall(compact)]
    if not requested_years:
        return ""
    available_to = _first_nested_value(calls, "available_to") or _latest_call_period(calls)
    if not available_to:
        return ""
    available_years = [int(value) for value in _YEAR_RE.findall(str(available_to))]
    if available_years and max(requested_years) > max(available_years):
        requested = max(requested_years)
        return f"보유 데이터는 {available_to}까지이며 {requested}년 실적은 없습니다."
    return ""


def _call_status(call: Mapping[str, Any]) -> str:
    return str(call.get("status") or _render_data(call).get("status") or "").strip().lower()


def _call_failed(call: Mapping[str, Any]) -> bool:
    return _call_status(call) in {
        "empty",
        "error",
        "failed",
        "mapping_failed",
        "missing",
        "no_data",
        "not_found",
        "query_failed",
        "timeout",
        "unsupported",
    }


def _has_usable_call_data(call: Mapping[str, Any]) -> bool:
    data = _render_data(call)
    if not data:
        return False
    return any(
        value not in (None, "", (), [], {})
        for key, value in data.items()
        if key not in {"status", "error", "message"}
    )


def _unresolved_entity_answer(
    question: str,
    answer: str,
    calls: Sequence[Mapping[str, Any]],
) -> str:
    if calls or "매출" not in question:
        return ""
    unavailable_markers = (
        "보유하고 있지",
        "지원 대상이 아니",
        "확인이 불가능",
        "존재하지 않아",
        "존재하지 않",
    )
    if any(marker in answer for marker in unavailable_markers):
        return "브랜드 목록에서 일치 항목을 찾지 못했습니다."
    return ""


def _restrained_interpretation_answer(
    question: str,
    calls: Sequence[Mapping[str, Any]],
) -> str:
    mode = next((token for token in ("왜", "원인", "전망", "위협") if token in question), "")
    if not mode:
        return ""
    observation = _brand_share_observation(question, calls)
    if not observation:
        return ""
    if mode in {"왜", "원인"}:
        limitation = (
            "이 변화의 직접 원인으로 확정할 수 없습니다. "
            "처방 전환, 환자 구성, 채널별 활동 자료를 추가 확인해야 합니다."
        )
    elif mode == "전망":
        limitation = (
            "현재 추세가 계속된다고 단정할 수 없습니다. 전망하려면 처방 전환, 신제품 출시, "
            "환자 구성과 채널별 활동 자료를 추가 확인해야 합니다."
        )
    else:
        limitation = (
            "점유율 변화는 경쟁 구도를 보여주는 관찰값이며 위협의 원인이나 지속성을 확정하지 않습니다. "
            "처방 전환, 신제품 출시와 채널별 활동 자료를 추가 확인해야 합니다."
        )
    return f"## 관찰\n{observation}\n\n## 가설과 한계\n{limitation}"


def _brand_share_observation(question: str, calls: Sequence[Mapping[str, Any]]) -> str:
    candidates: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
    for call in calls:
        data = _render_data(call)
        brand = str(data.get("brand") or "").strip()
        series = data.get("brand_value_series_10pt")
        if not brand or not isinstance(series, Sequence) or isinstance(series, str | bytes):
            continue
        points = tuple(item for item in series if isinstance(item, Mapping) and item.get("period"))
        if len(points) >= 2:
            candidates.append((brand, points[0], points[-1]))
    selected = next((item for item in candidates if item[0] in question), candidates[0] if candidates else None)
    if selected is None:
        return ""
    brand, start, latest = selected
    start_share = _decimal(start.get("ms_pct") or start.get("ms_recent_pct"))
    latest_share = _decimal(latest.get("ms_pct") or latest.get("ms_recent_pct"))
    if start_share is None or latest_share is None:
        return ""
    direction = "상승" if latest_share > start_share else "하락" if latest_share < start_share else "보합"
    return (
        f"{brand} 점유율은 {start['period']} {start_share:.2f}%에서 "
        f"{latest['period']} {latest_share:.2f}%로 {direction}했습니다."
    )


def _strategy_market_answer(question: str, calls: Sequence[Mapping[str, Any]]) -> str:
    if not re.search(r"\bml_\d+\b", question, re.IGNORECASE):
        return ""
    for call in calls:
        data = _render_data(call)
        market_id = str(data.get("market_id") or data.get("market") or "")
        view = str(data.get("view_type") or data.get("view") or "").lower()
        if not (market_id.lower().startswith("ml_") or "market_landscape" in view):
            continue
        value = data.get("market_size_억원")
        if value is None:
            value = _krw_to_eok(data.get("market_size_recent_krw"))
        amount = _decimal(value)
        if amount is None:
            continue
        name = str(data.get("market_name") or "해당 시장").strip()
        period = str(data.get("period") or data.get("requested_period") or "").strip()
        prefix = f"{period} " if period else ""
        return f"{prefix}{name}의 전략뷰 시장규모는 {amount:,.6f}억원입니다."
    return ""


def _brand_market_size_answer(question: str, calls: Sequence[Mapping[str, Any]]) -> str:
    if not re.search(r"시장\s*규모", question) or re.search(
        r"\bml_\d+\b", question, re.IGNORECASE
    ):
        return ""
    return _brand_market_size_from_calls(calls)


def _brand_market_size_from_calls(calls: Sequence[Mapping[str, Any]]) -> str:
    for call in calls:
        data = _render_data(call)
        view = str(data.get("view_type") or data.get("view") or "").lower()
        market_id = str(data.get("market_id") or data.get("market") or "").lower()
        if "market_landscape" not in view and not market_id.startswith("ml_"):
            continue
        brand = str(data.get("anchor_brand") or data.get("brand") or "").strip()
        amount = eok_value(
            data.get("market_size_억원"),
            data.get("market_size_recent_krw"),
        )
        if not brand or not amount:
            continue
        period = str(data.get("period") or data.get("requested_period") or "").strip()
        prefix = f"{period} " if period else ""
        return f"{prefix}{brand}가 속한 전략 시장의 시장규모는 {amount}입니다."
    return ""


def _concentration_answer(question: str, calls: Sequence[Mapping[str, Any]]) -> str:
    if not any(token in question for token in ("집중도", "HHI", "CR")):
        return ""
    hhi = _find_hhi(calls)
    shares = _top_shares(calls, 5)
    if hhi is None or len(shares) < 5:
        return ""
    cr5 = sum(shares, Decimal("0"))
    lines = [
        "## 시장 집중도\n"
        f"HHI {hhi:.2f}, CR5 {cr5:.2f}%입니다. "
        "두 지표는 동일한 최신 시장 범위의 원시 점유율로 계산했습니다."
    ]
    requested = _requested_top_count(question)
    if requested:
        ranked = _top_ranked_rows(calls, requested)
        if len(ranked) != requested:
            return ""
        lines.extend(
            (
                "",
                f"## 상위 {requested}개 브랜드",
                "| 순위 | 브랜드 | 점유율 |",
                "| --- | --- | --- |",
                *(f"| {rank}위 | {brand} | {share:.2f}% |" for rank, brand, share in ranked),
            )
        )
    return "\n".join(lines)


def _channel_ranking_answer(question: str, calls: Sequence[Mapping[str, Any]]) -> str:
    channel = _requested_channel(question)
    if not channel or not any(token in question for token in ("상위", "순위")):
        return ""
    for call in calls:
        data = _render_data(call)
        segments = data.get("level_segments")
        if not isinstance(segments, Sequence) or isinstance(segments, str | bytes):
            continue
        rows: list[str] = []
        for item in segments:
            if not isinstance(item, Mapping):
                continue
            rank = item.get("rank")
            brand = str(item.get("brand") or item.get("name") or "").strip()
            share = _decimal(item.get("ms_recent_pct"))
            if rank in (None, "") or not brand:
                continue
            share_text = f"{share:.2f}%" if share is not None else "해당 없음"
            rows.append(f"| {rank}위 | {brand} | {share_text} |")
        if rows:
            return "\n".join(
                (
                    f"## {channel} 채널 내 상위 브랜드",
                    "| 순위 | 브랜드 | 점유율 |",
                    "| --- | --- | --- |",
                    *rows,
                )
            )
    return ""


def _dimension_answer(question: str, calls: Sequence[Mapping[str, Any]]) -> str:
    dimension = "channel" if any(token in question for token in ("채널별", "채널 별")) else ""
    if "진료과" in question:
        dimension = "specialty"
    if not dimension:
        return ""
    label = "채널" if dimension == "channel" else "진료과"
    for call in calls:
        data = _render_data(call)
        if _call_dimension(call) != dimension:
            continue
        is_volume = _is_volume_data(data)
        segments = data.get("level_segments")
        if not isinstance(segments, Sequence) or isinstance(segments, str | bytes):
            continue
        rows: list[str] = []
        for item in segments:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or item.get("brand") or "").strip()
            if not name:
                continue
            value = _volume_value(item) if is_volume else _sales_value(item)
            share = _decimal(item.get("ms_recent_pct"))
            value_text = _format_volume_value(value) if is_volume else _format_eok_value(value)
            share_text = f"{share:.2f}%" if share is not None else "해당 없음"
            rows.append(f"| {name} | {value_text} | {share_text} |")
        if rows:
            value_header = "처방량(Rx)" if is_volume else "매출"
            share_header = "처방량 점유율" if is_volume else "점유율"
            return "\n".join(
                (
                    f"## {label}별 분포",
                    f"| {label} | {value_header} | {share_header} |",
                    "| --- | --- | --- |",
                    *rows,
                )
            )
    return ""


def _top_brand_volume_answer(question: str, calls: Sequence[Mapping[str, Any]]) -> str:
    if "처방량" not in question:
        return ""
    for call in calls:
        data = _render_data(call)
        if not _is_volume_data(data) or data.get("metric") != "market_top_brands":
            continue
        segments = data.get("level_segments")
        if not isinstance(segments, Sequence) or isinstance(segments, str | bytes):
            continue
        rows: list[str] = []
        for item in segments:
            if not isinstance(item, Mapping):
                continue
            rank = item.get("rank")
            brand = str(item.get("brand") or item.get("name") or "").strip()
            value = _volume_value(item)
            share = _decimal(item.get("ms_recent_pct"))
            if rank in (None, "") or not brand:
                continue
            rows.append(
                f"| {rank}위 | {brand} | {_format_volume_value(value)} | "
                f"{share:.2f}% |" if share is not None else
                f"| {rank}위 | {brand} | {_format_volume_value(value)} | 해당 없음 |"
            )
        if rows:
            return "\n".join(
                (
                    "## 상위 브랜드 처방량",
                    "| 순위 | 브랜드 | 처방량(Rx) | 처방량 점유율 |",
                    "| --- | --- | --- | --- |",
                    *rows,
                )
            )
    return ""


def _brand_comparison_answer(question: str, calls: Sequence[Mapping[str, Any]]) -> str:
    if "비교" not in question:
        return ""
    rank_requested = _RANK_REQUEST_RE.search(question) is not None
    rows: dict[str, tuple[Mapping[str, Any], Mapping[str, Any], Any]] = {}
    for call in calls:
        data = _render_data(call)
        brand = str(data.get("brand") or "").strip()
        series = data.get("brand_value_series_10pt")
        if not brand or brand in rows or not isinstance(series, Sequence) or isinstance(series, str | bytes):
            continue
        points = tuple(item for item in series if isinstance(item, Mapping) and item.get("period"))
        if len(points) >= 2:
            latest = points[-1]
            rank = data.get("rank")
            if rank in (None, ""):
                rank = latest.get("rank")
            rows[brand] = (points[0], latest, rank)
    requested = tuple(brand for brand in rows if brand in question)
    selected = requested if len(requested) >= 2 else tuple(rows)[:2]
    if len(selected) < 2:
        return ""
    rendered = [
        "## 브랜드 비교",
        (
            "| 브랜드 | 시작 점유율 | 최신 점유율 | 방향 | 시작 매출 | 최신 매출 | 최신 순위 |"
            if rank_requested
            else "| 브랜드 | 시작 점유율 | 최신 점유율 | 방향 | 시작 매출 | 최신 매출 |"
        ),
        (
            "| --- | --- | --- | --- | --- | --- | --- |"
            if rank_requested
            else "| --- | --- | --- | --- | --- | --- |"
        ),
    ]
    for brand in selected:
        start, latest, rank = rows[brand]
        start_share = _decimal(start.get("ms_pct") or start.get("ms_recent_pct"))
        latest_share = _decimal(latest.get("ms_pct") or latest.get("ms_recent_pct"))
        if start_share is None or latest_share is None:
            return ""
        direction = "상승" if latest_share > start_share else "하락" if latest_share < start_share else "보합"
        row = (
            "| "
            f"{brand} | {start['period']} {start_share:.2f}% | {latest['period']} {latest_share:.2f}% | {direction} | "
            f"{_sales_point(start)} | {_sales_point(latest)} |"
        )
        if rank_requested:
            rendered_rank = rank_value(rank, None)
            if not rendered_rank:
                return ""
            row = f"{row} {rendered_rank}위 |"
        rendered.append(row)
    return "\n".join(rendered)


def _historical_brand_metric_answer(question: str, calls: Sequence[Mapping[str, Any]]) -> str:
    if "분기" in question or re.search(r"20\d{2}-?Q[1-4]", question, flags=re.IGNORECASE):
        return ""
    if "매출" not in question or not re.search(r"20\d{2}(?:년|-)\s*(?:0?[1-9]|1[0-2])(?:월)?", question):
        return ""
    for call in calls:
        data = _render_data(call)
        brand = str(data.get("brand") or "").strip()
        period = str(data.get("period") or data.get("requested_period") or "").strip()
        if not brand or not period:
            continue
        value = precise_eok_value(
            data.get("sales_억원") or data.get("value_억원"),
            data.get("sales_krw") or data.get("value_krw") or data.get("value"),
        )
        if not value:
            continue
        return f"{period} {brand} 매출은 {value}입니다."
    return ""


def _hira_trend_answer(question: str, calls: Sequence[Mapping[str, Any]]) -> str:
    if "추이" not in question:
        return ""
    yearly: dict[str, Decimal] = {}
    disease = "해당 질환"
    for call in calls:
        if call.get("tool") != "hira_disease_hospitalization_outpatient_stats":
            continue
        data = _render_data(call)
        request = data.get("request")
        year = str(request.get("year") or "").strip() if isinstance(request, Mapping) else ""
        disease = str(data.get("mapping_disease_name") or disease)
        items = data.get("items")
        if not year or not isinstance(items, Sequence) or isinstance(items, str | bytes):
            continue
        outpatient = next(
            (
                _decimal(item.get("ptntCnt"))
                for item in items
                if isinstance(item, Mapping) and item.get("inpatOpat") == "외래"
            ),
            None,
        )
        if outpatient is not None:
            yearly[year] = outpatient
    if len(yearly) < 3:
        return ""
    lines = [
        f"## {disease} 외래 환자수 추이",
        "| 연도 | 환자수 |",
        "| --- | --- |",
        *(f"| {year} | {yearly[year]:,.0f}명 |" for year in sorted(yearly)),
    ]
    return "\n".join(lines)


def _trend_answer(question: str, calls: Sequence[Mapping[str, Any]]) -> str:
    if "추이" not in question and "처방량" not in question:
        return ""
    for call in calls:
        rows = _series_rows(call, question)
        if len(rows) < 3:
            continue
        subject = str(_render_data(call).get("brand") or "요청 지표")
        unit = _series_unit(call, rows)
        value_header = "처방량(Rx)" if unit == "Rx" else "매출" if unit in {"억원", "KRW"} else "값"
        rendered = [
            f"## {subject} 추이",
            f"| 기간 | {value_header} |",
            "| --- | --- |",
            *(f"| {period} | {_format_series_value(value, unit)} |" for period, value, _ in rows),
        ]
        return "\n".join(rendered)
    return ""


def _series_rows(call: Mapping[str, Any], question: str) -> tuple[tuple[str, Any, str], ...]:
    data = _render_data(call)
    rows: list[tuple[str, Any, str]] = []
    keys = _series_value_keys(question)
    for raw in _series_candidates(data):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            period = str(item.get("period") or item.get("year") or "").strip()
            key, value = _first_present_with_key(item, keys)
            if period and value not in (None, ""):
                rows.append((period, value, key))
        if len(rows) >= 3:
            break
    return tuple(dict.fromkeys(rows))


def _series_value_keys(question: str) -> tuple[str, ...]:
    sales = ("value_억원", "sales_억원", "sales_krw", "value_krw", "value")
    volume = ("prescription_volume", "value")
    share = ("ms_recent_pct", "ms_pct")
    if "처방량" in question:
        return (*volume, *share, "product_details", "ptntCnt", *sales)
    if "매출" in question:
        return (*sales, *share, "product_details", "ptntCnt")
    if "점유율" in question:
        return (*share, *sales, "product_details", "ptntCnt")
    return (*share, "product_details", "ptntCnt", *sales)


def _series_candidates(data: Mapping[str, Any]) -> tuple[Sequence[Any], ...]:
    candidates: list[Sequence[Any]] = []
    for key in ("series", "brand_value_series_10pt", "market_size_series", "items"):
        value = data.get(key)
        if isinstance(value, Sequence) and not isinstance(value, str | bytes):
            candidates.append(value)
    nested_calls = data.get("calls")
    if isinstance(nested_calls, Sequence) and not isinstance(nested_calls, str | bytes):
        for nested in nested_calls:
            if isinstance(nested, Mapping):
                candidates.extend(_series_candidates(_render_data(nested)))
    return tuple(candidates)


def _series_unit(call: Mapping[str, Any], rows: Sequence[tuple[str, Any, str]]) -> str:
    data = _render_data(call)
    selected_key = rows[0][2] if rows else ""
    explicit = str(data.get("unit") or data.get("unit_label") or "")
    if _is_volume_data(data) or selected_key == "prescription_volume":
        return explicit or "Rx"
    if selected_key in {"ms_recent_pct", "ms_pct"}:
        return "%"
    if selected_key == "ptntCnt":
        return "명"
    tool = str(call.get("tool") or "")
    if tool == "csd_activity_trend" or selected_key == "product_details":
        return "건"
    if selected_key in {"sales_krw", "value_krw"}:
        return "KRW"
    if selected_key in {"sales_억원", "value_억원", "value"}:
        return "억원"
    if explicit:
        return explicit
    metric = str(data.get("metric") or "").lower()
    if "share" in metric or any("ms_" in str(key) for key in data):
        return "%"
    if "patient" in metric or tool.startswith("hira_disease"):
        return "명"
    return "억원"


def _sales_point(point: Mapping[str, Any]) -> str:
    value = _decimal(point.get("value_억원"))
    if value is None:
        value = _krw_to_eok(point.get("value_krw") or point.get("value"))
    return f"{value:,.2f}억원" if value is not None else "해당 없음"


def _format_series_value(value: Any, unit: str) -> str:
    number = _decimal(value)
    if number is None:
        return str(value)
    if unit in {"건", "명"}:
        return f"{number:,.0f}{unit}"
    if unit == "Rx":
        return f"{_format_rx_decimal(number)} Rx"
    if unit == "%":
        return f"{number:.2f}%"
    if "KRW" in unit.upper() or abs(number) >= Decimal("100000000"):
        number /= Decimal("100000000")
    return f"{number:,.2f}억원"


def _is_volume_data(data: Mapping[str, Any]) -> bool:
    return (
        data.get("measure") == "volume"
        or data.get("unit_label") == "Rx"
        or data.get("value_label") == "처방량"
        or data.get("metric") == "prescription_volume"
    )


def _volume_value(item: Mapping[str, Any]) -> Decimal | None:
    value = item.get("prescription_volume")
    if value in (None, ""):
        value = item.get("value")
    return _decimal(value)


def _sales_value(item: Mapping[str, Any]) -> Decimal | None:
    value = _decimal(item.get("value_억원"))
    if value is None:
        value = _krw_to_eok(item.get("value") or item.get("value_krw"))
    return value


def _format_volume_value(value: Decimal | None) -> str:
    return f"{_format_rx_decimal(value)} Rx" if value is not None else "해당 없음"


def _format_eok_value(value: Decimal | None) -> str:
    return f"{value:,.2f}억원" if value is not None else "해당 없음"


def _format_rx_decimal(value: Decimal) -> str:
    if value == value.to_integral_value():
        return f"{value:,.0f}"
    return f"{value.normalize():f}"


def _public_language(question: str, answer: str) -> str:
    original = answer
    cleaned = _INTERNAL_LABEL_RE.sub("", answer)
    cleaned = re.sub(r"(?m)(전략뷰|일반뷰)\s*\(\s*\)", r"\1", cleaned)
    cleaned = _drop_unsupported_interpretation_lines(cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    asks_for_cause = any(token in question for token in ("왜", "원인"))
    if asks_for_cause and "## 가설과 한계" not in cleaned and (_CAUSAL_RE.search(original) or not cleaned.strip()):
        cleaned = (
            f"{cleaned.strip()}\n\n관찰된 수치만으로 원인으로 확정할 수 없습니다. "
            "가능성을 검토하려면 처방 전환, 환자 구성, 채널별 활동 자료를 추가 확인해야 합니다."
        ).strip()
    if any(token in question for token in ("왜", "원인", "전망", "위협")) and _CAUSAL_RE.search(cleaned):
        cleaned = _CAUSAL_RE.sub("", cleaned).strip()
        cleaned = (
            f"{cleaned}\n\n관찰된 수치만으로 원인으로 확정할 수 없습니다. "
            "가능성을 검토하려면 처방 전환, 환자 구성, 채널별 활동 자료를 추가 확인해야 합니다."
        ).strip()
    return cleaned


def _drop_unsupported_interpretation_lines(answer: str) -> str:
    unsupported = re.compile(
        r"(?:시장의?\s*중심.*이동|강력한 .*압력|시장 장악력|침투 수준|경쟁 방어 과제|"
        r"시장 재편의 방향|경쟁 압력의 근거|(?:때문|이므로|따라서).*(?:압력|하락|상승|이동))"
    )
    lines = [line for line in answer.splitlines() if not unsupported.search(line)]
    return "\n".join(lines)


def _replace_provenance(
    question: str,
    answer: str,
    calls: Sequence[Mapping[str, Any]],
    *,
    status_only: bool = False,
) -> str:
    if not calls and not status_only:
        return answer
    raw_rows = (
        (_status_provenance_row(question, answer, calls),)
        if status_only
        else _provenance_rows(calls)
    )
    completion_answer = (
        answer
        if _answer_market_applies_to_rows(raw_rows)
        else _ANSWER_MARKET_RE.sub("", answer)
    )
    rows = tuple(
        _complete_row(
            row,
            question=question,
            answer=completion_answer,
            unit=_requested_unit(question, answer),
        )
        for row in raw_rows
    )
    block = render_provenance_table("## 출처", rows).replace("## 출처\n|", "## 출처\n\n|")
    match = _SOURCE_HEADING_RE.search(answer)
    head = answer[: match.start()].rstrip() if match else answer.rstrip()
    return f"{head}\n\n{block}" if head else block


def _answer_market_applies_to_rows(rows: Sequence[ProvenanceRow]) -> bool:
    if len(rows) == 1:
        return True
    markets = tuple(_verified_row_market(row) for row in rows)
    return all(market is not None for market in markets) and len(set(markets)) == 1


def _verified_row_market(row: ProvenanceRow) -> str | None:
    if row.market not in (MISSING_LABEL, "해당 없음"):
        market = public_value(row.market)
        return market if market != MISSING_LABEL else None
    match = _STRATEGIC_VIEW_MARKET_RE.fullmatch(row.view)
    if match is None:
        return None
    market = public_value(match.group("market"))
    return market if market != MISSING_LABEL else None


def _status_provenance_row(
    question: str,
    answer: str,
    calls: Sequence[Mapping[str, Any]],
) -> ProvenanceRow:
    if "지역" in question and "재구매율" in question:
        return ProvenanceRow(source="지원 범위", unit="%")
    if answer.startswith("현재 지원되지 않는 시장 매핑입니다."):
        return ProvenanceRow(source="시장 매핑")
    if answer.startswith("브랜드·시장·기간을 지정해 주세요."):
        return ProvenanceRow(source="질문 조건")
    if answer.startswith("브랜드 목록에서 일치 항목을 찾지 못했습니다."):
        return ProvenanceRow(source="브랜드 카탈로그")
    if answer.startswith("데이터 존재 여부를 확인하지 못했습니다."):
        return ProvenanceRow(source="조회 상태")
    if answer.startswith("보유 데이터는"):
        available_to = _first_nested_value(calls, "available_to") or _latest_call_period(calls)
        return ProvenanceRow(source="보유 범위", period=str(available_to or MISSING_LABEL))
    return ProvenanceRow(source="지원 상태")


def _provenance_rows(calls: Sequence[Mapping[str, Any]]) -> tuple[ProvenanceRow, ...]:
    hira_years = sorted(
        {
            str(request.get("year"))
            for call in calls
            if call.get("tool") == "hira_disease_hospitalization_outpatient_stats"
            for request in (_render_data(call).get("request"),)
            if isinstance(request, Mapping) and request.get("year")
        }
    )
    if len(hira_years) >= 3:
        period = hira_years[0] if len(hira_years) == 1 else f"{hira_years[0]}~{hira_years[-1]}"
        return (
            ProvenanceRow(
                source="HIRA 질병정보서비스",
                period=period,
                view=MISSING_LABEL,
                market=MISSING_LABEL,
                denominator=MISSING_LABEL,
                channel="전체",
                unit="명",
            ),
        )
    primary_calls = tuple(call for call in calls if call.get("tool") != "agent_calculation")
    result_calls = tuple(call for call in primary_calls if not _is_query_plan_call(call))
    return provenance_rows_from_calls(result_calls or primary_calls or calls, ())


def _is_query_plan_call(call: Mapping[str, Any]) -> bool:
    return call.get("tool") == "query_spec" or _render_data(call).get("metric") == "query_spec"


def _complete_row(
    row: ProvenanceRow,
    *,
    question: str,
    answer: str,
    unit: str | None = None,
) -> ProvenanceRow:
    fields = {
        name: "해당 없음" if value == MISSING_LABEL else value
        for name, value in row.as_fields().items()
    }
    source = fields["source"]
    period = fields["period"]
    view = fields["view"]
    market = fields["market"]
    denominator = fields["denominator"]
    channel = fields["channel"]
    row_unit = fields["unit"]
    brand = fields["brand"]
    verified_market = _verified_row_market(row)
    if market == "해당 없음" and verified_market is not None:
        market = verified_market
    if view.startswith("전략뷰"):
        view = "전략뷰"
    elif view.startswith("일반뷰"):
        view = "일반뷰"
    market_match = _ANSWER_MARKET_RE.search(answer)
    denominator_match = re.search(r"(?m)^점유율 분모:\s*(.+?)\s*$", answer)
    if market == "해당 없음" and market_match is not None:
        answer_market = public_value(market_match.group("market"))
        if answer_market != MISSING_LABEL:
            market = answer_market
    if denominator == "해당 없음" and denominator_match is not None:
        denominator = denominator_match.group(1).strip()
    if market == "해당 없음" and view == "전략뷰":
        market = "요청 브랜드의 전략 시장"
    requested_channel = _requested_channel(question)
    if any(token in question for token in ("채널별", "채널 별")):
        channel = "채널별"
    elif requested_channel:
        channel = requested_channel
    if unit:
        row_unit = unit
    return ProvenanceRow(
        source=source,
        period=period,
        view=view,
        market=market,
        denominator=denominator,
        channel=channel,
        unit=row_unit,
        brand=brand,
    )


def _requested_unit(question: str, answer: str) -> str | None:
    if "환자" in question:
        return "명"
    if "영업활동" in question:
        return "건"
    if "처방량" in question:
        return "Rx"
    if any(token in question for token in ("매출", "시장규모", "규모")):
        return "억원"
    if any(token in question for token in ("점유율", "CR")) or re.search(r"\d[\d,.]*%", answer):
        return "%"
    if "HHI" in question and "CR" not in question:
        return "지수"
    return None


def _find_hhi(calls: Sequence[Mapping[str, Any]]) -> Decimal | None:
    for call in calls:
        data = _render_data(call)
        for key in ("hhi", "hhi_recent"):
            value = _decimal(data.get(key))
            if value is not None:
                return value
    return None


def _has_grounded_market_evidence(calls: Sequence[Mapping[str, Any]]) -> bool:
    for call in calls:
        data = _render_data(call)
        if str(data.get("status") or "ok").lower() != "ok":
            continue
        segments = data.get("level_segments")
        if isinstance(segments, Sequence) and not isinstance(segments, str | bytes) and segments:
            return True
        if any(
            data.get(key) not in (None, "")
            for key in ("hhi", "hhi_recent", "market_size_억원", "market_size_recent_krw", "sales_억원")
        ):
            return True
    return False


def _requested_top_count(question: str) -> int:
    match = re.search(r"상위\s*(\d+)\s*개", question)
    if not match:
        return 0
    count = int(match.group(1))
    return count if 1 <= count <= 20 else 0


def _top_ranked_rows(
    calls: Sequence[Mapping[str, Any]],
    count: int,
) -> tuple[tuple[int, str, Decimal], ...]:
    for call in calls:
        segments = _render_data(call).get("level_segments")
        if not isinstance(segments, Sequence) or isinstance(segments, str | bytes):
            continue
        rows: list[tuple[int, str, Decimal]] = []
        for expected_rank, item in enumerate(segments[:count], 1):
            if not isinstance(item, Mapping):
                break
            try:
                rank = int(item.get("rank"))
            except (TypeError, ValueError):
                break
            brand = str(item.get("brand") or item.get("name") or "").strip()
            share = _decimal(item.get("ms_recent_pct"))
            if rank != expected_rank or not brand or share is None:
                break
            rows.append((rank, brand, share))
        if len(rows) == count:
            return tuple(rows)
    return ()


def _top_shares(calls: Sequence[Mapping[str, Any]], count: int) -> tuple[Decimal, ...]:
    for call in calls:
        data = _render_data(call)
        segments = data.get("level_segments")
        if not isinstance(segments, Sequence) or isinstance(segments, str | bytes):
            continue
        shares: list[Decimal] = []
        for item in segments[:count]:
            if not isinstance(item, Mapping):
                break
            share = _decimal(item.get("ms_recent_pct"))
            if share is None:
                break
            shares.append(share)
        if len(shares) == count:
            return tuple(shares)
    return ()


def _first_nested_value(calls: Sequence[Mapping[str, Any]], key: str) -> Any:
    for call in calls:
        data = _render_data(call)
        if data.get(key) not in (None, ""):
            return data[key]
    return ""


def _calls_matching_question(
    question: str,
    calls: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    if "영업활동" in question:
        return tuple(call for call in calls if call.get("tool") == "csd_activity_trend")
    if any(token in question for token in ("채널별", "채널 별")):
        return tuple(call for call in calls if _call_dimension(call) == "channel")
    if "진료과" in question:
        return tuple(call for call in calls if _call_dimension(call) == "specialty")
    channel = _requested_channel(question)
    if channel:
        matched = tuple(call for call in calls if channel in _call_channels(call))
        return matched
    market_match = re.search(r"\bml_\d+\b", question, re.IGNORECASE)
    if market_match is not None:
        requested_market = market_match.group(0).lower()
        matched = tuple(
            call
            for call in calls
            if str(
                _render_data(call).get("market_id")
                or _render_data(call).get("market")
                or ""
            ).lower()
            == requested_market
        )
        return matched
    return tuple(calls)


def _requested_channel(question: str) -> str:
    aliases = (
        ("상급종합병원", "상급종병"),
        ("상급종병", "상급종병"),
        ("종합병원", "종병"),
        ("종병", "종병"),
        ("병원", "병원"),
        ("의원", "의원"),
        ("약국", "약국"),
    )
    for alias, channel in aliases:
        if alias in question:
            return channel
    return ""


def _call_channels(call: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for candidate in (call.get("applied_filters"), _render_data(call).get("applied_filters")):
        if not isinstance(candidate, Mapping):
            continue
        value = candidate.get("channel") or candidate.get("visit_location")
        if isinstance(value, Sequence) and not isinstance(value, str | bytes):
            values.extend(_canonical_channel(str(item)) for item in value)
        elif value not in (None, ""):
            values.append(_canonical_channel(str(value)))
    return tuple(values)


def _canonical_channel(value: str) -> str:
    aliases = {
        "상급종합병원": "상급종병",
        "상급종병": "상급종병",
        "종합병원": "종병",
        "종병": "종병",
    }
    return aliases.get(value.strip(), value.strip())


def _latest_call_period(calls: Sequence[Mapping[str, Any]]) -> str:
    periods: list[str] = []
    for call in calls:
        data = _render_data(call)
        for candidate in (data.get("period"), data.get("available_to")):
            if candidate not in (None, ""):
                periods.append(str(candidate))
        for series in _series_candidates(data):
            periods.extend(
                str(item.get("period"))
                for item in series
                if isinstance(item, Mapping) and item.get("period")
            )
    return max(periods) if periods else ""


def _call_dimension(call: Mapping[str, Any]) -> str:
    data = _render_data(call)
    explicit = str(data.get("requested_dimension") or data.get("level") or "").lower()
    if explicit in {"channel", "specialty"}:
        return explicit
    for candidate in (data.get("query_spec"), call.get("query_spec")):
        if not isinstance(candidate, Mapping):
            continue
        dimensions = candidate.get("dimensions") or candidate.get("group_by")
        if isinstance(dimensions, Sequence) and not isinstance(dimensions, str | bytes):
            for dimension in dimensions:
                if str(dimension).lower() in {"channel", "specialty"}:
                    return str(dimension).lower()
    return ""


def available_conditional_axes(calls: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    axis_for_dimension = {"specialty": "진료과", "channel": "유통채널"}
    available: list[str] = []
    for call in calls:
        if _call_failed(call) or not _has_usable_call_data(call):
            continue
        axis = axis_for_dimension.get(_call_dimension(call))
        if axis and axis not in available:
            available.append(axis)
    return tuple(available)


def _is_file_tool(call: Mapping[str, Any]) -> bool:
    tool = str(call.get("tool") or "")
    source = str(call.get("source") or "")
    return tool.startswith(("query_uploaded_sql", "search_uploaded_file", "file_")) or source.startswith(
        ("uploaded_file", "file_sql")
    )


def _first_present_with_key(container: Mapping[str, Any], keys: Sequence[str]) -> tuple[str, Any]:
    for key in keys:
        if container.get(key) not in (None, ""):
            return key, container[key]
    return "", None


def _render_data(call: Mapping[str, Any]) -> Mapping[str, Any]:
    data = call.get("render_data")
    return data if isinstance(data, Mapping) else {}


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


def _krw_to_eok(value: Any) -> Decimal | None:
    amount = _decimal(value)
    return amount / Decimal("100000000") if amount is not None else None
