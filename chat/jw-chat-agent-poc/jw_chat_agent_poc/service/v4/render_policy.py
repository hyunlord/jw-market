from __future__ import annotations

from collections import defaultdict
import re

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceSet, RenderNode
from jw_chat_agent_poc.service.v4.render_common import (
    coverage_text,
    display,
    effective_date,
    table,
    text,
)


POLICY_REQUIRED_FIELDS = (
    "notice_no",
    "effective_date",
    "target",
    "exclusions",
    "administration_frequency",
)


def render_policy(evidence_set: EvidenceSet) -> tuple[list[RenderNode], tuple[str, ...]]:
    records = list(evidence_set.records)
    nodes: list[RenderNode] = [
        RenderNode(
            block_id="policy:coverage",
            surface_fields=("records_received", "records_unique", "records_rendered"),
            text="## 조사 범위와 완전성\n"
            + coverage_text(evidence_set.coverage, rendered=len(records)),
        )
    ]
    for index, record in enumerate(records, start=1):
        payload = record.payload
        raw = text(payload.get("raw_text"))
        sections = _policy_sections(raw)
        prefix = f"policy:{index}"
        info = table(
            ("항목", "값"),
            (
                ("고시번호", display(payload.get("notice_number"))),
                ("시행일", effective_date(payload, raw)),
                ("제목", display(payload.get("title"))),
            ),
        )
        nodes.extend(
            (
                RenderNode(
                    block_id=f"{prefix}:info",
                    record_ids=(record.evidence_id,),
                    surface_fields=("notice_no", "effective_date"),
                    text=f"## 고시 정보\n{info}",
                ),
                _policy_node(prefix, "target", "투여대상", sections.get("target"), record.evidence_id),
                _policy_node(prefix, "exclusions", "제외기준", sections.get("exclusions"), record.evidence_id),
                _policy_node(
                    prefix,
                    "administration_frequency",
                    "투여 방법 및 횟수",
                    sections.get("administration_frequency"),
                    record.evidence_id,
                ),
                _policy_node(
                    prefix,
                    "revision_reason",
                    "개정 사유",
                    sections.get("revision_reason"),
                    record.evidence_id,
                ),
                RenderNode(
                    block_id=f"{prefix}:raw",
                    record_ids=(record.evidence_id,),
                    surface_fields=("raw_text",),
                    text="## 공식 원문 전문\n" + (raw or "조회 실패"),
                ),
            )
        )
    return nodes, POLICY_REQUIRED_FIELDS
def _policy_node(
    prefix: str,
    field: str,
    heading: str,
    value: str | None,
    record_id: str,
) -> RenderNode:
    return RenderNode(
        block_id=f"{prefix}:{field}",
        record_ids=(record_id,),
        surface_fields=(field,),
        text=f"## {heading}\n{value or '자동 분류 실패 · 공식 원문 표시'}",
    )


def _policy_sections(raw: str) -> dict[str, str]:
    markers = (
        ("target", re.compile(r"투여\s*대상")),
        ("exclusions", re.compile(r"(?:제외\s*기준|투여\s*(?:중지|제외))")),
        ("administration_frequency", re.compile(r"(?:투여\s*(?:방법|횟수|용량|기간)|재투여\s*방법)")),
        ("revision_reason", re.compile(r"(?:고시\s*)?개정\s*사유")),
    )
    found: dict[str, list[str]] = defaultdict(list)
    active: str | None = None
    for line in raw.splitlines():
        next_key = next((key for key, pattern in markers if pattern.search(line)), None)
        if next_key:
            active = next_key
        if active:
            found[active].append(line)
    return {key: "\n".join(lines).strip() for key, lines in found.items() if lines}
