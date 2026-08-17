from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord, EvidenceSet, RenderNode
from jw_chat_agent_poc.service.v4.render_common import coverage_text, display, link, table, text
from jw_chat_agent_poc.service.v4.render_document import render_document


MARKET_REQUIRED_FIELDS = (
    "brand",
    "period",
    "sales_krw",
    "market_share",
    "rank",
    "growth_rate",
)


def render_market(evidence_set: EvidenceSet) -> tuple[list[RenderNode], tuple[str, ...]]:
    rows = [
        (
            display(record.payload.get("brand")),
            display(record.payload.get("period")),
            _sales_eok(record.payload.get("sales_krw")),
            _percent(record.payload.get("market_share")),
            _rank(record.payload),
            _percent(
                _first(
                    record.payload,
                    "growth_rate",
                    "growth_pct",
                    "yoy_growth",
                    "yoy_growth_pct",
                )
            ),
        )
        for record in evidence_set.records
    ]
    return _table_nodes(
        evidence_set,
        block="market",
        heading="시장 데이터",
        headers=("브랜드", "기간", "매출(억원)", "점유율", "순위", "성장률"),
        rows=rows,
        fields=MARKET_REQUIRED_FIELDS,
    ), MARKET_REQUIRED_FIELDS


def render_hira_statistics(
    evidence_set: EvidenceSet,
) -> tuple[list[RenderNode], tuple[str, ...]]:
    fields = (
        "sickCd",
        "sickNm",
        "period",
        "inpatOpat",
        "sex",
        "age",
        "patient_count",
        "cost",
    )
    rows = [
        (
            display(_first(record.payload, "sickCd", "sick_code")),
            display(_first(record.payload, "sickNm", "sick_name")),
            display(_first(record.payload, "period", "year", "month")),
            display(_first(record.payload, "inpatOpat", "patient_type")),
            display(record.payload.get("sex")),
            display(record.payload.get("age")),
            _number(_first(record.payload, "patient_count", "ptntCnt", "value")),
            _number(
                _first(
                    record.payload,
                    "cost",
                    "cost_krw",
                    "amount",
                    "rvdInsupBrdnAmt",
                )
            ),
        )
        for record in evidence_set.records
    ]
    nodes = _table_nodes(
        evidence_set,
        block="hira-statistics",
        heading="환자수·비용",
        headers=("상병코드", "상병명", "기간", "구분", "성별", "연령", "환자수", "비용"),
        rows=rows,
        fields=fields,
    )
    truncated_gender_age = tuple(
        dict.fromkeys(
            (
                int(record.payload["_source_total_count"]),
                int(record.payload["_source_received_count"]),
            )
            for record in evidence_set.records
            if record.payload.get("_source_tool") == "hira_disease_gender_age_stats"
            and isinstance(record.payload.get("_source_total_count"), int)
            and isinstance(record.payload.get("_source_received_count"), int)
            and record.payload["_source_total_count"]
            > record.payload["_source_received_count"]
        )
    )
    if nodes and truncated_gender_age:
        details = "\n".join(
            f"성별·연령 통계는 원천 {total}건 중 {received}건 표시했습니다."
            for total, received in truncated_gender_age
        )
        nodes[0] = nodes[0].model_copy(update={"text": f"{nodes[0].text}\n{details}"})
    return nodes, fields


def render_nedrug(evidence_set: EvidenceSet) -> tuple[list[RenderNode], tuple[str, ...]]:
    fields = ("item_name", "company", "active_ingredient", "approval_date")
    rows = [
        tuple(display(record.payload.get(field)) for field in fields)
        for record in evidence_set.records
    ]
    return _table_nodes(
        evidence_set,
        block="nedrug",
        heading="의약품 허가 정보",
        headers=("품목명", "업체", "성분", "허가일"),
        rows=rows,
        fields=fields,
    ), fields


def render_web(evidence_set: EvidenceSet) -> tuple[list[RenderNode], tuple[str, ...]]:
    fields = ("title", "publisher", "published_at", "url")
    rows = [
        (
            link(record.payload),
            display(record.payload.get("publisher")),
            display(record.payload.get("published_at")),
            display(record.payload.get("summary")),
        )
        for record in evidence_set.records
    ]
    return _table_nodes(
        evidence_set,
        block="web",
        heading="공개 자료",
        headers=("제목", "매체", "일자", "요약"),
        rows=rows,
        fields=fields,
    ), fields


def render_openfda(evidence_set: EvidenceSet) -> tuple[list[RenderNode], tuple[str, ...]]:
    fields = ("product_name", "active_ingredient", "approval_date", "label_section")
    rows = [
        tuple(display(record.payload.get(field)) for field in fields)
        for record in evidence_set.records
    ]
    return _table_nodes(
        evidence_set,
        block="openfda",
        heading="미국 의약품 공개 정보",
        headers=("제품명", "성분", "기준일", "공개 내용"),
        rows=rows,
        fields=fields,
    ), fields


def _table_nodes(
    evidence_set: EvidenceSet,
    *,
    block: str,
    heading: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    fields: tuple[str, ...],
) -> list[RenderNode]:
    if not evidence_set.records:
        return []
    return [
        RenderNode(
            block_id=f"{block}:coverage",
            surface_fields=("total_reported", "records_received", "records_unique", "records_rendered"),
            text="## 조사 범위와 완전성\n"
            + coverage_text(evidence_set.coverage, rendered=len(evidence_set.records)),
        ),
        RenderNode(
            block_id=f"{block}:records",
            record_ids=tuple(record.evidence_id for record in evidence_set.records),
            surface_fields=fields,
            text=f"## {heading}\n{table(headers, rows)}",
        ),
    ]


def _first(payload: Mapping[str, object], *keys: str) -> object | None:
    return next((payload[key] for key in keys if payload.get(key) not in (None, "")), None)


def _decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("%", ""))
    except (InvalidOperation, ValueError):
        return None


def _sales_eok(value: object) -> str:
    number = _decimal(value)
    if number is None:
        return "원천 미제공"
    rendered = number / Decimal("100000000")
    return f"{rendered:,.2f}".rstrip("0").rstrip(".")


def _percent(value: object) -> str:
    number = _decimal(value)
    return "원천 미제공" if number is None else f"{number:,.2f}%".replace(".00%", "%")


def _number(value: object) -> str:
    number = _decimal(value)
    return "원천 미제공" if number is None else f"{number:,.0f}"


def _rank(payload: Mapping[str, object]) -> str:
    value = _first(payload, "rank", "market_rank", "sales_rank")
    return text(value) or "원천 미제공"
