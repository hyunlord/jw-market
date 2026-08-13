from __future__ import annotations

from collections import defaultdict
from datetime import date

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord, EvidenceSet, RenderNode
from jw_chat_agent_poc.service.v4.patent import patent_record_sort_key
from jw_chat_agent_poc.service.v4.render_common import display, link, table, text


PATENT_REQUIRED_FIELDS = (
    "patent_no",
    "invention_title",
    "patent_type",
    "listed_status",
    "expiration_date",
    "jurisdiction",
    "as_of_date",
)
MAX_DOMESTIC_PATENT_ROWS = 2_147_483_647  # Compatibility export; rendering is uncapped.


def render_patent(
    evidence_set: EvidenceSet,
    observed_on: date,
) -> tuple[list[RenderNode], tuple[str, ...]]:
    by_lane: dict[str, list[EvidenceRecord]] = defaultdict(list)
    for record in evidence_set.records:
        by_lane[text(record.payload.get("lane"))].append(record)
    kr_records = sorted(
        by_lane["kr_primary"],
        key=lambda record: patent_record_sort_key(record.payload),
    )
    selected_kr = kr_records
    nodes: list[RenderNode] = [
        RenderNode(
            block_id="patent:coverage",
            surface_fields=("records_received", "records_unique", "records_rendered"),
            text=_coverage_surface(
                evidence_set,
                rendered=len(selected_kr),
            ),
        )
    ]
    kr_rows = []
    for record in selected_kr:
        payload = record.payload
        status = display(payload.get("status"))
        expiration = display(payload.get("expiration_date"))
        kr_rows.append(
            [
                display(payload.get("product")),
                display(payload.get("ingredient")),
                display(payload.get("patent_no")),
                display(payload.get("invention_title")),
                display(payload.get("patent_type")),
                status,
                expiration,
                display(payload.get("owner")),
                (
                    f"{observed_on.isoformat()} 조회 기준 NeDrug 특허목록상 상태 "
                    f"'{status}' · 등재목록상 소멸일 {expiration}"
                ),
            ]
        )
    registered_count = sum(
        text(record.payload.get("status")) == "등록" for record in kr_records
    )
    selection_note = (
        "국내 특허는 등록 우선, 등재목록상 소멸일 내림차순으로 표시합니다."
    )
    status_note = (
        f"등록 상태 {registered_count}건을 먼저 표시합니다."
        if registered_count
        else ""
    )
    nodes.append(
        RenderNode(
            block_id="patent:kr-primary",
            record_ids=tuple(record.evidence_id for record in selected_kr),
            surface_fields=(
                (
                    "patent_no",
                    "invention_title",
                    "patent_type",
                    "listed_status",
                    "expiration_date",
                    "jurisdiction",
                    "as_of_date",
                )
                if selected_kr
                else ()
            ),
            text=(
                "## 국내 NeDrug 특허목록 정본\n"
                + "\n".join(part for part in (status_note, selection_note) if part)
                + "\n"
                + table(
                    ("제품", "성분", "특허번호", "발명명", "특허구분", "목록상 상태", "등재목록상 소멸일", "권리자", "판독"),
                    kr_rows,
                )
            ),
        )
    )
    us_rows = [
        [
            f"{observed_on.isoformat()} 조회 기준",
            display(record.payload.get("product")),
            display(record.payload.get("ingredient")),
            display(record.payload.get("patent_no")),
            display(record.payload.get("invention_title")),
            display(record.payload.get("status")),
            display(record.payload.get("expiration_date")),
            display(record.payload.get("owner")),
        ]
        for record in by_lane["us_secondary"]
    ]
    nodes.append(
        RenderNode(
            block_id="patent:us-secondary",
            record_ids=tuple(record.evidence_id for record in by_lane["us_secondary"]),
            surface_fields=(
                (
                    "patent_no",
                    "invention_title",
                    "listed_status",
                    "expiration_date",
                    "jurisdiction",
                    "as_of_date",
                )
                if by_lane["us_secondary"]
                else ()
            ),
            text="## 미국 Orange Book 보조표\n"
            + table(
                (
                    "조회 기준",
                    "제품",
                    "성분",
                    "미국 특허번호",
                    "발명명",
                    "등재 상태",
                    "만료일",
                    "권리자",
                ),
                us_rows,
            ),
        )
    )
    news_rows = [
        [
            display(record.payload.get("event_date")),
            display(record.payload.get("published_at")),
            link(record.payload),
            display(record.payload.get("snippet")),
        ]
        for record in by_lane["news"]
    ]
    nodes.append(
        RenderNode(
            block_id="patent:news",
            record_ids=tuple(record.evidence_id for record in by_lane["news"]),
            surface_fields=("event_date", "published_at", "title", "url"),
            text=(
                "## 뉴스 맥락\n"
                + table(("사건일", "게시일", "보도", "맥락"), news_rows)
                + "\n\n뉴스는 보도 맥락이며 국내 정본을 덮어쓰지 않습니다. 최종 확정은 공식 목록에서 별도 확인합니다."
            ),
        )
    )
    nodes.append(
        RenderNode(
            block_id="patent:limits",
            text=(
                "## 해석 상한\n"
                + (
                    "식약처 등재목록 API만으로 특허 만료 예정일을 확인할 수 없습니다. "
                    if _asks_for_expiry_forecast(evidence_set)
                    else ""
                )
                + "무효로 소멸한 특허의 등재목록상 소멸일은 원 존속기간과 다를 수 있습니다. "
                "국내 목록과 미국 Orange Book 날짜를 합산하지 않으며, 이 정보만으로 후발 제품 출시 가능성을 단정하지 않습니다."
            ),
        )
    )
    return nodes, PATENT_REQUIRED_FIELDS


def _coverage_surface(evidence_set: EvidenceSet, *, rendered: int) -> str:
    manifest = next(
        (
            item
            for item in evidence_set.query_manifest
            if text(item.get("lane")) == "kr_primary"
        ),
        {},
    )
    received = manifest.get("records_received", evidence_set.coverage.records_received)
    unique = manifest.get("records_unique", evidence_set.coverage.records_unique)
    product_patent_rows = manifest.get("product_patent_rows", unique)
    lines = [
        "## 조사 범위와 완전성",
        f"국내 정본: 원천 수신 {received}건 → 제품특허 {product_patent_rows}건 → "
        f"고유 특허번호 {unique}건 → 상세 표시 {rendered}건",
    ]
    non_product = manifest.get("non_product_exclusions")
    if isinstance(non_product, int) and non_product:
        lines.append(
            f"기타특허 {non_product}건은 등재특허가 아니어서 정본 표에서 제외했습니다."
        )
    excluded = manifest.get("identifier_exclusions")
    if isinstance(excluded, int) and excluded:
        lines.append(f"특허번호가 없어 고유 특허 집계에서 제외한 원천 행 {excluded}건")
    if manifest.get("source_limit_reached") is True:
        source_limit = manifest.get("source_limit") or "미상"
        lines.append(
            f"국내 특허 조회가 상류 호출 상한 {source_limit}건에 도달해 전체 현황으로 단정할 수 없습니다."
        )
    return "\n".join(lines)


def _asks_for_expiry_forecast(evidence_set: EvidenceSet) -> bool:
    query = " ".join(evidence_set.query_spec)
    return any(
        signal in query
        for signal in ("만료 예정", "만료예정", "언제 만료", "만료일")
    )
