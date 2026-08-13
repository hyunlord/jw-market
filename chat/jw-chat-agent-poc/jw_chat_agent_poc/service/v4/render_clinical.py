from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord, EvidenceSet, RenderNode
from jw_chat_agent_poc.service.v4.narrative_values import public_enum_value
from jw_chat_agent_poc.service.v4.render_common import display, list_display, table, text


CLINICAL_REQUIRED_FIELDS = (
    "nct_id",
    "brief_title",
    "overall_status",
    "phases",
    "sponsor",
    "last_update_date",
    "total_reported",
    "records_after_status_filter",
    "records_received",
    "records_unique",
    "records_relevant",
    "records_rendered",
)
ACTIVE_CLINICAL_STATUSES = {
    "RECRUITING",
    "ACTIVE_NOT_RECRUITING",
    "NOT_YET_RECRUITING",
}
_STATUS_PRIORITY = {
    "RECRUITING": 0,
    "NOT_YET_RECRUITING": 1,
    "ACTIVE_NOT_RECRUITING": 2,
}
_GENERIC_SIGNALS = ("제네릭", "생동", "generic", "bioequivalence")


def render_clinical(
    evidence_set: EvidenceSet,
    *,
    single: bool,
) -> tuple[list[RenderNode], tuple[str, ...]]:
    records = list(evidence_set.records)
    selected = _selected_records(records, evidence_set.query_spec, single=single)
    scope = RenderNode(
        block_id="clinical:coverage",
        surface_fields=(
            "total_reported",
            "records_after_status_filter",
            "records_received",
            "records_unique",
            "records_relevant",
            "records_rendered",
        ),
        text=_coverage_surface(evidence_set, rendered=len(selected), single=single),
    )

    column_specs = (
        ("nct_id", "NCT ID", _nct_link),
        (
            "brief_title",
            "간략 시험명",
            lambda payload: public_enum_value(display(payload.get("brief_title"))),
        ),
        (
            "overall_status",
            "상태",
            lambda payload: public_enum_value(payload.get("overall_status")),
        ),
        (
            "phases",
            "단계",
            lambda payload: _public_list(payload.get("phases")),
        ),
        ("sponsor", "스폰서", lambda payload: display(payload.get("sponsor"))),
        (
            "last_update_date",
            "최종 갱신일",
            lambda payload: display(payload.get("last_update_date")),
        ),
    )
    included = tuple(
        spec
        for spec in column_specs
        if spec[0] == "nct_id"
        or any(_has_value(record.payload.get(spec[0])) for record in selected)
    )
    rows = tuple(
        tuple(render(record.payload) for _field, _header, render in included)
        for record in selected
    )
    table_title = "## 단일 임상시험 상세" if single else "## 임상시험 상세"
    notes = _selection_notes(
        evidence_set.query_spec,
        total=len(records),
        rendered=len(selected),
        single=single,
    )
    record_table = RenderNode(
        block_id="clinical:records",
        record_ids=tuple(record.evidence_id for record in selected),
        surface_fields=tuple(field for field, _header, _render in included),
        text="\n".join(
            part
            for part in (
                table_title,
                notes,
                table(tuple(header for _field, header, _render in included), rows),
            )
            if part
        ),
    )
    detail_node = _record_detail_node(selected)
    return [scope, record_table, *([detail_node] if detail_node else [])], CLINICAL_REQUIRED_FIELDS


def _coverage_surface(
    evidence_set: EvidenceSet,
    *,
    rendered: int,
    single: bool,
) -> str:
    coverage = evidence_set.coverage
    total = _count(coverage.total_reported)
    after_status = _count(
        coverage.records_after_status_filter,
        fallback=coverage.records_received,
    )
    relevant = _count(coverage.records_relevant, fallback=len(evidence_set.records))
    status_excluded = _excluded(
        coverage.records_excluded_by_status,
        before=coverage.total_reported,
        after=coverage.records_after_status_filter,
    )
    relevance_excluded = _excluded(
        coverage.records_excluded_by_relevance,
        before=coverage.records_unique,
        after=coverage.records_relevant,
    )
    funnel_parts = [f"원천 검색 {total}건"]
    if coverage.records_after_status_filter is not None:
        funnel_parts.append(
            f"활성 상태 기준 {after_status}건 ({status_excluded}건 제외)"
        )
    funnel_parts.extend(
        (
            f"수신 {coverage.records_received}건",
            f"중복 제거 {coverage.records_unique}건",
            f"관련성 확인 {relevant}건 ({relevance_excluded}건 제외)",
            f"상세 표시 {rendered}건",
        )
    )
    lines = [
        "## 조사 범위와 완전성",
        _scope_statement(
            evidence_set.query_spec,
            single=single,
            status_filtered=coverage.records_after_status_filter is not None,
        ),
        " → ".join(funnel_parts),
    ]
    if coverage.partial_reasons:
        lines.append("부분 결과 사유: " + " / ".join(coverage.partial_reasons))
    if not coverage.pagination_complete:
        lines.append("페이지 수집이 완료되지 않아 전체 현황으로 볼 수 없습니다.")
    return "\n".join(lines)


def _scope_statement(
    query_spec: Sequence[str],
    *,
    single: bool,
    status_filtered: bool,
) -> str:
    if single:
        return "지정한 단일 임상시험 레코드의 상세 범위입니다."
    joined = " ".join(query_spec).casefold()
    if any(token in joined for token in ("과거", "완료", "종료", "historical", "completed")):
        return "완료·종료·철회 시험을 포함한 과거 시험 범위입니다."
    if status_filtered:
        return "진행 중·모집 중 시험 기준 (완료·종료 시험 제외)"
    return "질문에 지정된 임상시험 검색 범위입니다."


def _selection_notes(
    query_spec: Sequence[str],
    *,
    total: int,
    rendered: int,
    single: bool,
) -> str:
    return ""


def _selected_records(
    records: Sequence[EvidenceRecord],
    query_spec: Sequence[str],
    *,
    single: bool,
) -> list[EvidenceRecord]:
    if single:
        return list(records)
    joined = " ".join(query_spec).casefold()
    prefer_generic = any(token in joined for token in _GENERIC_SIGNALS)

    def sort_key(record: EvidenceRecord) -> tuple[int, int, int, str]:
        payload = record.payload
        status = text(payload.get("overall_status")).upper()
        generic_rank = 0 if prefer_generic and _is_generic_study(payload) else 1
        return (
            generic_rank,
            _STATUS_PRIORITY.get(status, len(_STATUS_PRIORITY)),
            -_date_ordinal(payload.get("last_update_date")),
            text(payload.get("nct_id")),
        )

    return sorted(records, key=sort_key)


def _record_detail_node(records: Sequence[EvidenceRecord]) -> RenderNode | None:
    sections: list[str] = []
    surface_fields: list[str] = []
    for record in records:
        payload = record.payload
        details = (
            ("적응증", "conditions", list_display(payload.get("conditions"), na="")),
            ("개입약물", "interventions", list_display(payload.get("interventions"), na="")),
            ("대상자수", "enrollment", _enrollment(payload.get("enrollment"))),
            ("협력기관", "collaborators", list_display(payload.get("collaborators"), na="")),
            ("국가", "countries", list_display(payload.get("countries"), na="")),
            ("기관", "facilities", list_display(payload.get("facilities"), na="")),
            ("1차 평가변수", "primary_outcomes", _outcome_text(payload.get("primary_outcomes"))),
            ("2차 평가변수", "secondary_outcomes", _outcome_text(payload.get("secondary_outcomes"))),
            ("간략 요약", "brief_summary", _bounded_text(payload.get("brief_summary"))),
            ("선정·제외 기준", "eligibility_criteria", _bounded_text(payload.get("eligibility_criteria"))),
            ("대상 성별", "sex", public_enum_value(payload.get("sex"))),
            ("연령", "minimum_age", _age_range(payload)),
            ("결과 게시", "has_results", _result_text(payload.get("has_results"))),
        )
        visible = [
            (label, field, value)
            for label, field, value in details
            if value and _detail_field_present(payload, field)
        ]
        if not visible:
            continue
        nct_id = display(payload.get("nct_id"))
        sections.append(
            "\n".join((f"### {nct_id} 조회 상세", *(f"- {label}: {value}" for label, _field, value in visible)))
        )
        surface_fields.extend(field for _label, field, _value in visible)
    if not sections:
        return None
    return RenderNode(
        block_id="clinical:record-details",
        record_ids=tuple(record.evidence_id for record in records),
        surface_fields=tuple(dict.fromkeys(surface_fields)),
        text="## 주요 임상시험 건별 상세\n" + "\n\n".join(sections),
    )


def _enrollment(value: object) -> str:
    if isinstance(value, Mapping):
        count = value.get("count")
        count_text = f"{text(count)}명" if count not in (None, "") else ""
        kind = public_enum_value(value.get("type")) if value.get("type") else ""
        kind_text = f"({kind})" if kind else ""
        return " ".join(part for part in (count_text, kind_text) if part)
    return text(value)


def _outcome_text(value: object) -> str:
    if not isinstance(value, list):
        return ""
    output = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        measure = text(item.get("measure"))
        time_frame = text(item.get("time_frame"))
        if measure:
            output.append(f"{measure} ({time_frame})" if time_frame else measure)
    return "; ".join(output)


def _bounded_text(value: object, limit: int = 1200) -> str:
    raw = text(value)
    return raw if len(raw) <= limit else raw[:limit] + "… [원문 있음]"


def _detail_field_present(payload: Mapping[str, object], field: str) -> bool:
    if field == "minimum_age":
        return _has_value(payload.get("minimum_age")) or _has_value(
            payload.get("maximum_age")
        )
    return _has_value(payload.get(field))


def _age_range(payload: Mapping[str, object]) -> str:
    return " ~ ".join(
        part for part in (text(payload.get("minimum_age")), text(payload.get("maximum_age"))) if part
    )


def _result_text(value: object) -> str:
    if value is True:
        return "게시됨"
    if value is False:
        return "미게시"
    return ""


def _public_list(value: object) -> str:
    if not isinstance(value, (list, tuple, set)):
        return public_enum_value(value)
    return ", ".join(public_enum_value(item) for item in value)


def _is_generic_study(payload: Mapping[str, object]) -> bool:
    surface = " ".join(
        (
            text(payload.get("brief_title")),
            text(payload.get("official_title")),
            list_display(payload.get("interventions"), na=""),
        )
    ).casefold()
    return any(token in surface for token in _GENERIC_SIGNALS)


def _nct_link(payload: Mapping[str, object]) -> str:
    nct_id = display(payload.get("nct_id"))
    url = text(payload.get("url"))
    return f"[{nct_id}]({url})" if url else nct_id


def _has_value(value: object) -> bool:
    if value in (None, ""):
        return False
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _count(value: int | None, *, fallback: int | None = None) -> str:
    resolved = value if value is not None else fallback
    return str(resolved) if resolved is not None else "확인 불가"


def _excluded(
    value: int | None,
    *,
    before: int | None,
    after: int | None,
) -> str:
    if value is not None:
        return str(value)
    if before is not None and after is not None:
        return str(max(before - after, 0))
    return "확인 불가"


def _date_ordinal(value: object) -> int:
    raw = text(value)
    try:
        return date.fromisoformat(raw[:10]).toordinal()
    except ValueError:
        return 0
