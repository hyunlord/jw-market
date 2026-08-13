from __future__ import annotations

from collections.abc import Mapping

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceSet, RenderNode
from jw_chat_agent_poc.service.v4.render_common import display, table


MARKET_REQUIRED_FIELDS = (
    "brand",
    "period",
    "sales_krw",
    "sales_delta_krw",
    "growth_pct",
    "market_share",
    "market_share_delta_pp",
)


def render_market(
    evidence_set: EvidenceSet,
) -> tuple[list[RenderNode], tuple[str, ...]]:
    records = tuple(evidence_set.records)
    if not records:
        return [], MARKET_REQUIRED_FIELDS
    columns = tuple(
        (field, header)
        for field, header in (
            ("brand", "브랜드"),
            ("period", "기간"),
            ("sales_krw", "값"),
            ("sales_delta_krw", "증감"),
            ("growth_pct", "증감률"),
            ("market_share", "점유율"),
            ("market_share_delta_pp", "점유율 증감"),
        )
        if field in {"brand", "period"}
        or any(_provided(record.payload, field) for record in records)
    )
    rows = tuple(
        tuple(_market_value(record.payload, field) for field, _header in columns)
        for record in records
    )
    return [
        RenderNode(
            block_id="market:records",
            record_ids=tuple(record.evidence_id for record in records),
            surface_fields=tuple(field for field, _header in columns),
            text="## 브랜드별 시장 지표\n" + table(
                tuple(header for _field, header in columns),
                rows,
            ),
        )
    ], MARKET_REQUIRED_FIELDS


def _provided(payload: Mapping[str, object], field: str) -> bool:
    return payload.get(field) not in (None, "", (), [])


def _market_value(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if value in (None, ""):
        return ""
    if field in {"growth_pct", "market_share"}:
        return f"{display(value)}%"
    if field == "market_share_delta_pp":
        return f"{display(value)}%p"
    return display(value)
