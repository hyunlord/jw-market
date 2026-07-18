from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from collections.abc import Iterable
from typing import Any, Final


NUMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z])[+-]?\d[\d,]*(?:\.\d+)?(?:\s*(?:억\s*원|억원|원|명|건|개|위|년|월|%p|%))?"
)
CODE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]{1,8}\d[A-Za-z0-9.-]*(?![A-Za-z0-9])",
)
TABLE_LIMIT: Final[int] = 6


def allowed_numbers(markdown: str) -> tuple[str, ...]:
    numbers = {normalize_number(match.group(0)) for match in NUMBER_RE.finditer(markdown)}
    numbers.update(match.group(0).upper() for match in CODE_RE.finditer(markdown))
    return tuple(sorted(number for number in numbers if number))


def sanitize_interpretation(markdown: str, allowed: tuple[str, ...]) -> str:
    allowed_set = set(allowed)
    kept: list[str] = []
    for line in markdown.splitlines():
        tokens = {normalize_number(match.group(0)) for match in NUMBER_RE.finditer(line)}
        tokens.update(match.group(0).upper() for match in CODE_RE.finditer(line))
        tokens.discard("")
        if tokens and not tokens.issubset(allowed_set):
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    if cleaned:
        return cleaned
    return "- 표에 포함된 확정 데이터만 기준으로 해석합니다."


def table(title: str, headers: tuple[str, ...], rows: tuple[tuple[Any, ...], ...]) -> str:
    actual_headers = headers
    actual_rows = rows
    if not actual_rows:
        actual_headers = ("항목", "값")
        actual_rows = (("-", "데이터 없음"),)
    header = "| " + " | ".join(actual_headers) + " |"
    divider = "| " + " | ".join("---" for _ in actual_headers) + " |"
    body = ["| " + " | ".join(cell(value) for value in row) + " |" for row in actual_rows]
    return "\n".join((title, header, divider, *body))


def cell(value: Any) -> str:
    text = "-" if value is None or value == "" else str(value)
    return escape(text, quote=False).replace("|", "\\|").replace("\n", " ")


def items(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = data.get("items")
    if isinstance(raw_items, list):
        return [item for item in raw_items[:TABLE_LIMIT] if isinstance(item, dict)]
    return []


def eok_value(eok: Any, krw: Any) -> str:
    if isinstance(eok, int | float):
        return f"{float(eok):,.2f}억원"
    if isinstance(krw, int | float):
        return f"{float(krw) / 100_000_000:,.2f}억원"
    return ""


def precise_eok_value(eok: Any, krw: Any, *, decimal_places: int = 6) -> str:
    """Render source precision for explicit single-period answers."""

    value: Decimal
    try:
        if isinstance(krw, (int, float, Decimal)) and not isinstance(krw, bool):
            value = Decimal(str(krw)) / Decimal("100000000")
        elif isinstance(eok, (int, float, Decimal)) and not isinstance(eok, bool):
            value = Decimal(str(eok))
        else:
            return ""
        value = value.quantize(
            Decimal(1).scaleb(-decimal_places),
            rounding=ROUND_HALF_UP,
        )
    except InvalidOperation:
        return ""
    if not value.is_finite():
        return ""
    text = f"{value:,f}".rstrip("0").rstrip(".")
    return f"{text}억원"


def latest_series_eok(series: Any) -> str:
    if not isinstance(series, dict) or not series:
        return ""
    latest = series[sorted(series)[-1]]
    if isinstance(latest, int | float):
        return f"{float(latest) / 100_000_000:,.2f}억원"
    return ""


def pct_value(value: Any) -> str:
    if isinstance(value, int | float):
        return f"{float(value):.2f}%"
    return ""


def rank_value(rank: Any, total: Any) -> str:
    if rank is None:
        return ""
    rank_text = str(rank)
    if "/" in rank_text:
        return rank_text
    if total is None:
        return rank_text
    return f"{rank_text}/{total}"


def number_value(value: Any) -> str:
    if isinstance(value, int | float):
        return f"{float(value):,.4f}".rstrip("0").rstrip(".")
    if value is None:
        return ""
    return str(value)


def source_label(source: str | None) -> str:
    key = "" if source is None else str(source)
    labels = {
        "cache": "UBIST",
        "metrics": "UBIST",
        "UBIST": "UBIST",
        "IQVIA": "IQVIA",
        "external_api": "외부 API",
        "hira_disease": "HIRA 질병정보서비스",
        "hira_procedure": "HIRA 진료행위정보서비스",
        "web_search": "웹 검색 결과(미검증)",
        "deep_analysis_events": "뉴스/이슈",
        "nedrug_mcp": "식약처 의약품 정보",
        "document": "업로드 문서",
        "none": "데이터 없음",
        "unsupported_brand": "전략 마트 원천 미확인",
    }
    return labels.get(key, key or "도구 결과")


def source_description(source: str | None) -> str:
    label = source_label(source)
    descriptions = {
        "UBIST": "매출·시장규모·점유율·순위 등 운영 지표",
        "IQVIA": "매출·시장규모·점유율·순위 등 운영 지표",
        "external_api": "ClinicalTrials, MFDS, OpenFDA 등 외부 조회 결과",
        "외부 API": "ClinicalTrials, MFDS, OpenFDA 등 외부 조회 결과",
        "hira_disease": "HIRA 질병정보서비스 KCD 기반 환자 통계",
        "HIRA 질병정보서비스": "KCD 기반 질병명 및 환자 통계",
        "hira_procedure": "HIRA 5단 행위코드 기준 진료행위 통계",
        "HIRA 진료행위정보서비스": "5단 행위코드 기준 진료행위 통계",
        "web_search": "웹 검색 결과 URL/snippet, 내부 fact 미승격",
        "웹 검색 결과(미검증)": "URL/snippet 기반 미검증 웹 검색 결과",
        "deep_analysis_events": "뉴스·이슈 분석 결과",
        "뉴스/이슈": "뉴스·이슈 분석 결과",
        "nedrug_mcp": "식약처 의약품 허가·성분·임상·특허 조회 결과",
        "식약처 의약품 정보": "식약처 의약품 허가·성분·임상·특허 조회 결과",
        "식약처 의약품 특허 정보": "식약처 의약품 특허 정보 조회 결과",
        "document": "사용자가 업로드한 문서 검색 결과",
        "업로드 문서": "사용자가 업로드한 문서 검색 결과",
        "none": "현재 POC가 보유하지 않은 데이터 영역",
        "데이터 없음": "현재 POC가 보유하지 않은 데이터 영역",
        "unsupported_brand": "현재 전략 마트 원천에서 브랜드 미확인",
        "지원 범위 밖": "현재 운영 지원 브랜드 목록 밖 질의",
    }
    return descriptions.get(label, descriptions.get(str(source or ""), "도구 결과"))


def source_labels(sources: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    labels: list[str] = []
    for source in sources:
        label = source_label(source)
        if label not in seen:
            labels.append(label)
            seen.add(label)
    return labels


def normalize_number(value: str) -> str:
    return re.sub(r"\s+", "", value.replace(",", ""))
