from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from jw_chat_agent_poc.service.context_scope import (
    explicit_file_comparison_sources,
    has_explicit_file_source_comparison,
)
from jw_chat_agent_poc.service.v4.contracts import PlannerOutput
from jw_chat_agent_poc.service.v4.lane_execution import (
    LaneExecutionRecord,
    source_notice_bindings_from_lane_execution,
)
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    DeterministicRender,
    EvidenceRecord,
    EvidenceSet,
    RenderNode,
    SourceReference,
)
from jw_chat_agent_poc.service.v4.render_common import table, text
from jw_chat_agent_poc.service.v4.source_tiers import source_tier

_SOURCE_LABELS = {
    "document": "업로드 파일",
    "mart": "내부 마트",
    "hira": "심평원",
}
_MONEY_FIELDS = ("sales_krw", "amount_krw", "total_cost", "cost")
_COUNT_FIELDS = ("patient_count", "patients", "count")


def render_file_source_comparison(
    plan: PlannerOutput,
    evidence_sets: Sequence[EvidenceSet],
    *,
    lane_execution: Mapping[str, LaneExecutionRecord] | None = None,
) -> DeterministicRender | None:
    """Render explicit file/source comparisons without combining populations."""

    question = plan.resolved_question
    comparison_sources = explicit_file_comparison_sources(question)
    if not has_explicit_file_source_comparison(question) or not comparison_sources:
        return None

    expected_sources = ("document", *comparison_sources)
    sets_by_source = {item.source: item for item in evidence_sets}
    rows: list[tuple[str, str, str, str, str, str]] = []
    record_ids: list[str] = []
    surface_fields: set[str] = {"source", "period", "measure", "identifier", "value"}
    partial_reasons: list[str] = []

    for source in expected_sources:
        evidence_set = sets_by_source.get(source)
        if evidence_set is None or not evidence_set.records:
            reason = _absence_reason(evidence_set)
            rows.append((_SOURCE_LABELS[source], "-", "조회 결과", "-", reason, "-"))
            partial_reasons.append(f"{source}: {reason}")
            continue
        for record in evidence_set.records:
            rows.append(_comparison_row(record))
            record_ids.append(record.evidence_id)
            surface_fields.update(record.payload)

    notices = [
        "요청한 자료원별 값을 병치하며 합산하지 않습니다.",
    ]
    same_source = _same_chso_csd_source(sets_by_source)
    if same_source:
        notices.append(
            "업로드 파일과 내부 마트가 같은 원천(CHSO/CSD sellout)으로 확인되어 "
            "합산하지 않고 각각 표시합니다."
        )
    else:
        definition_notice = _comparison_definition_notice(sets_by_source)
        if definition_notice:
            notices.append(definition_notice)
    conflict_notice = _comparison_conflict_notice(sets_by_source, expected_sources)
    if conflict_notice:
        notices.append(conflict_notice)

    table_text = table(
        ("출처", "기간", "지표", "식별자", "값", "단위·정의"),
        rows,
    )
    node = RenderNode(
        block_id="file-source-comparison:facts",
        record_ids=tuple(dict.fromkeys(record_ids)),
        surface_fields=tuple(sorted(surface_fields)),
        text="\n\n".join(("## 출처별 조회 결과", table_text, *notices)),
    )
    rendered_sets = tuple(
        sets_by_source[source]
        for source in expected_sources
        if source in sets_by_source
    )
    received = sum(item.coverage.records_received for item in rendered_sets)
    source_ids = {
        record.evidence_id
        for evidence_set in rendered_sets
        for record in evidence_set.records
    }
    coverage = CoverageLedger(
        total_reported=_total_reported(rendered_sets),
        records_received=received,
        records_unique=len(source_ids),
        records_rendered=len(set(record_ids)),
        pagination_complete=all(item.coverage.pagination_complete for item in rendered_sets),
        partial_reasons=tuple(
            dict.fromkeys(
                [
                    *(reason for item in rendered_sets for reason in item.coverage.partial_reasons),
                    *partial_reasons,
                ]
            )
        ),
    )
    source_notice_bindings = (
        source_notice_bindings_from_lane_execution(lane_execution)
        if lane_execution is not None
        else ()
    )
    return DeterministicRender(
        profile="market_analysis",
        text=node.text,
        nodes=(node,),
        coverage=coverage,
        source_refs=_source_refs(rendered_sets),
        required_fields=("source", "period", "measure", "identifier", "value"),
        record_surface_rate=(len(set(record_ids)) / len(source_ids) if source_ids else 1.0),
        required_field_surface_rate=1.0,
        source_tiers={source: source_tier(plan, source) for source in expected_sources},
        selection_by_source={
            source: {
                "records_received": (
                    sets_by_source[source].coverage.records_received
                    if source in sets_by_source
                    else 0
                ),
                "records_rendered": (
                    len(sets_by_source[source].records) if source in sets_by_source else 0
                ),
            }
            for source in expected_sources
        },
        source_notices=tuple(
            str(binding["notice"]) for binding in source_notice_bindings
        ),
        source_notice_bindings=source_notice_bindings,
    )


def _comparison_row(record: EvidenceRecord) -> tuple[str, str, str, str, str, str]:
    payload = _document_payload(record.payload) if record.source == "document" else record.payload
    source = _SOURCE_LABELS.get(record.source, record.source)
    period = _first_text(payload, "period", "period_ym", "year", "month") or _period_from_payload(payload)
    measure = _first_text(payload, "measure", "metric", "indicator", "title")
    identifier = _first_text(
        payload,
        "market_name",
        "brand",
        "sickCd",
        "sick_cd",
        "disease_code",
        "document_name",
        "file_name",
    )
    value, unit = _value_and_unit(payload)
    if record.source == "document":
        answer = _first_text(payload, "deterministic_answer", "content", "raw_body")
        exact_amount = _exact_won_amount(answer)
        value = exact_amount or answer or value
        measure = measure or "업로드 문서 조회값"
        unit = "원" if exact_amount else (_first_text(payload, "sheet_name") or unit)
    return (
        source,
        period or "원천 미제공",
        measure or "원천 미제공",
        identifier or "원천 미제공",
        value or "원천 미제공",
        unit or "원천 정의",
    )


def _value_and_unit(payload: Mapping[str, Any]) -> tuple[str, str]:
    for field in _MONEY_FIELDS:
        value = payload.get(field)
        if value not in (None, ""):
            return _format_number(value, suffix="원"), "원"
    for field in _COUNT_FIELDS:
        value = payload.get(field)
        if value not in (None, ""):
            return _format_number(value, suffix="명"), "명"
    for field in ("value", "amount", "total", "result"):
        value = payload.get(field)
        if value not in (None, ""):
            return str(value), _first_text(payload, "unit_label", "unit")
    return "", _first_text(payload, "unit_label", "unit")


def _format_number(value: object, *, suffix: str) -> str:
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return f"{value}{suffix}"
    rendered = f"{number:,.0f}" if number.is_integer() else f"{number:,.4f}".rstrip("0").rstrip(".")
    return f"{rendered}{suffix}"


def _exact_won_amount(value: str) -> str:
    matches = re.findall(r"(?<![\d,])(\d{1,3}(?:,\d{3})+)\s*원", value)
    return f"{matches[-1]}원" if matches else ""


def _same_chso_csd_source(sets_by_source: Mapping[str, EvidenceSet]) -> bool:
    document = sets_by_source.get("document")
    mart = sets_by_source.get("mart")
    if document is None or mart is None or not document.records or not mart.records:
        return False
    document_identity = _document_source_identity(document)
    if "chso" not in document_identity or "sellout" not in re.sub(r"\s+", "", document_identity):
        return False
    document_periods = _record_periods(document.records)
    for record in mart.records:
        payload = record.payload
        source_identity = _mart_source_identity(payload)
        measure = _first_text(payload, "measure", "metric").casefold()
        is_chso_source = any(
            token in source_identity for token in ("csd channel", "csd_channel", "chso")
        )
        if is_chso_source and any(token in measure for token in ("sales", "sellout", "sell out")):
            mart_period = _first_text(payload, "period", "period_ym", "year")
            if (
                not document_periods
                or not mart_period
                or _normalized_period(mart_period) in document_periods
            ):
                return True
    return False


def _comparison_definition_notice(
    sets_by_source: Mapping[str, EvidenceSet],
) -> str | None:
    document = sets_by_source.get("document")
    mart = sets_by_source.get("mart")
    if (
        document is None
        or mart is None
        or not document.records
        or not mart.records
        or "chso" not in _document_source_identity(document)
    ):
        return None
    identities = {
        identity
        for record in mart.records
        if (identity := _mart_source_identity(record.payload))
    }
    if not identities:
        return "자료원별 원천 정의의 일치 여부를 확인할 수 없어 합산할 수 없습니다."
    if not any(
        token in identity
        for identity in identities
        for token in ("csd channel", "csd_channel", "chso")
    ):
        return "자료원별 원천 정의가 달라 직접 비교할 수 없습니다."
    return None


def _document_source_identity(document: EvidenceSet) -> str:
    return " ".join(
        str(value)
        for record in document.records
        for key, value in _document_payload(record.payload).items()
        if key in {"document_name", "file_name", "sheet_name", "deterministic_answer"}
    ).casefold()


def _mart_source_identity(payload: Mapping[str, Any]) -> str:
    return " ".join(
        _first_text(payload, field)
        for field in ("source", "source_name", "_source_identity")
    ).casefold()


def _comparison_conflict_notice(
    sets_by_source: Mapping[str, EvidenceSet], expected_sources: Sequence[str]
) -> str | None:
    periods = {
        source: _record_periods(sets_by_source[source].records)
        for source in expected_sources
        if source in sets_by_source and sets_by_source[source].records
    }
    nonempty = [values for values in periods.values() if values]
    if len(nonempty) > 1 and not set.intersection(*nonempty):
        return "자료원별 기간이 달라 직접 비교할 수 없습니다."
    return None


def _record_periods(records: Sequence[EvidenceRecord]) -> set[str]:
    periods: set[str] = set()
    for record in records:
        payload = _document_payload(record.payload) if record.source == "document" else record.payload
        period = _first_text(payload, "period", "period_ym", "year")
        if not period:
            period = _period_from_payload(payload)
        if period:
            periods.add(_normalized_period(period))
    return periods


def _period_from_payload(payload: Mapping[str, Any]) -> str:
    haystack = " ".join(str(value) for value in payload.values())
    match = re.search(
        r"(?:20\d{2})(?:[-./]\s*|\s*년\s*)(?:0?[1-9]|1[0-2])(?:\s*월)?",
        haystack,
    )
    return match.group(0) if match else ""


def _normalized_period(value: str) -> str:
    match = re.search(r"(20\d{2})\D+(0?[1-9]|1[0-2])", value)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    return value.strip()


def _absence_reason(evidence_set: EvidenceSet | None) -> str:
    if evidence_set is None:
        return "실행 기록 없음"
    for failure in evidence_set.item_failures:
        for key in ("notice", "message", "reason", "status"):
            value = text(failure.get(key))
            if value:
                return value
    return "조건 일치 0건"


def _total_reported(evidence_sets: Sequence[EvidenceSet]) -> int | None:
    if not evidence_sets or any(item.coverage.total_reported is None for item in evidence_sets):
        return None
    return sum(item.coverage.total_reported or 0 for item in evidence_sets)


def _source_refs(evidence_sets: Sequence[EvidenceSet]) -> tuple[SourceReference, ...]:
    refs: dict[str, SourceReference] = {}
    for evidence_set in evidence_sets:
        for ref in evidence_set.source_refs:
            candidate = ref.model_copy(update={"source": evidence_set.source})
            current = refs.get(ref.url)
            if current is None or (not current.title and candidate.title):
                refs[ref.url] = candidate
    return tuple(refs.values())


def _first_text(payload: Mapping[str, Any], *fields: str) -> str:
    for field in fields:
        value = text(payload.get(field))
        if value:
            return value
    return ""


def _document_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = payload.get("records")
    if not isinstance(nested, (list, tuple)) or not nested:
        return payload
    first = nested[0]
    if not isinstance(first, Mapping):
        return payload
    return {**payload, **first}
