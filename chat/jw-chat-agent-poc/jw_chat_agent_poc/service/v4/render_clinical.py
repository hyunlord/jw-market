from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import date

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord, EvidenceSet, RenderNode
from jw_chat_agent_poc.service.v4.render_common import (
    coverage_text,
    display,
    enrollment_display,
    list_display,
    results_display,
    table,
    text,
)


CLINICAL_REQUIRED_FIELDS = (
    "nct_id",
    "phases",
    "overall_status",
    "interventions",
    "start_date",
    "total_reported",
    "records_received",
    "records_unique",
    "records_rendered",
)
ACTIVE_CLINICAL_STATUSES = {
    "RECRUITING",
    "ACTIVE_NOT_RECRUITING",
    "ENROLLING_BY_INVITATION",
    "NOT_YET_RECRUITING",
}


def render_clinical(
    evidence_set: EvidenceSet,
    *,
    single: bool,
) -> tuple[list[RenderNode], tuple[str, ...]]:
    records = list(evidence_set.records)
    coverage = evidence_set.coverage
    funnel = coverage_text(coverage, rendered=len(records))
    scope_lines = ["## 조사 범위와 완전성", funnel]
    if coverage.partial_reasons:
        scope_lines.append("부분 결과 사유: " + " / ".join(coverage.partial_reasons))
    if not coverage.pagination_complete:
        scope_lines.append("페이지 수집이 완료되지 않아 전체 현황으로 볼 수 없습니다.")
    scope = RenderNode(
        block_id="clinical:coverage",
        surface_fields=(
            "total_reported",
            "records_received",
            "records_unique",
            "records_rendered",
        ),
        text="\n".join(scope_lines),
    )

    status_counts: Counter[tuple[str, str]] = Counter()
    sponsor_groups: dict[str, list[str]] = defaultdict(list)
    for record in records:
        payload = record.payload
        phases = list_display(payload.get("phases"))
        status = display(payload.get("overall_status"))
        status_counts[(phases, status)] += 1
        sponsor = display(payload.get("sponsor"))
        sponsor_groups[sponsor].append(display(payload.get("nct_id")))
    summary_rows = [
        [phase, status, str(count)]
        for (phase, status), count in sorted(status_counts.items())
    ]
    summary = RenderNode(
        block_id="clinical:phase-status",
        record_ids=tuple(record.evidence_id for record in records),
        surface_fields=("phases", "overall_status"),
        text="## 단계 및 상태 집계\n" + table(("단계", "상태", "건수"), summary_rows),
    )
    group_rows = [[sponsor, ", ".join(ids)] for sponsor, ids in sorted(sponsor_groups.items())]
    groups = RenderNode(
        block_id="clinical:sponsor-groups",
        record_ids=tuple(record.evidence_id for record in records),
        surface_fields=("sponsor", "nct_id"),
        text="## 회사 및 제품별 그룹\n" + table(("스폰서", "시험"), group_rows),
    )

    headers = (
        "NCT ID",
        "유형",
        "단계",
        "상태",
        "개입",
        "대조",
        "스폰서",
        "시작일",
        "1차 완료일",
        "완료일",
        "결과",
    )
    rows = []
    for record in records:
        payload = record.payload
        nct_id = display(payload.get("nct_id"))
        url = text(payload.get("url"))
        rows.append(
            [
                f"[{nct_id}]({url})" if url else nct_id,
                display(payload.get("study_type")),
                list_display(payload.get("phases")),
                display(payload.get("overall_status")),
                list_display(payload.get("interventions")),
                list_display(payload.get("comparators"), na="해당 없음(N/A)"),
                display(payload.get("sponsor")),
                display(payload.get("start_date")),
                display(payload.get("primary_completion_date")),
                display(payload.get("completion_date")),
                results_display(payload.get("has_results")),
            ]
        )
    table_title = "## 단일 임상시험 상세" if single else "## 임상시험 전건"
    record_table = RenderNode(
        block_id="clinical:records",
        record_ids=tuple(record.evidence_id for record in records),
        surface_fields=(
            "nct_id",
            "study_type",
            "phases",
            "overall_status",
            "interventions",
            "comparators",
            "sponsor",
            "start_date",
            "primary_completion_date",
            "completion_date",
            "has_results",
        ),
        text=f"{table_title}\n{table(headers, rows)}",
    )
    nodes = [scope, summary, groups, record_table]
    card_records = _major_clinical_records(records)
    if card_records:
        cards = []
        for record in card_records:
            payload = record.payload
            nct_id = display(payload.get("nct_id"))
            cards.append(
                "\n".join(
                    (
                        f"### {nct_id}",
                        f"- 공식 제목: {display(payload.get('official_title'))}",
                        f"- 간략 제목: {display(payload.get('brief_title'))}",
                        f"- 질환: {list_display(payload.get('conditions'))}",
                        f"- 등록: {enrollment_display(payload.get('enrollment'))}",
                        f"- 국가: {list_display(payload.get('countries'))}",
                        f"- 최종 갱신일: {display(payload.get('last_update_date'))}",
                    )
                )
            )
        nodes.append(
            RenderNode(
                block_id="clinical:cards",
                record_ids=tuple(record.evidence_id for record in card_records),
                surface_fields=(
                    "official_title",
                    "brief_title",
                    "conditions",
                    "enrollment",
                    "countries",
                    "last_update_date",
                ),
                text=(
                    (
                        "## 건별 상세\n"
                        if len(records) <= 12
                        else (
                            f"## 주요 임상시험 건별 상세 ({len(card_records)}건)\n"
                            "활성 상태, 결과 게시, 최종 갱신일 순으로 선정했습니다.\n\n"
                        )
                    )
                    + "\n\n".join(cards)
                ),
            )
        )
    return nodes, CLINICAL_REQUIRED_FIELDS


def _major_clinical_records(records: Sequence[EvidenceRecord]) -> list[EvidenceRecord]:
    if len(records) <= 12:
        return list(records)

    def sort_key(record: EvidenceRecord) -> tuple[int, int, int, str]:
        payload = record.payload
        status = text(payload.get("overall_status")).upper()
        return (
            0 if status in ACTIVE_CLINICAL_STATUSES else 1,
            0 if payload.get("has_results") is True else 1,
            -_date_ordinal(payload.get("last_update_date")),
            text(payload.get("nct_id")),
        )

    return sorted(records, key=sort_key)[:12]


def _date_ordinal(value: object) -> int:
    raw = text(value)
    try:
        return date.fromisoformat(raw[:10]).toordinal()
    except ValueError:
        return 0
