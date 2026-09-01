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


def render_policy(
    evidence_set: EvidenceSet,
    *,
    require_product_match: bool = False,
) -> tuple[list[RenderNode], tuple[str, ...]]:
    records = [record for record in evidence_set.records if _renderable_policy_record(record.payload)]
    if require_product_match:
        records = [record for record in records if _matches_requested_product(record.payload)]
    if not records:
        return [], POLICY_REQUIRED_FIELDS
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
        info_rows = tuple(
            (label, value)
            for label, value in (
                ("고시번호", text(payload.get("notice_number"))),
                ("시행일", effective_date(payload, raw)),
                ("제목", text(payload.get("title"))),
            )
            if value
        )
        record_nodes: list[RenderNode | None] = [
            (
                RenderNode(
                    block_id=f"{prefix}:info",
                    record_ids=(record.evidence_id,),
                    surface_fields=("notice_no", "effective_date"),
                    text=f"## 고시 정보\n{table(('항목', '값'), info_rows)}",
                )
                if info_rows
                else None
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
        ]
        nodes.extend(node for node in record_nodes if node is not None)
    if require_product_match and any(
        "[일반원칙] 고지혈증 치료제" in text(record.payload.get("raw_text"))
        for record in records
    ):
        nodes.append(
            RenderNode(
                block_id="policy:limits",
                text=(
                    "## 미확인 요소\n"
                    "세부 급여 인정 조건([일반원칙] 고지혈증 치료제)은 확인하지 못했습니다."
                ),
            )
        )
    return nodes, POLICY_REQUIRED_FIELDS


def _renderable_policy_record(payload: dict[str, object]) -> bool:
    status = text(payload.get("status")).casefold()
    if status in {
        "no_data",
        "empty",
        "error",
        "timeout",
        "deadline_exceeded",
        "quota",
        "upstream",
        "parse_error",
        "unsupported",
    }:
        return False
    return any(
        text(payload.get(field))
        for field in ("notice_number", "effective_date", "title", "raw_text")
    )


def _matches_requested_product(payload: dict[str, object]) -> bool:
    request = payload.get("request")
    requested_brand = text(request.get("brand")) if isinstance(request, dict) else ""
    if not requested_brand:
        return True

    requested_key = _product_key(requested_brand)
    candidate_values = [text(payload.get("brand_name"))]
    match_candidates = payload.get("match_candidates")
    if isinstance(match_candidates, (list, tuple)):
        candidate_values.extend(text(candidate) for candidate in match_candidates)
    return any(_product_key(candidate) == requested_key for candidate in candidate_values)


def _product_key(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value).removesuffix("정").casefold()


def _policy_node(
    prefix: str,
    field: str,
    heading: str,
    value: str | None,
    record_id: str,
) -> RenderNode | None:
    if not value:
        return None
    return RenderNode(
        block_id=f"{prefix}:{field}",
        record_ids=(record_id,),
        surface_fields=(field,),
        text=f"## {heading}\n{value}",
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
    normalized = re.sub(r"\s+(?=■\s*)", "\n", raw)
    for line in normalized.splitlines():
        next_key = next((key for key, pattern in markers if pattern.search(line)), None)
        if next_key:
            active = next_key
        elif line.lstrip().startswith("■"):
            active = None
        if active:
            found[active].append(line)
    return {key: "\n".join(lines).strip() for key, lines in found.items() if lines}
