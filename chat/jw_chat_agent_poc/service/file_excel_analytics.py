from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

from jw_chat_agent_poc.service.actor_context import code_serving_actor_headers


class AnalyticsFileSource(Protocol):
    logical_name: str
    file_name: str
    sheet_name: str


@dataclass(frozen=True, slots=True)
class FileAnalyticsOutcome:
    file_context: str
    answer_md: str
    status: str
    trace: tuple[dict[str, str], ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)


AnalyticsPost = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]

_CATEGORY_RE = re.compile(
    r"(?:점유율|m/s|market\s*share).*(?:성장|추이)|"
    r"(?:성장|추이).*(?:점유율|m/s|market\s*share)",
    re.IGNORECASE,
)
_TOP_GROWING_RE = re.compile(
    r"(?:가장|최고|상위).{0,12}(?:성장|증가).{0,12}(?:카테고리|분류)|"
    r"(?:카테고리|분류).{0,12}(?:가장|최고|상위).{0,12}(?:성장|증가)",
    re.IGNORECASE,
)
_MOLECULE_RE = re.compile(r"(?:성분|molecule)", re.IGNORECASE)
_ATC_RE = re.compile(
    r"\bATC\s*([1-4])\s+([A-Z][A-Z0-9]*)(?=$|[^A-Z0-9])",
    re.IGNORECASE,
)
_ATC_ANALYTICS_RE = re.compile(
    r"(?:제품별|브랜드별|매출|수량|볼륨|점유율|m/s|성장률|cagr)",
    re.IGNORECASE,
)
_BRAND_MEASURE_RE = re.compile(
    r"(?P<brand>[가-힣A-Za-z0-9][가-힣A-Za-z0-9._-]{1,})"
    r"(?=\s*(?:20\d{2}\s*년\s*)?(?:\d{1,2}\s*월\s*)?"
    r"(?:매출|판매액|판매|금액|총액|수량|성장률))",
    re.IGNORECASE,
)
_MONTH_RE = re.compile(r"(20\d{2})\s*년\s*(0?[1-9]|1[0-2])\s*월")
_PARTICLES = (
    "에서부터",
    "으로부터",
    "이랑",
    "에서",
    "부터",
    "으로",
    "까지",
    "랑",
    "의",
    "을",
    "를",
    "은",
    "는",
    "이",
    "가",
    "과",
    "와",
    "도",
    "만",
)
_NON_BRAND_TOKENS = frozenset({"sellout", "sellin", "values", "value", "unit"})
_BRAND_PERIOD_MODIFIER_RE = re.compile(
    r"(?:최근|올해|금년|작년|전년|당월|당해|현재|"
    r"\d{1,2}(?:개)?년|\d{1,2}개월|20\d{2}년|상반기|하반기|분기|"
    r"(?:연도|월|분기|반기|기간)별)",
    re.IGNORECASE,
)
_NON_BRAND_GENERIC_TOKENS = frozenset(
    {"브랜드", "브랜드별", "카테고리", "시장", "제품", "전체", "상위"}
)
_BRAND_QUALIFIER_RE = re.compile(
    r"(?:가장|최고|최대|최소|상위|하위|전체|모든)|"
    r"(?:0?[1-9]|1[0-2])월",
    re.IGNORECASE,
)


def _analytics_intent(question: str) -> str | None:
    if _TOP_GROWING_RE.search(question):
        return "top_growing"
    if _CATEGORY_RE.search(question):
        return "category_overview"
    if _ATC_RE.search(question) and _ATC_ANALYTICS_RE.search(question):
        return "category_overview"
    if _MOLECULE_RE.search(question) and re.search(
        r"(?:기준|보여|성장|점유율)", question
    ):
        return "molecule"
    if _brand_measure_match(question) is not None:
        return (
            "brand_monthly_yoy"
            if re.search(r"전년\s*동월|YoY|성장률", question, re.IGNORECASE)
            else "brand_monthly"
        )
    return None


def _brand_value_candidates(value: str) -> tuple[str, ...]:
    original = value.strip()
    if not original:
        return ()
    candidates = [original]
    for particle in _PARTICLES:
        if not original.endswith(particle) or len(original) <= len(particle) + 1:
            continue
        stem = original[: -len(particle)]
        if particle in {"이", "은", "과", "이랑", "을", "으로"} and not _has_batchim(stem):
            continue
        if particle in {"가", "는", "와", "랑", "를"} and _has_batchim(stem):
            continue
        candidates.append(stem)
        break
    return tuple(dict.fromkeys(candidates))


def _brand_measure_match(question: str) -> re.Match[str] | None:
    return next(
        (
            match
            for match in _BRAND_MEASURE_RE.finditer(question)
            if _brand_candidate_reason(match.group("brand")) is None
        ),
        None,
    )


def _brand_candidate_reason(candidate: str) -> str | None:
    normalized = candidate.strip().casefold()
    if normalized in _NON_BRAND_TOKENS or normalized in _NON_BRAND_GENERIC_TOKENS:
        return "generic_noun"
    if _BRAND_PERIOD_MODIFIER_RE.fullmatch(normalized):
        return "period_modifier"
    if _BRAND_QUALIFIER_RE.fullmatch(normalized):
        return "query_modifier"
    return None


def _brand_candidate_filter(question: str) -> dict[str, Any]:
    matches = tuple(_BRAND_MEASURE_RE.finditer(question))
    accepted = next(
        (match for match in matches if _brand_candidate_reason(match.group("brand")) is None),
        None,
    )
    if accepted is not None:
        return {
            "candidate": accepted.group("brand"),
            "excluded": False,
            "reason": "brand_candidate",
            "excluded_count": sum(
                _brand_candidate_reason(match.group("brand")) is not None
                for match in matches
            ),
        }
    excluded = next(
        (match for match in matches if _brand_candidate_reason(match.group("brand"))),
        None,
    )
    return {
        "candidate": excluded.group("brand") if excluded is not None else None,
        "excluded": excluded is not None,
        "reason": (
            _brand_candidate_reason(excluded.group("brand"))
            if excluded is not None
            else "no_candidate"
        ),
        "excluded_count": len(matches),
    }


def _with_brand_candidate_filter(
    outcome: FileAnalyticsOutcome,
    candidate_filter: Mapping[str, Any],
) -> FileAnalyticsOutcome:
    status = "excluded" if candidate_filter.get("excluded") else "accepted"
    return FileAnalyticsOutcome(
        file_context=outcome.file_context,
        answer_md=outcome.answer_md,
        status=outcome.status,
        trace=(
            *outcome.trace,
            {
                "stage": "brand_candidate_filter",
                "status": status,
                "candidate": str(candidate_filter.get("candidate") or ""),
                "reason": str(candidate_filter.get("reason") or ""),
                "excluded_count": str(candidate_filter.get("excluded_count") or 0),
            },
        ),
        detail={**outcome.detail, "brand_candidate_filter": dict(candidate_filter)},
    )


def _has_batchim(value: str) -> bool:
    if not value:
        return False
    codepoint = ord(value[-1])
    return 0xAC00 <= codepoint <= 0xD7A3 and (codepoint - 0xAC00) % 28 != 0


def query_file_analytics(
    question: str,
    conversation_id: str,
    sources: Sequence[AnalyticsFileSource],
    *,
    post: AnalyticsPost | None = None,
) -> FileAnalyticsOutcome | None:
    candidate_filter = _brand_candidate_filter(question)
    intent = _analytics_intent(question)
    if intent is None or not sources:
        return None
    source = sources[0]
    request = post or _post_analytics
    base = _base_payload(conversation_id, source.logical_name)
    schema_body = dict(request("/file-sql/analytics", {**base, "operation": "schema"}))
    schema = schema_body.get("schema")
    if not isinstance(schema, Mapping):
        return _failure("analytics schema unavailable", "schema_failed")
    dimensions = tuple(str(value) for value in schema.get("dimensions") or ())
    if intent in {"brand_monthly", "brand_monthly_yoy"}:
        outcome = _query_brand_monthly(
            question,
            source,
            request,
            base,
            schema_body,
            schema,
            dimensions,
        )
        return (
            _with_brand_candidate_filter(outcome, candidate_filter)
            if outcome is not None
            else None
        )
    complete_years = tuple(
        int(value)
        for value in schema.get("complete_years") or ()
        if isinstance(value, int)
    )
    if _MOLECULE_RE.search(question):
        return _unsupported_dimension(source, dimensions, schema_body)
    if len(complete_years) < 2:
        return _failure("complete-year range unavailable", "period_unresolved")

    end_year = complete_years[-1]
    requested_span = _requested_growth_span(question)
    start_year = max(complete_years[0], end_year - requested_span)
    payload: dict[str, Any] = {
        **base,
        "operation": intent,
        "dimension": "ATC 3",
        "start_year": start_year,
        "end_year": end_year,
    }
    if intent == "category_overview":
        match = _ATC_RE.search(question)
        if match is None:
            return _dimension_candidates(source, dimensions, schema_body)
        dimension = f"ATC {match.group(1)}"
        if dimension not in dimensions:
            return _unsupported_dimension(
                source, dimensions, schema_body, requested=dimension
            )
        payload.update(
            {
                "dimension": dimension,
                "dimension_value": match.group(2).upper(),
                "brand_dimension": _brand_dimension(dimensions),
                "match_mode": "prefix",
                "limit": 100,
            }
        )
    else:
        payload.update({"min_last_sales": 5_000_000_000, "limit": 20})
    response = dict(request("/file-sql/analytics", payload))
    return _with_brand_candidate_filter(
        _outcome(source, payload, response, schema_body),
        candidate_filter,
    )


def _query_brand_monthly(
    question: str,
    source: AnalyticsFileSource,
    request: AnalyticsPost,
    base: Mapping[str, Any],
    schema_body: Mapping[str, Any],
    schema: Mapping[str, Any],
    dimensions: Sequence[str],
) -> FileAnalyticsOutcome | None:
    match = _brand_measure_match(question)
    if match is None:
        return None
    candidates = _brand_value_candidates(match.group("brand"))
    if not candidates:
        return None
    target_period = _requested_month(question) or _schema_latest_month(schema)
    if not target_period:
        return _failure("monthly period unavailable", "period_unresolved")
    operation = (
        "brand_monthly_yoy"
        if re.search(r"전년\s*동월|YoY|성장률", question, re.IGNORECASE)
        else "brand_monthly"
    )
    measures = ["sales"]
    if re.search(r"수량|units?|volume", question, re.IGNORECASE):
        measures.append("units")
    attempts: list[dict[str, Any]] = []
    response: dict[str, Any] = {}
    payload: dict[str, Any] = {}
    for candidate in candidates:
        payload = {
            **base,
            "operation": operation,
            "brand_dimension": _brand_dimension(dimensions),
            "brand_value": candidate,
            "match_mode": "exact",
            "measures": measures,
            **(
                {"target_period": target_period}
                if operation == "brand_monthly_yoy"
                else {"periods": [target_period]}
            ),
        }
        response = dict(request("/file-sql/analytics", payload))
        attempts.append(
            {
                "brand_value": candidate,
                "status": str(response.get("status") or ""),
                "brand_exists": response.get("brand_exists"),
            }
        )
        if response.get("brand_exists") is not False:
            break
    outcome = _outcome(source, payload, response, schema_body)
    return FileAnalyticsOutcome(
        file_context=outcome.file_context,
        answer_md=outcome.answer_md,
        status=outcome.status,
        trace=outcome.trace,
        detail={**outcome.detail, "brand_binding_attempts": attempts},
    )


def _requested_month(question: str) -> str:
    match = _MONTH_RE.search(question)
    return f"{match.group(1)}-{int(match.group(2)):02d}" if match else ""


def _schema_latest_month(schema: Mapping[str, Any]) -> str:
    value = str(schema.get("period_end") or "")
    match = re.match(r"(20\d{2})-(0[1-9]|1[0-2])", value)
    return f"{match.group(1)}-{match.group(2)}" if match else ""


def _base_payload(conversation_id: str, logical_name: str) -> dict[str, Any]:
    return {
        "workflow_id": int(os.getenv("JW_CHAT_FILE_WORKFLOW_ID", "301")),
        "app_session_id": conversation_id,
        "chat_id": conversation_id,
        "logical_name": logical_name,
    }


def _post_analytics(path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    base_url = os.getenv(
        "JW_CHAT_FILE_SEARCH_BASE", "http://code-serving-235:8080"
    ).rstrip("/")
    response = requests.post(
        f"{base_url}{path}",
        json=dict(payload),
        headers=code_serving_actor_headers(),
        timeout=float(os.getenv("JW_CHAT_FILE_ANALYTICS_TIMEOUT_S", "75")),
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise TypeError("file analytics response must be an object")
    return body


def _requested_growth_span(question: str) -> int:
    match = re.search(r"최근\s*(\d+)\s*개?년", question)
    return min(max(int(match.group(1)), 1), 10) if match else 3


def _brand_dimension(dimensions: Sequence[str]) -> str:
    return next(
        (value for value in dimensions if value.casefold() == "product name kor"),
        "PRODUCT NAME KOR",
    )


def _unsupported_dimension(
    source: AnalyticsFileSource,
    dimensions: Sequence[str],
    schema_body: Mapping[str, Any],
    *,
    requested: str = "MOLECULE DESC",
) -> FileAnalyticsOutcome:
    available = ", ".join(dimensions) or "없음"
    answer = f"이 파일에는 {requested} 성분 차원이 없습니다. 사용 가능한 차원은 {available}입니다."
    return FileAnalyticsOutcome(
        file_context=f"## 업로드 스프레드시트 분석\n파일: {source.file_name}\n{answer}",
        answer_md=answer,
        status="unsupported_dimension",
        trace=({"stage": "analytics_binding", "status": "unsupported_dimension"},),
        detail={
            "analytics_schema": dict(schema_body),
            "available_dimensions": list(dimensions),
        },
    )


def _dimension_candidates(
    source: AnalyticsFileSource,
    dimensions: Sequence[str],
    schema_body: Mapping[str, Any],
) -> FileAnalyticsOutcome:
    available = ", ".join(dimensions) or "없음"
    answer = (
        f"분석 차원과 값을 확정하지 못했습니다. 사용 가능한 차원은 {available}입니다."
    )
    return FileAnalyticsOutcome(
        file_context=f"## 업로드 스프레드시트 분석\n파일: {source.file_name}\n{answer}",
        answer_md=answer,
        status="dimension_unresolved",
        trace=({"stage": "analytics_binding", "status": "dimension_unresolved"},),
        detail={
            "analytics_schema": dict(schema_body),
            "available_dimensions": list(dimensions),
        },
    )


def _failure(message: str, status: str) -> FileAnalyticsOutcome:
    answer = f"파일 분석을 완료하지 못했습니다. {message}"
    return FileAnalyticsOutcome(
        answer, answer, status, trace=({"stage": "analytics", "status": status},)
    )


def _outcome(
    source: AnalyticsFileSource,
    request: Mapping[str, Any],
    response: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> FileAnalyticsOutcome:
    operation = str(response.get("operation") or request.get("operation") or "")
    columns = tuple(str(value) for value in response.get("columns") or ())
    rows = tuple(row for row in response.get("rows") or () if isinstance(row, list))
    years = tuple(
        int(value) for value in response.get("years") or () if isinstance(value, int)
    )
    if operation in {"brand_monthly", "brand_monthly_yoy"}:
        return _brand_monthly_outcome(
            source,
            request,
            response,
            schema,
            columns,
            rows,
        )
    period = f"{years[0]}~{years[-1]}" if len(years) >= 2 else "완전 연도"
    if operation == "category_overview":
        answer = _category_answer(response, period)
        table_rows = tuple(
            row for row in rows if len(row) >= 7 and row[1] == years[-1]
        )[:40]
        headers = (
            "브랜드",
            "연도",
            "매출",
            "수량",
            "M/S",
            "판매가(SO)",
            "매입가(SI)",
            "YoY 성장률(%)",
            "CAGR(%)",
        )
    else:
        answer = _growth_answer(rows, period)
        table_rows = rows[:20]
        headers = ("카테고리", "시작 매출", "종료 매출", "CAGR")
    context = "\n".join(
        (
            "## 업로드 스프레드시트 분석",
            f"파일: {source.file_name}",
            f"시트: {source.sheet_name}",
            f"완전 연도 {period} 기준 · 부분 연도 제외",
            (
                "판매가(SO)=소비자 판매 단가 · 매입가(SI)=약국 매입 단가 · "
                "연도 값은 해당 연도 평균"
            ) if operation == "category_overview" else "",
            _markdown_table(headers, table_rows),
        )
    )
    detail = {
        "generation_path": "template_analytics",
        "analytics_request": dict(request),
        "analytics_response": dict(response),
        "analytics_schema": dict(schema),
        "columns": list(columns),
        "rows": [list(row) for row in table_rows],
        "period": period,
        "executed_sql": response.get("executed_sql")
        or response.get("executed_sql_statements"),
        "display_sql": _display_sql(response),
        "table_mapping": response.get("table_mapping")
        or response.get("sheet_table_map"),
        "aggregate_values": response.get("aggregate_values"),
        "aggregation_summary": response.get("aggregation_summary"),
    }
    return FileAnalyticsOutcome(
        file_context=context,
        answer_md=answer,
        status=str(response.get("status") or "ok"),
        trace=(
            {"stage": "analytics_schema", "status": "ok"},
            {"stage": "analytics_query", "status": str(response.get("status") or "ok")},
        ),
        detail=detail,
    )


def _brand_monthly_outcome(
    source: AnalyticsFileSource,
    request: Mapping[str, Any],
    response: Mapping[str, Any],
    schema: Mapping[str, Any],
    columns: Sequence[str],
    rows: Sequence[list[Any]],
) -> FileAnalyticsOutcome:
    brand = str(request.get("brand_value") or "요청 브랜드")
    target_period = str(
        request.get("target_period")
        or next(iter(request.get("periods") or ()), "요청 기간")
    )
    candidates = tuple(str(value) for value in response.get("candidates") or ())
    unavailable = tuple(
        value
        for value in response.get("unavailable_metrics") or ()
        if isinstance(value, Mapping)
    )
    absence_reason = ""
    if response.get("brand_exists") is False:
        answer = (
            f"업로드 파일에서 '{brand}'을 찾지 못했으며, "
            f"유사 브랜드: {', '.join(candidates)}입니다."
            if candidates
            else f"업로드 파일에서 '{brand}'을 찾지 못했습니다."
        )
        absence_reason = "brand_not_found"
    elif any(value.get("reason") == "period_not_found" for value in unavailable):
        answer = f"업로드 파일에서 '{brand}' 브랜드는 존재하나 {target_period} 데이터가 없습니다."
        absence_reason = "period_not_found"
    else:
        answer = _brand_monthly_answer(rows, columns, target_period)
    headers = tuple(_brand_monthly_header(value) for value in columns)
    table = _markdown_table(headers, rows) if headers and rows else ""
    context = "\n".join(
        part
        for part in (
            "## 업로드 스프레드시트 분석",
            f"파일: {source.file_name}",
            f"시트: {source.sheet_name}",
            answer,
            table,
        )
        if part
    )
    metrics = [
        dict(value)
        for value in response.get("metrics") or ()
        if isinstance(value, Mapping)
    ]
    detail = {
        "generation_path": "template_analytics",
        "analytics_request": dict(request),
        "analytics_response": dict(response),
        "analytics_schema": dict(schema),
        "columns": list(columns),
        "rows": [list(row) for row in rows],
        "period": target_period,
        "executed_sql": response.get("executed_sql")
        or response.get("executed_sql_statements"),
        "display_sql": _display_sql(response),
        "table_mapping": response.get("table_mapping")
        or response.get("sheet_table_map"),
        "aggregate_values": response.get("aggregate_values"),
        "aggregation_summary": response.get("aggregation_summary"),
        "analytics_metrics": metrics,
        "absence_reason": absence_reason,
        "candidates": list(candidates),
    }
    return FileAnalyticsOutcome(
        file_context=context,
        answer_md=answer,
        status=str(response.get("status") or "ok"),
        trace=(
            {"stage": "analytics_schema", "status": "ok"},
            {
                "stage": "analytics_query",
                "status": str(response.get("status") or "ok"),
            },
        ),
        detail=detail,
    )


def _brand_monthly_answer(
    rows: Sequence[list[Any]],
    columns: Sequence[str],
    target_period: str,
) -> str:
    if not rows:
        return f"업로드 파일의 {target_period} 조건에서 결과 행을 찾지 못했습니다."
    values = dict(zip(columns, rows[0]))
    brand = str(values.get("brand") or "요청 브랜드")
    period = str(values.get("target_period") or values.get("period") or target_period)
    comparison = str(values.get("comparison_period") or "")
    facts: list[str] = []
    if isinstance(values.get("sales"), (int, float)):
        facts.append(f"매출 {_won(values['sales'])}")
    if isinstance(values.get("units"), (int, float)):
        facts.append(f"수량 {float(values['units']):,.0f} UNIT")
    first = f"업로드 파일에서 {brand}의 {period} " + ", ".join(facts) + "입니다."
    changes: list[str] = []
    if isinstance(values.get("sales_yoy_pct"), (int, float)):
        changes.append(f"매출 {float(values['sales_yoy_pct']):+.2f}%")
    if isinstance(values.get("units_yoy_pct"), (int, float)):
        changes.append(f"수량 {float(values['units_yoy_pct']):+.2f}%")
    second = (
        f"업로드 파일에서 전년 동월({comparison}) 대비 "
        + ", ".join(changes)
        + "입니다."
        if comparison and changes
        else ""
    )
    return " ".join(part for part in (first, second) if part)


def _brand_monthly_header(value: str) -> str:
    return {
        "brand": "브랜드",
        "period": "기간",
        "target_period": "대상 기간",
        "comparison_period": "비교 기간",
        "sales": "매출",
        "comparison_sales": "전년 동월 매출",
        "sales_yoy_pct": "매출 YoY(%)",
        "units": "수량",
        "comparison_units": "전년 동월 수량",
        "units_yoy_pct": "수량 YoY(%)",
    }.get(value, value)


def _category_answer(response: Mapping[str, Any], period: str) -> str:
    metrics = tuple(
        value for value in response.get("metrics") or () if isinstance(value, Mapping)
    )
    sales = [value for value in metrics if value.get("name") == "category_sales"]
    growth = [value for value in metrics if value.get("name") == "category_growth"]
    cagr = next(
        (value for value in metrics if value.get("name") == "category_cagr"), {}
    )
    rows = tuple(
        row
        for row in response.get("rows") or ()
        if isinstance(row, list) and len(row) >= 7
    )
    latest_year = max((int(row[1]) for row in rows), default=0)
    latest = sorted(
        (row for row in rows if row[1] == latest_year),
        key=lambda row: float(row[2]),
        reverse=True,
    )
    start_value = sales[0].get("value") if sales else None
    end_value = sales[-1].get("value") if sales else None
    growth_text = "/".join(f"{float(value.get('value')):+.2f}%" for value in growth)
    top_text = (
        f"{latest[0][0]} M/S {float(latest[0][4]):.2f}%"
        if latest
        else "대표 브랜드 미제공"
    )
    summary = (
        f"완전 연도 {period} 기준 카테고리 매출은 {_won(start_value)}에서 {_won(end_value)}로 변했습니다. "
        f"연도별 성장률은 {growth_text or '미제공'}이고 CAGR은 {float(cagr.get('value')):.2f}%입니다. "
        f"최신 연도 1위는 {top_text}입니다."
    )
    if latest and len(latest[0]) >= 10:
        brand_cagr = latest[0][8]
        first_year = latest[0][9]
        if isinstance(brand_cagr, (int, float)) and isinstance(first_year, int):
            summary += (
                f" 선두 {latest[0][0]}는 {first_year}년 진입 후 "
                f"연평균 {brand_cagr:.2f}% 성장했습니다."
            )
    return summary


def _growth_answer(rows: Sequence[list[Any]], period: str) -> str:
    leaders = ", ".join(
        f"{row[0]} {float(row[3]):+.2f}%" for row in rows[:2] if len(row) >= 4
    )
    return f"완전 연도 {period} 기준 성장 상위 카테고리는 {leaders or '확인되지 않았습니다'}."


def _won(value: Any) -> str:
    return f"{float(value):,.0f}원" if isinstance(value, (int, float)) else "미제공"


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    header = "| " + " | ".join(headers) + " |"
    divider = "| " + " | ".join("---" for _ in headers) + " |"
    body = [
        "| "
        + " | ".join(
            _format_cell(value, headers[index] if index < len(headers) else "")
            for index, value in enumerate(row[: len(headers)])
        )
        + " |"
        for row in rows
    ]
    return "\n".join((header, divider, *body))


def _format_cell(value: Any, header: str) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        if header in {"M/S", "CAGR", "YoY 성장률(%)", "CAGR(%)"}:
            return f"{value:,.2f}%"
        return (
            f"{value:,.2f}"
            if header in {"SO가", "SI가", "판매가(SO)", "매입가(SI)"}
            else f"{value:,.0f}"
        )
    if isinstance(value, int):
        if header == "연도":
            return str(value)
        return f"{value:,}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _display_sql(response: Mapping[str, Any]) -> str:
    direct = response.get("display_sql")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    statements = response.get("executed_sql_statements")
    if not isinstance(statements, Sequence) or isinstance(statements, (str, bytes)):
        return ""
    rendered = tuple(
        str(statement.get("display_sql") or "").strip()
        for statement in statements
        if isinstance(statement, Mapping)
        and str(statement.get("display_sql") or "").strip()
    )
    return "\n\n".join(rendered)
