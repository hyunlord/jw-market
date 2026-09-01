from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict


Domain = Literal["market", "regulatory", "clinical", "file", "meta"]
Operation = Literal["get_current", "get_trend", "compare", "explain", "breakdown"]
Presentation = Literal["text", "table", "chart", "chart_overlay", "portal_analysis"]


class IntentEntity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["brand", "market", "disease_code", "nct_id"]
    value: str


class IntentAxes(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    view: str | None = None
    source: str | None = None
    measure: str | None = None
    period: str | None = None


class IntentFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    domains: tuple[Domain, ...] = ()
    operations: tuple[Operation, ...] = ()
    entities: tuple[IntentEntity, ...] = ()
    axes: IntentAxes = IntentAxes()
    presentation: Presentation = "text"
    context_refs: tuple[str, ...] = ()


_DOMAIN_TERMS: tuple[tuple[Domain, tuple[str, ...]], ...] = (
    (
        "market",
        (
            "매출",
            "시장",
            "점유",
            "순위",
            "추이",
            "hhi",
            "성장 기여",
            "ubist",
            "iqvia",
            "dosage_unit",
        ),
    ),
    (
        "regulatory",
        (
            "허가",
            "식약처",
            "mfds",
            "급여",
            "보험",
            "hira",
            "환자수",
            "상병",
        ),
    ),
    ("clinical", ("임상", "clinical", "nct")),
    ("file", ("파일", "엑셀", "업로드", "시트", "컬럼", "셀")),
    ("meta", ("시장 정의", "정의 변경", "recode", "변경 사유", "분모 정의")),
)
_OPERATION_TERMS: tuple[tuple[Operation, tuple[str, ...]], ...] = (
    ("get_trend", ("추이", "시계열", "기간별", "변화")),
    ("compare", ("비교", "대비", "vs", "한번에", "같이")),
    ("explain", ("왜", "사유", "설명", "정의", "기준")),
    ("breakdown", ("분해", "채널별", "연령별", "성별", "지역별", "기관별")),
)
_KNOWN_BRANDS = ("리바로", "가드렛", "아일리아", "리피토")
_CONTEXT_REFS = ("그거", "그것", "이거", "이것", "해당", "앞서", "위에서")
_NCT_RE = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)
_DISEASE_CODE_RE = re.compile(r"\b[A-Z]\d{2}(?:\.\d|\d)?\b", re.IGNORECASE)
_MONTH_RE = re.compile(r"\b20\d{2}[-./](?:0?[1-9]|1[0-2])\b")
_YEAR_RE = re.compile(r"\b20\d{2}년?\b")


def extract_intent_frame(question: str) -> IntentFrame:
    """Extract conservative routing hints without replacing legacy classification."""

    if not isinstance(question, str):
        return IntentFrame()
    try:
        normalized = question.strip()
        lowered = normalized.casefold()
        domains = tuple(
            domain
            for domain, terms in _DOMAIN_TERMS
            if any(term.casefold() in lowered for term in terms)
        )
        operations = tuple(
            operation
            for operation, terms in _OPERATION_TERMS
            if any(term.casefold() in lowered for term in terms)
        )
        if not operations and normalized:
            operations = ("get_current",)
        entities = _entities(normalized)
        return IntentFrame(
            domains=domains,
            operations=operations,
            entities=entities,
            axes=_axes(normalized),
            presentation=_presentation(lowered),
            context_refs=tuple(term for term in _CONTEXT_REFS if term in normalized),
        )
    except Exception:
        return IntentFrame()


def _entities(question: str) -> tuple[IntentEntity, ...]:
    values: list[IntentEntity] = []
    values.extend(
        IntentEntity(kind="nct_id", value=match.group(0).upper())
        for match in _NCT_RE.finditer(question)
    )
    values.extend(
        IntentEntity(kind="disease_code", value=match.group(0).upper())
        for match in _DISEASE_CODE_RE.finditer(question)
    )
    values.extend(
        IntentEntity(kind="brand", value=brand)
        for brand in _KNOWN_BRANDS
        if brand in question
    )
    return tuple(dict.fromkeys(values))


def _axes(question: str) -> IntentAxes:
    lowered = question.casefold()
    period_match = _MONTH_RE.search(question) or _YEAR_RE.search(question)
    return IntentAxes(
        view=(
            "strategic"
            if "전략뷰" in lowered
            else "general"
            if "일반뷰" in lowered
            else None
        ),
        source=(
            "IQVIA"
            if "iqvia" in lowered
            else "UBIST"
            if "ubist" in lowered
            else None
        ),
        measure=_measure(lowered),
        period=period_match.group(0) if period_match else None,
    )


def _measure(lowered: str) -> str | None:
    for value, terms in (
        ("dosage_unit", ("dosage_unit", "dosage unit")),
        ("hhi", ("hhi", "집중도")),
        ("share", ("점유율", "점유")),
        ("rank", ("순위", "몇 등", "몇등")),
        ("sales", ("매출", "판매액", "금액")),
    ):
        if any(term in lowered for term in terms):
            return value
    return None


def _presentation(lowered: str) -> Presentation:
    if "원인분석" in lowered:
        return "portal_analysis"
    if any(term in lowered for term in ("오버레이", "중첩 차트", "겹쳐")):
        return "chart_overlay"
    if any(term in lowered for term in ("차트", "그래프", "시각화")):
        return "chart"
    if any(term in lowered for term in ("표로", "테이블")):
        return "table"
    return "text"
