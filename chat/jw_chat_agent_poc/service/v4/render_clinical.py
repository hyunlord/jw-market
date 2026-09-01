from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

from jw_chat_agent_poc.service.v4.lossless_contracts import (
    EvidenceRecord,
    EvidenceSet,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.narrative_values import public_enum_value
from jw_chat_agent_poc.service.v4.render_common import (
    display,
    list_display,
    table,
    text,
)
from jw_chat_agent_poc.service.v4.temporal_analysis import clinical_time_axis

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
CLINICAL_DISPLAY_LIMIT = 10


def render_clinical(
    evidence_set: EvidenceSet,
    *,
    single: bool,
    observed_on: date | None = None,
) -> tuple[list[RenderNode], tuple[str, ...]]:
    reference_date = observed_on or datetime.now(UTC).date()
    records = list(evidence_set.records)
    selected, query_assignments = _selected_records(
        records,
        evidence_set.query_spec,
        single=single,
    )
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
        (
            "relevance_status",
            "직접 관련성",
            lambda payload: display(payload.get("relevance_status")),
        ),
        (
            "matched_query",
            "유래 질의",
            lambda payload: " · ".join(_query_values(payload.get("matched_query"))),
        ),
    )
    included = tuple(
        spec
        for spec in column_specs
        if spec[0] == "nct_id"
        or any(_has_value(record.payload.get(spec[0])) for record in selected)
    )
    rows_by_id = {
        record.evidence_id: tuple(
            render(record.payload) for _field, _header, render in included
        )
        for record in selected
    }
    table_title = "## 단일 임상시험 상세" if single else "## 임상시험 상세"
    notes = _selection_notes(
        evidence_set.query_spec,
        total=len(records),
        rendered=len(selected),
        single=single,
    )
    grouped_tables = _query_grouped_tables(
        selected,
        rows_by_id,
        query_assignments,
        evidence_set.query_spec,
        headers=tuple(header for _field, header, _render in included),
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
                grouped_tables,
            )
            if part
        ),
    )
    return [
        scope,
        *_statistics_nodes(evidence_set),
        _time_axis_node(evidence_set, reference_date),
        record_table,
    ], CLINICAL_REQUIRED_FIELDS


def _statistics_nodes(evidence_set: EvidenceSet) -> tuple[RenderNode, ...]:
    aggregate = next(
        (
            manifest
            for manifest in evidence_set.query_manifest
            if manifest.get("lane") == "surface_full_aggregate"
        ),
        None,
    )
    direct_records = _direct_distribution_records(evidence_set.records)
    population = (
        int(aggregate.get("direct_related_count"))
        if aggregate is not None
        and isinstance(aggregate.get("direct_related_count"), int)
        else len(direct_records)
    )
    status_counts = (
        _clinical_status_counts_from_mapping(aggregate.get("direct_status_counts"))
        if aggregate is not None
        else _clinical_status_counts_with_missing(direct_records)
    )
    phase_counts = (
        _clinical_phase_counts_from_mapping(aggregate.get("direct_phase_counts"))
        if aggregate is not None
        else _clinical_phase_counts_with_missing(direct_records)
    )
    raw_sponsor_counts = (
        aggregate.get("direct_sponsor_counts") if aggregate is not None else None
    )
    sponsor_counts = (
        Counter(
            {
                _distribution_label(str(label), dimension="sponsor"): int(count)
                for label, count in raw_sponsor_counts.items()
                if isinstance(label, str)
                and isinstance(count, int)
                and not isinstance(count, bool)
                and count > 0
            }
        )
        if isinstance(raw_sponsor_counts, Mapping)
        else Counter(
            text(record.payload.get("lead_sponsor") or record.payload.get("sponsor"))
            or "스폰서 미기재"
            for record in direct_records
        )
    )
    status_rows = _distribution_rows(status_counts, population, dimension="status")
    phase_rows = _distribution_rows(phase_counts, population, dimension="phase")
    sorted_sponsors = sorted(sponsor_counts.items(), key=lambda item: (-item[1], item[0]))
    displayed_sponsors = sorted_sponsors[:10]
    omitted_sponsors = sum(count for _label, count in sorted_sponsors[10:])
    if omitted_sponsors:
        displayed_sponsors.append(("기타", omitted_sponsors))
    sponsor_rows = _distribution_rows(displayed_sponsors, population, dimension="sponsor")
    sponsor_note = "주관 스폰서 기준"
    if omitted_sponsors:
        sponsor_note += f" · 상위 10 + 기타 {omitted_sponsors}건"
    record_ids = tuple(record.evidence_id for record in evidence_set.records)
    direct_definition = _direct_relevance_definition(evidence_set.query_spec)
    return (
        RenderNode(
            block_id="clinical:statistics:status",
            record_ids=record_ids,
            surface_fields=("overall_status",),
            text="\n".join(
                (
                    "## 임상시험 분포",
                    direct_definition,
                    "### 상태 분포",
                    f"직접 관련 {population}건 기준",
                    table(("항목", "건수", "비율"), status_rows),
                )
            ),
        ),
        RenderNode(
            block_id="clinical:statistics:phase",
            record_ids=record_ids,
            surface_fields=("phases",),
            text="\n".join(
                (
                    "### 단계 분포",
                    f"직접 관련 {population}건 기준",
                    table(("항목", "건수", "비율"), phase_rows),
                )
            ),
        ),
        RenderNode(
            block_id="clinical:statistics:sponsor",
            record_ids=record_ids,
            surface_fields=("sponsor",),
            text="\n".join(
                (
                    "### 주관 스폰서 상위",
                    f"직접 관련 {population}건 기준 · {sponsor_note}",
                    table(("항목", "건수", "비율"), sponsor_rows),
                )
            ),
        ),
    )


def _direct_distribution_records(
    records: Sequence[EvidenceRecord],
) -> tuple[EvidenceRecord, ...]:
    relevance_marked = any(
        text(record.payload.get("relevance_status")) for record in records
    )
    return tuple(
        record
        for record in records
        if not relevance_marked
        or record.payload.get("relevance_status") == "직접 관련 확인"
    )


def _direct_relevance_definition(query_spec: Sequence[str]) -> str:
    combined = " ".join(query_spec).upper()
    subject = "두 성분이" if " AND " in combined else "질의 성분이"
    return f"직접 관련 = {subject} 시험 약물(intervention)로 등재된 임상"


def _clinical_status_counts_with_missing(
    records: Sequence[EvidenceRecord],
) -> tuple[tuple[str, int], ...]:
    counts = dict(_clinical_status_counts(records))
    missing = sum(not text(record.payload.get("overall_status")) for record in records)
    if missing:
        counts["상태 미기재"] = missing
    return tuple(counts.items())


def _clinical_phase_counts_with_missing(
    records: Sequence[EvidenceRecord],
) -> tuple[tuple[str, int], ...]:
    counts: Counter[str] = Counter()
    for record in records:
        phases = _query_values(record.payload.get("phases"))
        label = " / ".join(
            public_enum_value(phase) for phase in phases
        ) or "단계 미기재"
        counts[label] += 1
    return tuple(counts.items())


def _distribution_label(label: str, *, dimension: str) -> str:
    if label != "__MISSING__":
        return label
    return {
        "status": "상태 미기재",
        "phase": "단계 미기재",
        "sponsor": "스폰서 미기재",
    }[dimension]


def _distribution_rows(
    counts: Sequence[tuple[str, int]],
    population: int,
    *,
    dimension: str,
) -> tuple[tuple[str, str, str], ...]:
    normalized = tuple(
        (_distribution_label(label, dimension=dimension), int(count))
        for label, count in counts
        if int(count) > 0
    )
    observed = sum(count for _label, count in normalized)
    if observed != population:
        raise ValueError(
            "clinical_distribution_population_mismatch:"
            f"dimension={dimension}:population={population}:observed={observed}"
        )
    return tuple(
        (
            label,
            str(count),
            f"{(count / population * 100):.2f}%" if population else "0.00%",
        )
        for label, count in normalized
    )


def _time_axis_node(evidence_set: EvidenceSet, observed_on: date) -> RenderNode:
    aggregate = next(
        (
            manifest
            for manifest in evidence_set.query_manifest
            if manifest.get("lane") == "surface_full_aggregate"
        ),
        None,
    )
    aggregate_axis = aggregate.get("temporal_axis") if aggregate is not None else None
    axis = (
        aggregate_axis
        if isinstance(aggregate_axis, Mapping)
        else clinical_time_axis(evidence_set.records, observed_on)
    )
    lines = ["## 임상시험 시간축", f"{observed_on.isoformat()} 기준"]
    completed_total = axis["completed_total"]
    recent_count = axis["recent_completed_count"]
    recent_ratio = axis["recent_completed_ratio_pct"]
    if completed_total:
        lines.append(
            f"완료 {completed_total}건 중 최근 3년 내 완료 {recent_count}건"
            + (f" ({_percentage_text(recent_ratio)})" if recent_ratio is not None else "")
        )
        latest = axis["latest_completed"]
        if latest:
            lines.append(
                f"최신 완료: {latest['title']} {str(latest['completion_date'])[:7]}"
            )
    latest_update = axis["latest_update"]
    if latest_update:
        lines.append(
            f"최신 갱신: {latest_update['title']} "
            f"{str(latest_update['last_update_date'])[:7]}"
        )
    progress_rows = tuple(
        (
            item["nct_id"],
            item["title"],
            item["status"],
            str(item["start_date"])[:7],
            str(item["primary_completion_date"])[:7],
            _percentage_text(item["progress_pct"]),
        )
        for item in axis["active_progress"]
    )
    if progress_rows:
        first_progress = axis["active_progress"][0]
        lines.append(
            f"{first_progress['title']}: {str(first_progress['start_date'])[:7]} 시작 → "
            f"{str(first_progress['primary_completion_date'])[:7]} 1차 완료 예정 · "
            f"경과율 {_percentage_text(first_progress['progress_pct'])}"
        )
    milestone_rows = tuple(
        (
            str(item["primary_completion_date"])[:7],
            item["nct_id"],
            item["title"],
            item["status"],
        )
        for item in axis["future_milestones"]
    )
    timeline_rows = tuple(
        ("진행 경과", nct_id, title, status, started, due, progress)
        for nct_id, title, status, started, due, progress in progress_rows
    ) + tuple(
        ("향후 이정표", nct_id, title, status, "-", due, "예정")
        for due, nct_id, title, status in milestone_rows
    )
    if timeline_rows:
        lines.extend(
            (
                "### 진행·향후 이정표",
                table(
                    (
                        "구분",
                        "NCT ID",
                        "시험명",
                        "상태",
                        "시작",
                        "1차 완료 예정",
                        "경과율",
                    ),
                    timeline_rows,
                ),
            )
        )
    partial = int(axis["imprecise_date_count"])
    missing = int(axis["missing_date_count"])
    if partial or missing:
        lines.append(f"날짜 판독 제한: 부분·불명확 {partial}개 · 원천 미제공 {missing}개")
    return RenderNode(
        block_id="clinical:time-axis",
        record_ids=tuple(record.evidence_id for record in evidence_set.records),
        surface_fields=(
            "start_date",
            "primary_completion_date",
            "completion_date",
            "last_update_date",
        ),
        text="\n".join(lines),
    )


def _percentage_text(value: object) -> str:
    numeric = float(value)
    return f"{numeric:.1f}%" if not numeric.is_integer() else f"{int(numeric)}%"


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
    direct_confirmed, direct_unconfirmed = _direct_relevance_counts(evidence_set)
    funnel_parts = [f"원천 검색 {total}건"]
    if coverage.records_after_status_filter is not None:
        funnel_parts.append(
            f"활성 상태 기준 {after_status}건 ({status_excluded}건 제외)"
        )
    funnel_parts.extend(
        (
            f"수신 {coverage.records_received}건",
            f"중복 제거 {coverage.records_unique}건",
            f"표시 대상 {relevant}건 (폐기 0건)",
            f"상세 표시 {rendered}건",
        )
    )
    if direct_confirmed is not None or direct_unconfirmed:
        funnel_parts.append(
            "직접 관련 확인 "
            f"{direct_confirmed or 0}건 · 직접 관련 여부 미확인 {direct_unconfirmed}건"
        )
    aggregate = next(
        (
            manifest
            for manifest in evidence_set.query_manifest
            if manifest.get("lane") == "surface_full_aggregate"
        ),
        None,
    )
    status_counts = (
        _clinical_status_counts_from_mapping(aggregate.get("status_counts"))
        if aggregate is not None
        else _clinical_status_counts(evidence_set.records)
    )
    phase_counts = (
        _clinical_phase_counts_from_mapping(aggregate.get("phase_counts"))
        if aggregate is not None
        else _clinical_phase_counts(evidence_set.records)
    )
    if status_counts:
        funnel_parts.append(
            "상태별 "
            + " · ".join(f"{label} {count}건" for label, count in status_counts)
        )
    if phase_counts:
        funnel_parts.append(
            "단계별 "
            + " · ".join(f"{label} {count}건" for label, count in phase_counts)
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


def _direct_relevance_counts(evidence_set: EvidenceSet) -> tuple[int | None, int]:
    statuses = [
        str(record.payload.get("relevance_status") or "").strip()
        for record in evidence_set.records
    ]
    if any(statuses):
        return (
            sum(status == "직접 관련 확인" for status in statuses),
            sum(status == "직접 관련 여부 미확인" for status in statuses),
        )

    confirmed = 0
    unconfirmed = 0
    found = False
    for manifest in evidence_set.query_manifest:
        confirmed_value = manifest.get("records_direct_relevance_confirmed")
        unconfirmed_value = manifest.get("records_direct_relevance_unconfirmed")
        if isinstance(confirmed_value, int) and not isinstance(confirmed_value, bool):
            confirmed += confirmed_value
            found = True
        if isinstance(unconfirmed_value, int) and not isinstance(unconfirmed_value, bool):
            unconfirmed += unconfirmed_value
            found = True
    return (confirmed if found else None), unconfirmed


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
    if single:
        return ""
    return f"수신 {total}건 중 대표 {rendered}건 표시"


def _selected_records(
    records: Sequence[EvidenceRecord],
    query_spec: Sequence[str],
    *,
    single: bool,
) -> tuple[list[EvidenceRecord], dict[str, str]]:
    if single:
        return list(records), {}
    joined = " ".join(query_spec).casefold()
    prefer_generic = any(token in joined for token in _GENERIC_SIGNALS)

    def sort_key(record: EvidenceRecord) -> tuple[int, int, int, str]:
        payload = record.payload
        status = text(payload.get("overall_status")).upper()
        generic_rank = 0 if prefer_generic and _is_generic_study(payload) else 1
        return (
            generic_rank,
            -_date_ordinal(payload.get("last_update_date")),
            _STATUS_PRIORITY.get(status, len(_STATUS_PRIORITY)),
            text(payload.get("nct_id")),
        )

    ranked = sorted(records, key=sort_key)
    queries = tuple(dict.fromkeys(query for query in query_spec if query.strip()))
    if len(ranked) <= CLINICAL_DISPLAY_LIMIT or len(queries) < 2:
        return ranked[:CLINICAL_DISPLAY_LIMIT], {
            record.evidence_id: _primary_query(record, queries) for record in ranked[:CLINICAL_DISPLAY_LIMIT]
        }

    queues = {
        query: [record for record in ranked if _record_matches_query(record, query)]
        for query in queries
    }
    positions = {query: 0 for query in queries}
    selected: list[EvidenceRecord] = []
    assignments: dict[str, str] = {}
    seen: set[str] = set()
    while len(selected) < CLINICAL_DISPLAY_LIMIT:
        progressed = False
        for query in queries:
            queue = queues[query]
            while positions[query] < len(queue):
                record = queue[positions[query]]
                positions[query] += 1
                if record.evidence_id in seen:
                    continue
                selected.append(record)
                seen.add(record.evidence_id)
                assignments[record.evidence_id] = query
                progressed = True
                break
            if len(selected) >= CLINICAL_DISPLAY_LIMIT:
                break
        if not progressed:
            break
    for record in ranked:
        if len(selected) >= CLINICAL_DISPLAY_LIMIT:
            break
        if record.evidence_id in seen:
            continue
        selected.append(record)
        seen.add(record.evidence_id)
        assignments[record.evidence_id] = _primary_query(record, queries)
    return selected, assignments


def _query_grouped_tables(
    records: Sequence[EvidenceRecord],
    rows_by_id: Mapping[str, tuple[str, ...]],
    assignments: Mapping[str, str],
    query_spec: Sequence[str],
    *,
    headers: tuple[str, ...],
    single: bool,
) -> str:
    if single or len(query_spec) < 2:
        return table(headers, tuple(rows_by_id[record.evidence_id] for record in records))
    queries = tuple(dict.fromkeys(query for query in query_spec if query.strip()))
    grouped = {
        query: [record for record in records if assignments.get(record.evidence_id) == query]
        for query in queries
    }
    counts = " · ".join(f"{query}: {len(grouped[query])}건" for query in queries)
    return "\n".join(
        (
            f"질의별 표시: {counts}",
            table(headers, tuple(rows_by_id[record.evidence_id] for record in records)),
        )
    )


def _query_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    return ()


def _record_matches_query(record: EvidenceRecord, query: str) -> bool:
    target = " ".join(query.split()).casefold()
    return any(" ".join(value.split()).casefold() == target for value in _query_values(record.payload.get("matched_query")))


def _primary_query(record: EvidenceRecord, queries: Sequence[str]) -> str:
    return next((query for query in queries if _record_matches_query(record, query)), queries[0] if queries else "기타")


def _clinical_status_counts(records: Sequence[EvidenceRecord]) -> tuple[tuple[str, int], ...]:
    buckets: Counter[str] = Counter()
    for record in records:
        status = text(record.payload.get("overall_status")).upper()
        if status == "COMPLETED":
            buckets["완료"] += 1
        elif status in ACTIVE_CLINICAL_STATUSES:
            buckets["모집중"] += 1
        elif status in {"TERMINATED", "WITHDRAWN", "SUSPENDED"}:
            buckets["중단"] += 1
        elif status:
            buckets[public_enum_value(status)] += 1
    order = ("완료", "모집중", "중단")
    return tuple((label, buckets[label]) for label in order if buckets[label]) + tuple(
        sorted((label, count) for label, count in buckets.items() if label not in order)
    )


def _clinical_status_counts_from_mapping(value: object) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, Mapping):
        return ()
    buckets: Counter[str] = Counter()
    for raw_status, count in value.items():
        if not isinstance(count, int) or count <= 0:
            continue
        status = text(raw_status).upper()
        if status == "__MISSING__":
            buckets["상태 미기재"] += count
        elif status == "COMPLETED":
            buckets["완료"] += count
        elif status in ACTIVE_CLINICAL_STATUSES:
            buckets["모집중"] += count
        elif status in {"TERMINATED", "WITHDRAWN", "SUSPENDED"}:
            buckets["중단"] += count
        elif status:
            buckets[public_enum_value(status)] += count
    order = ("완료", "모집중", "중단")
    return tuple((label, buckets[label]) for label in order if buckets[label]) + tuple(
        sorted((label, count) for label, count in buckets.items() if label not in order)
    )


def _clinical_phase_counts(records: Sequence[EvidenceRecord]) -> tuple[tuple[str, int], ...]:
    labels = {"PHASE1": "1상", "PHASE2": "2상", "PHASE3": "3상", "PHASE4": "4상"}
    counts: Counter[str] = Counter()
    for record in records:
        for phase in _query_values(record.payload.get("phases")):
            normalized = phase.upper().replace(" ", "")
            counts[labels.get(normalized, public_enum_value(phase))] += 1
    return tuple((label, counts[label]) for label in ("1상", "2상", "3상", "4상") if counts[label]) + tuple(
        sorted((label, count) for label, count in counts.items() if label not in {"1상", "2상", "3상", "4상"})
    )


def _clinical_phase_counts_from_mapping(value: object) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, Mapping):
        return ()
    labels = {"PHASE1": "1상", "PHASE2": "2상", "PHASE3": "3상", "PHASE4": "4상"}
    counts: Counter[str] = Counter()
    for raw_phase, count in value.items():
        if not isinstance(count, int) or count <= 0:
            continue
        phase = text(raw_phase)
        if phase == "__MISSING__":
            counts["단계 미기재"] += count
            continue
        if " / " in phase:
            label = " / ".join(
                labels.get(item, public_enum_value(item))
                for item in phase.split(" / ")
            )
            counts[label] += count
            continue
        normalized = phase.upper().replace(" ", "")
        counts[labels.get(normalized, public_enum_value(phase))] += count
    order = ("1상", "2상", "3상", "4상")
    return tuple((label, counts[label]) for label in order if counts[label]) + tuple(
        sorted((label, count) for label, count in counts.items() if label not in order)
    )


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
    return display(payload.get("nct_id"))


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
