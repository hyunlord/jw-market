from __future__ import annotations

from collections import defaultdict
from datetime import date

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord, EvidenceSet, RenderNode
from jw_chat_agent_poc.service.v4.render_common import coverage_text, display, link, table, text


PATENT_REQUIRED_FIELDS = (
    "patent_no",
    "invention_title",
    "listed_status",
    "expiration_date",
    "jurisdiction",
    "as_of_date",
)


def render_patent(
    evidence_set: EvidenceSet,
    observed_on: date,
) -> tuple[list[RenderNode], tuple[str, ...]]:
    by_lane: dict[str, list[EvidenceRecord]] = defaultdict(list)
    for record in evidence_set.records:
        by_lane[text(record.payload.get("lane"))].append(record)
    nodes: list[RenderNode] = [
        RenderNode(
            block_id="patent:coverage",
            surface_fields=("records_received", "records_unique", "records_rendered"),
            text="## 조사 범위와 완전성\n"
            + coverage_text(evidence_set.coverage, rendered=len(evidence_set.records)),
        )
    ]
    kr_rows = []
    for record in by_lane["kr_primary"]:
        payload = record.payload
        status = display(payload.get("status"))
        expiration = display(payload.get("expiration_date"))
        kr_rows.append(
            [
                display(payload.get("product")),
                display(payload.get("ingredient")),
                display(payload.get("patent_no")),
                display(payload.get("invention_title")),
                status,
                expiration,
                display(payload.get("owner")),
                (
                    f"{observed_on.isoformat()} 조회 기준 NeDrug 특허목록상 상태 "
                    f"'{status}' · 목록상 존속기간만료일 {expiration}"
                ),
            ]
        )
    nodes.append(
        RenderNode(
            block_id="patent:kr-primary",
            record_ids=tuple(record.evidence_id for record in by_lane["kr_primary"]),
            surface_fields=(
                (
                    "patent_no",
                    "invention_title",
                    "listed_status",
                    "expiration_date",
                    "jurisdiction",
                    "as_of_date",
                )
                if by_lane["kr_primary"]
                else ()
            ),
            text=(
                "## 국내 NeDrug 특허목록 정본\n"
                + table(
                    ("제품", "성분", "특허번호", "발명명", "목록상 상태", "목록상 존속기간만료일", "권리자", "판독"),
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
                "## 해석 상한\n특허목록상 존속기간만료일은 말소 처리일과 같은 필드가 아닙니다. "
                "국내 목록과 미국 Orange Book 날짜를 합산하지 않으며, 이 정보만으로 후발 제품 출시 가능성을 단정하지 않습니다."
            ),
        )
    )
    return nodes, PATENT_REQUIRED_FIELDS
