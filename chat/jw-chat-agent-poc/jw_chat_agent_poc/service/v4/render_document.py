from __future__ import annotations

from collections import Counter
import re
from typing import Final

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord, EvidenceSet, RenderNode
from jw_chat_agent_poc.service.v4.render_common import coverage_text, display, table, text


DOCUMENT_EXCERPT_LIMIT: Final = 320
DOCUMENT_BROKEN_TOKEN_MIN: Final = 40

_PAGE_TOKEN_RE: Final = re.compile(r"^\s*-?\s*\d{1,4}\s*-?\s*$")
_NUMBER_OR_SYMBOL_RE: Final = re.compile(r"^[\W\d_]+$", re.UNICODE)
_REPEATED_FOOTER_RE: Final = re.compile(
    r"^\s*[^|\n]{1,60}\|\s*(?:19|20)\d{2}[./-]\d{1,2}[./-]\d{1,2}\s*$"
)
_BROKEN_TOKEN_RE: Final = re.compile(
    rf"[0-9A-Za-z가-힣]{{{DOCUMENT_BROKEN_TOKEN_MIN},}}"
)

DOCUMENT_FIELDS: Final = ("document_name", "page", "section", "content")


def render_document(evidence_set: EvidenceSet) -> tuple[list[RenderNode], tuple[str, ...]]:
    if not evidence_set.records:
        return [], DOCUMENT_FIELDS

    content_counts = Counter(_content(record) for record in evidence_set.records)
    visible_records: list[EvidenceRecord] = []
    hidden_count = 0
    seen_content: set[str] = set()
    duplicate_groups: list[int] = []

    for record in evidence_set.records:
        content = _content(record)
        if _is_surface_noise(
            content,
            repetitions=content_counts[content],
            total_records=len(evidence_set.records),
        ):
            hidden_count += 1
            continue
        if content in seen_content:
            continue
        seen_content.add(content)
        visible_records.append(record)
        if content_counts[content] > 1:
            duplicate_groups.append(content_counts[content])

    rows = [
        (
            display(record.payload.get("document_name")),
            display(record.payload.get("page") or record.payload.get("page_number")),
            display(record.payload.get("section")),
            _excerpt(_content(record)),
        )
        for record in visible_records
    ]
    notices = _surface_notices(
        evidence_set.records,
        duplicate_groups=duplicate_groups,
        hidden_count=hidden_count,
    )
    all_ids = tuple(record.evidence_id for record in evidence_set.records)
    nodes = [
        RenderNode(
            block_id="document:coverage",
            surface_fields=(
                "total_reported",
                "records_received",
                "records_unique",
                "records_rendered",
            ),
            text="## 조사 범위와 완전성\n"
            + coverage_text(evidence_set.coverage, rendered=len(evidence_set.records)),
        )
    ]
    body = [f"본문 표시 {len(rows)}행 · 조회 상세 {len(evidence_set.records)}건"]
    body.extend(notices)
    rendered_table = table(("파일", "페이지/슬라이드", "절", "발췌"), rows)
    if rendered_table:
        body.append(rendered_table)
    nodes.append(
        RenderNode(
            block_id="document:records",
            record_ids=all_ids,
            surface_fields=DOCUMENT_FIELDS,
            text="## 업로드 문서 근거\n" + "\n".join(body),
        )
    )
    return nodes, DOCUMENT_FIELDS


def _content(record: EvidenceRecord) -> str:
    return text(record.payload.get("content"))


def _is_surface_noise(
    content: str,
    *,
    repetitions: int,
    total_records: int,
) -> bool:
    if not content:
        return True
    if _PAGE_TOKEN_RE.fullmatch(content) or _NUMBER_OR_SYMBOL_RE.fullmatch(content):
        return True
    return (
        repetitions >= 3
        and repetitions * 2 > total_records
        and _REPEATED_FOOTER_RE.fullmatch(content) is not None
    )


def _excerpt(content: str) -> str:
    if len(content) <= DOCUMENT_EXCERPT_LIMIT:
        return content
    return f"{content[:DOCUMENT_EXCERPT_LIMIT]}… (전문은 조회 상세)"


def _surface_notices(
    records: tuple[EvidenceRecord, ...],
    *,
    duplicate_groups: list[int],
    hidden_count: int,
) -> list[str]:
    notices = [f"동일 내용 {count}개 청크" for count in duplicate_groups]
    if hidden_count:
        notices.append(f"머리글/페이지번호 {hidden_count}건 제외")
    if any(_BROKEN_TOKEN_RE.search(_content(record)) for record in records):
        notices.append(
            "원문 추출 결과이며 슬라이드 도형 순서에 따라 문장이 이어질 수 있습니다."
        )
    return notices
