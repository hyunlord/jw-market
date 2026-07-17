from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from jw_chat_agent_poc.orchestrator.provenance_labels import provenance_source_block
from jw_chat_agent_poc.service.conversation import (
    ConversationSlots,
    ConversationTurn,
    RankedBrandSlot,
    SeriesPoint,
)


_FIRST_RANK_RE = re.compile(r"그중\s*1위(?:\s*브랜드)?")
_ANCHOR_RE = re.compile(r"그\s*브랜드")
_PERIOD_RE = re.compile(r"같은\s*기간")
_MARKET_RE = re.compile(r"(?:방금|이|해당|그)\s*시장")
_REFERENCE_RES = (_FIRST_RANK_RE, _ANCHOR_RE, _PERIOD_RE, _MARKET_RE)
_CONTRAST_FOLLOWUP_RE = re.compile(
    r"^\s*(?:그럼|그러면|그렇다면)\s+(?P<brand>[가-힣A-Za-z0-9_-]{2,30}?)(?:은|는|이|가)?\s*[?!.]?\s*$",
    re.IGNORECASE,
)
_NON_BRAND_CONTRAST_TARGET_RE = re.compile(
    r"^(?:\d{1,2}(?:월|분기)|20\d{2}년|매출|점유율|순위|시장|성분|임상|허가|부작용|환자수|그건|이건|저건)$",
    re.IGNORECASE,
)
_INHERITABLE_INTENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"매출\s*(?:경향성|추이|흐름|변화)"), "매출 추이"),
    (re.compile(r"점유율\s*(?:경향성|추이|흐름|변화)"), "점유율 추이"),
    (re.compile(r"경쟁\s*구도"), "경쟁구도"),
    (re.compile(r"임상\s*(?:시험|실험)"), "임상시험"),
    (re.compile(r"허가\s*(?:일|날짜|현황|정보)"), "허가 현황"),
    (re.compile(r"부작용|이상반응"), "부작용"),
    (re.compile(r"성분"), "성분"),
    (re.compile(r"환자\s*수"), "환자수"),
    (re.compile(r"매출"), "매출"),
    (re.compile(r"점유율"), "점유율"),
    (re.compile(r"순위"), "순위"),
)


@dataclass(frozen=True, slots=True)
class AnaphoraResolution:
    resolved_question: str
    brand: str | None = None
    reusable_ranked: RankedBrandSlot | None = None
    unresolved_reference: bool = False
    interpretation_notice: str | None = None


def extract_conversation_slots(result: dict[str, Any]) -> ConversationSlots:
    resolution = result.get("resolution")
    anchor = str(resolution.get("canonical_brand") or "").strip() if isinstance(resolution, dict) else ""
    market = ""
    market_definition = ""
    period = ""
    denominator = ""
    ranked: tuple[RankedBrandSlot, ...] = ()
    ranked_names: tuple[str, ...] = ()
    file_name = ""
    file_measure = ""
    file_manufacturer = ""
    file_sheet = ""

    deterministic_file_answer = str(result.get("deterministic_file_answer") or "")
    if deterministic_file_answer:
        file_match = re.search(r"^파일:\s*(.+)$", deterministic_file_answer, re.MULTILINE)
        measure_match = re.search(
            r"^사용 열:\s*(.+?)(?=\n집계 함수:|\n적용 행 수:|\Z)",
            deterministic_file_answer,
            re.MULTILINE | re.DOTALL,
        )
        manufacturer_match = re.search(
            r"^필터 조건:\s*[^\n=]+?=\s*'([^']+)'\s*$",
            deterministic_file_answer,
            re.MULTILINE,
        )
        sheet_match = re.search(
            r"^시트(?:·테이블명)?:\s*(.+?)(?:\s*/\s*data)?\s*$",
            deterministic_file_answer,
            re.MULTILINE,
        )
        file_name = file_match.group(1).strip() if file_match else ""
        file_measure = (
            " ".join(measure_match.group(1).split(",", maxsplit=1)[0].split())
            if measure_match
            else ""
        )
        file_manufacturer = manufacturer_match.group(1).strip() if manufacturer_match else ""
        file_sheet = sheet_match.group(1).strip() if sheet_match else ""

    for call in result.get("tool_calls", []):
        if not isinstance(call, dict):
            continue
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        anchor = anchor or _text(data.get("anchor_brand") or data.get("brand"))
        market = market or _text(data.get("market_id") or data.get("market_name") or data.get("view_source_id"))
        market_definition = market_definition or _text(
            data.get("market_definition_full") or data.get("market_definition_label") or data.get("market_name")
        )
        period = period or _text(data.get("period"))
        denominator = denominator or _denominator(data)
        if not ranked:
            ranked = _ranked_slots(data.get("level_top5_trend_series"))
        if not ranked_names:
            ranked_names = tuple(item.brand for item in ranked) or _segment_names(data.get("level_segments"))

    if not period and ranked:
        period = next((item.series[-1].period for item in ranked if item.series), "")
    return ConversationSlots(
        anchor_brand=anchor or None,
        market=market or None,
        market_definition=market_definition or None,
        period=period or None,
        denominator=denominator or None,
        ranked_brands=ranked_names,
        ranked=ranked,
        file_name=file_name or None,
        file_measure=file_measure or None,
        file_manufacturer=file_manufacturer or None,
        file_sheet=file_sheet or None,
    )


def resolve_anaphora(question: str, previous_turn: ConversationTurn | None) -> AnaphoraResolution:
    contrast = _CONTRAST_FOLLOWUP_RE.match(question)
    if contrast is not None:
        brand = contrast.group("brand")
        if _NON_BRAND_CONTRAST_TARGET_RE.fullmatch(brand):
            return AnaphoraResolution(resolved_question=question, unresolved_reference=True)
        if previous_turn is None:
            return AnaphoraResolution(resolved_question=question, unresolved_reference=True)
        intent = _inheritable_intent(previous_turn.question)
        if not intent:
            return AnaphoraResolution(resolved_question=question, unresolved_reference=True)
        return AnaphoraResolution(
            resolved_question=f"{brand} {intent}는?",
            brand=brand,
            interpretation_notice=f"{brand}의 {intent}로 이해했어요.",
        )
    if not any(pattern.search(question) for pattern in _REFERENCE_RES):
        return AnaphoraResolution(resolved_question=question)
    if previous_turn is None:
        return AnaphoraResolution(resolved_question=question, unresolved_reference=True)

    slots = previous_turn.slots
    resolved = question
    brand: str | None = None
    reusable: RankedBrandSlot | None = None

    if _FIRST_RANK_RE.search(resolved):
        if not slots.ranked_brands:
            return AnaphoraResolution(resolved_question=question, unresolved_reference=True)
        brand = slots.ranked_brands[0]
        reusable = next((item for item in slots.ranked if item.brand == brand and item.series), None)
        resolved = _FIRST_RANK_RE.sub(brand, resolved)
        if reusable is None and slots.anchor_brand and slots.anchor_brand != brand:
            resolved = f"{slots.anchor_brand} 시장의 {resolved}"
    if _ANCHOR_RE.search(resolved):
        brand = brand or slots.anchor_brand
        if not brand:
            return AnaphoraResolution(resolved_question=question, unresolved_reference=True)
        resolved = _ANCHOR_RE.sub(brand, resolved)
    if _PERIOD_RE.search(resolved):
        if not slots.period:
            return AnaphoraResolution(resolved_question=question, unresolved_reference=True)
        resolved = _PERIOD_RE.sub(slots.period, resolved)
    if _MARKET_RE.search(resolved):
        if not slots.market:
            return AnaphoraResolution(resolved_question=question, unresolved_reference=True)
        market_hint = f"{slots.anchor_brand} 시장" if slots.anchor_brand else slots.market
        resolved = _MARKET_RE.sub(market_hint, resolved)
    return AnaphoraResolution(resolved_question=resolved, brand=brand, reusable_ranked=reusable)


def _inheritable_intent(question: str) -> str:
    for pattern, intent in _INHERITABLE_INTENTS:
        if pattern.search(question):
            return intent
    return ""


def reused_context_result(
    question: str,
    ranked: RankedBrandSlot,
    inherited_slots: ConversationSlots | None = None,
) -> dict[str, Any]:
    series = [_point_dict(point) for point in ranked.series]
    fact_md = _series_fact_markdown(ranked)
    inherited = inherited_slots or ConversationSlots()
    render_data: dict[str, Any] = {
        "brand": ranked.brand,
        "metric": "series",
        "rank": ranked.rank,
        "period": ranked.series[-1].period,
        "brand_value_series_10pt": series,
        "context_role": "previous_turn_verified_fact",
    }
    if inherited.market:
        render_data["market_id"] = inherited.market
    if inherited.market_definition:
        render_data["market_definition_full"] = inherited.market_definition
    if inherited.denominator:
        render_data["inherited_denominator"] = inherited.denominator
    call = {
        "source": "conversation_context",
        "tool": "conversation_context",
        "summary_text": f"직전 턴에서 확인한 {ranked.brand} 시계열을 재사용했습니다.",
        "render_data": render_data,
    }
    answer = _series_answer(ranked, provenance_source_block((call,), ("conversation_context",)))
    return {
        "question": question,
        "resolution": {"canonical_brand": ranked.brand, "scope": "previous_turn_verified_fact"},
        "answer": answer,
        "markdown_response": {"markdown": answer, "fact_md": fact_md, "data_md": fact_md},
        "sources": ["conversation_context"],
        "tool_calls": [call],
        "context_fact_reused": True,
    }


def unresolved_reference_result(question: str) -> dict[str, Any]:
    return {
        "question": question,
        "answer": "직전 대화에서 가리키는 대상을 확인할 수 없습니다. 어느 브랜드나 시장을 뜻하는지 명시해 주세요.",
        "sources": [],
        "tool_calls": [],
        "conversation_reference_unresolved": True,
    }


def _ranked_slots(value: Any) -> tuple[RankedBrandSlot, ...]:
    if not isinstance(value, list):
        return ()
    rows: list[RankedBrandSlot] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        brand = _text(item.get("brand") or item.get("name"))
        if not brand:
            continue
        rows.append(RankedBrandSlot(brand=brand, rank=_integer(item.get("rank")), series=_series(item.get("series"))))
    return tuple(sorted(rows, key=lambda item: (item.rank is None, item.rank or 0, item.brand)))


def _series(value: Any) -> tuple[SeriesPoint, ...]:
    if not isinstance(value, list):
        return ()
    points: list[SeriesPoint] = []
    for item in value:
        if not isinstance(item, dict) or not _text(item.get("period")):
            continue
        points.append(
            SeriesPoint(
                period=_text(item.get("period")),
                value_krw=_number(item.get("value_krw")),
                ms_pct=_number(item.get("ms_pct")),
                rank=_integer(item.get("rank")),
            )
        )
    return tuple(points)


def _segment_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        name
        for item in value
        if isinstance(item, dict) and (name := _text(item.get("brand") or item.get("name")))
    )


def _denominator(data: dict[str, Any]) -> str:
    inherited = _text(data.get("inherited_denominator"))
    if inherited:
        return inherited
    if (value := _number(data.get("market_size_억원"))) is not None:
        return f"{value:g}억원"
    if (value := _number(data.get("market_size_recent_krw") or data.get("market_size_krw"))) is not None:
        return f"{value / 100_000_000:g}억원"
    return ""


def _point_dict(point: SeriesPoint) -> dict[str, Any]:
    return {
        key: value
        for key, value in (("period", point.period), ("value_krw", point.value_krw), ("ms_pct", point.ms_pct), ("rank", point.rank))
        if value is not None
    }


def _series_fact_markdown(ranked: RankedBrandSlot) -> str:
    rows = "\n".join(
        f"| {point.period} | {_eok(point.value_krw)} | {_pct(point.ms_pct)} |"
        for point in ranked.series
    )
    return f"### {ranked.brand} 매출 시계열 fact\n| 기간 | 매출 | MS |\n| --- | --- | --- |\n{rows}"


def _series_answer(ranked: RankedBrandSlot, source_block: str) -> str:
    first, latest = ranked.series[0], ranked.series[-1]
    delta = None if first.ms_pct is None or latest.ms_pct is None else latest.ms_pct - first.ms_pct
    delta_text = "" if delta is None else f" ({delta:+.2f}%p)"
    rows = "\n".join(f"| {point.period} | {_eok(point.value_krw)} | {_pct(point.ms_pct)} |" for point in ranked.series)
    return (
        f"{ranked.brand}의 시장점유율은 {first.period} {_pct(first.ms_pct)}에서 "
        f"{latest.period} {_pct(latest.ms_pct)}로 변했습니다{delta_text}.\n\n"
        f"| 기간 | 매출 | 시장점유율 |\n| --- | ---: | ---: |\n{rows}\n\n"
        + source_block
    )


def _eok(value: float | None) -> str:
    return "-" if value is None else f"{value / 100_000_000:,.2f}억원"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}%"


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _integer(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) else None
