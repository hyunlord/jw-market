from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from jw_chat_agent_poc.hira_surface import (
    hira_dimension_display,
    hira_is_aggregate_row,
    hira_patient_value,
    hira_row_reconciliation,
)
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    EvidenceRecord,
    EvidenceSet,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.render_common import (
    coverage_text,
    display,
    link,
    table,
    text,
)
from jw_chat_agent_poc.service.v4.temporal_analysis import (
    nedrug_time_axis,
    normalize_surface_dates,
)

MARKET_REQUIRED_FIELDS = (
    "market_id",
    "market_name",
    "brand",
    "period",
    "sales_krw",
    "market_share",
    "rank",
    "growth_rate",
)


def render_market(
    evidence_set: EvidenceSet,
    *,
    period_count: int | None = None,
) -> tuple[list[RenderNode], tuple[str, ...]]:
    sales_records = tuple(
        record
        for record in evidence_set.records
        if record.payload.get("sales_krw") not in (None, "")
    )
    if not sales_records:
        return [], MARKET_REQUIRED_FIELDS
    all_record_ids = tuple(record.evidence_id for record in sales_records)
    sales_records, merged_count = _merge_duplicate_market_rows(sales_records)
    sales_records, sort_notice = _sort_market_records(sales_records)
    sales_records = _limit_market_periods(sales_records, period_count=period_count)
    projected = evidence_set.model_copy(update={"records": sales_records})
    rows = [
        (
            _market_label(record.payload),
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
        for record in sales_records
    ]
    nodes = _table_nodes(
        projected,
        block="market",
        heading="시장 데이터",
        headers=("시장", "브랜드", "기간", "매출(억원)", "점유율", "순위", "성장률"),
        rows=rows,
        fields=MARKET_REQUIRED_FIELDS,
    )
    for index, node in enumerate(nodes):
        if node.block_id == "market:records":
            merge_notice = (
                f"본문 중복 {merged_count}행을 병합했습니다. 조회 상세 원문은 보존했습니다."
                if merged_count
                else ""
            )
            nodes[index] = node.model_copy(
                update={
                    "record_ids": all_record_ids,
                    "text": "\n".join(
                        part for part in (node.text, sort_notice, merge_notice) if part
                    ),
                }
            )
    return nodes, MARKET_REQUIRED_FIELDS


def _limit_market_periods(
    records: Sequence[EvidenceRecord],
    *,
    period_count: int | None,
) -> tuple[EvidenceRecord, ...]:
    if period_count is None:
        return tuple(records)
    periods = tuple(
        dict.fromkeys(
            text(record.payload.get("period"))
            for record in records
            if text(record.payload.get("period"))
        )
    )
    allowed = frozenset(periods[:period_count])
    return tuple(
        record
        for record in records
        if text(record.payload.get("period")) in allowed
    )


def _merge_duplicate_market_rows(
    records: Sequence[EvidenceRecord],
) -> tuple[tuple[EvidenceRecord, ...], int]:
    grouped: dict[tuple[str, ...], list[EvidenceRecord]] = {}
    for record in records:
        payload = record.payload
        key = tuple(
            text(value).casefold()
            for value in (
                payload.get("market_id") or payload.get("market_name"),
                payload.get("brand"),
                payload.get("period"),
                payload.get("sales_krw"),
                payload.get("market_share"),
                _first(
                    payload,
                    "growth_rate",
                    "growth_pct",
                    "yoy_growth",
                    "yoy_growth_pct",
                ),
            )
        )
        grouped.setdefault(key, []).append(record)
    merged = tuple(
        min(
            candidates,
            key=lambda record: (
                0
                if _first(
                    record.payload,
                    "rank",
                    "market_rank",
                    "sales_rank",
                )
                not in (None, "")
                else 1,
                record.evidence_id,
            ),
        )
        for candidates in grouped.values()
    )
    return merged, len(records) - len(merged)


_PERIOD_RE = re.compile(r"^(?P<year>\d{4})(?:-(?:(?P<quarter>Q[1-4])|(?P<month>\d{1,2})))?$")


def _sort_market_records(
    records: Sequence[EvidenceRecord],
) -> tuple[tuple[EvidenceRecord, ...], str]:
    if any(text(record.payload.get("period")) for record in records):
        return (
            tuple(sorted(records, key=_period_display_key)),
            "표시 정렬: 기간 내림차순, 시장·브랜드 순.",
        )
    return (
        tuple(
            sorted(
                records,
                key=lambda record: (
                    -(_decimal(record.payload.get("sales_krw")) or Decimal("0")),
                    _market_label(record.payload).casefold(),
                    text(record.payload.get("brand")).casefold(),
                    record.evidence_id,
                ),
            )
        ),
        "표시 정렬: 매출 내림차순, 시장·브랜드 순.",
    )


def _period_display_key(record: EvidenceRecord) -> tuple[object, ...]:
    period = text(record.payload.get("period"))
    match = _PERIOD_RE.fullmatch(period)
    if match:
        year = int(match.group("year"))
        quarter = match.group("quarter")
        month = match.group("month")
        sequence = int(quarter[1:]) * 3 if quarter else int(month or 0)
        period_key = (0, -year, -sequence, period)
    else:
        period_key = (1, 0, 0, period)
    return (
        *period_key,
        _market_label(record.payload).casefold(),
        text(record.payload.get("brand")).casefold(),
        record.evidence_id,
    )


def _market_label(payload: Mapping[str, object]) -> str:
    market_id = text(payload.get("market_id"))
    market_name = text(payload.get("market_name"))
    if market_name and market_name != market_id:
        return f"{market_name} ({market_id})" if market_id else market_name
    if market_id.startswith("ml_"):
        return f"전략시장 {market_id}"
    if market_id.startswith("cd_"):
        return f"경쟁시장 {market_id}"
    return market_name or market_id or "원천 미제공"


def render_hira_statistics(
    evidence_set: EvidenceSet,
    *,
    question: str | None = None,
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
    query_text = question if question is not None else " ".join(evidence_set.query_spec)
    query_codes = {
        match.group(0)
        for match in re.finditer(r"\b[A-Z]\d{2}(?:\.\d+)?\b", query_text.upper())
    }
    if "I10" in query_codes and "고혈압" in query_text:
        query_codes.update(("I11", "I12", "I13", "I15"))
    scoped_records = tuple(
        record
        for record in evidence_set.records
        if not query_codes
        or any(
            text(_first(record.payload, "sickCd", "sick_code")).upper().startswith(code)
            for code in query_codes
        )
    )
    visible_records = tuple(
        record
        for record in scoped_records
        if any(
            _first(record.payload, *keys) is not None
            for keys in (
                ("period", "year", "month"),
                ("inpatOpat", "patient_type"),
                ("sex",),
                ("age",),
                ("patient_count", "ptntCnt", "value"),
                ("cost_krw",),
            )
        )
    )
    if scoped_records and not visible_records:
        visible_records = scoped_records
    excluded_count = len(scoped_records) - len(visible_records)
    aggregate_records = tuple(
        record for record in visible_records if hira_is_aggregate_row(record.payload)
    )
    detail_records = tuple(
        sorted(
            (
                record
                for record in visible_records
                if not hira_is_aggregate_row(record.payload)
            ),
            key=lambda record: (
                -(hira_patient_value(record.payload) or 0),
                record.evidence_id,
            ),
        )
    )
    query_text = query_text.casefold()
    requested_axes = {
        axis
        for axis, markers in (
            ("sex", ("성별", "남녀", "남성", "여성")),
            ("age", ("연령", "나이", "연령별")),
            ("patient_type", ("입원", "외래", "구분별")),
        )
        if any(marker in query_text for marker in markers)
    }
    if requested_axes:
        table_population = (*detail_records, *aggregate_records)
        row_limit = 160
    else:
        pure_axis_records = tuple(
            record
            for record in detail_records
            if sum(
                bool(_first(record.payload, *keys))
                for keys in (
                    ("inpatOpat", "patient_type"),
                    ("sex", "gender", "sexCd"),
                    ("age", "age_group", "ageCd"),
                )
            )
            <= 1
        )
        table_population = (
            aggregate_records
            if aggregate_records
            else pure_axis_records or detail_records
        )
        row_limit = 160
    table_records = _select_hira_table_records(table_population, row_limit)
    unknown_unit_count = sum(
        record.payload.get("rvdInsupBrdnAmt") not in (None, "")
        and record.payload.get("cost_krw") in (None, "")
        for record in table_records
    )
    implausible_count = sum(_implausible_hira_cost(record.payload) for record in visible_records)
    notices = []
    if requested_axes:
        rows = [
            (
                display(_first(record.payload, "sickCd", "sick_code")),
                display(_first(record.payload, "sickNm", "sick_name")),
                display(_first(record.payload, "period", "year", "month")),
                hira_dimension_display(record.payload, "inpatOpat", "patient_type"),
                hira_dimension_display(record.payload, "sex", "gender", "sexCd"),
                hira_dimension_display(record.payload, "age", "age_group", "ageCd"),
                _number(_first(record.payload, "patient_count", "ptntCnt", "value")),
                _number(record.payload.get("cost_krw")),
            )
            for record in table_records
        ]
    else:
        rows = _hira_total_rows(table_records)
    if aggregate_records and detail_records:
        notices.append(
            f"집계행 {len(aggregate_records)}건은 총계 요약에 사용하고 세부 표에서 분리했습니다."
        )
    source_row_count = max(
        len(table_population),
        int(evidence_set.coverage.records_unique or 0),
    )
    displayed_row_count = len(rows)
    if source_row_count > displayed_row_count:
        notices.append(
            f"전체 {source_row_count}건 중 {displayed_row_count}건 표시 · "
            "나머지는 조회 상세에서 확인할 수 있습니다."
        )
    elif requested_axes:
        notices.append(f"전체 {source_row_count}건 중 {displayed_row_count}건 표시했습니다.")
    reconciliation = hira_row_reconciliation(
        tuple(record.payload for record in visible_records)
    )
    if reconciliation.get("status") == "mismatch":
        notices.append(str(reconciliation.get("reason") or "집계행과 세부행 합계가 다릅니다."))
    if excluded_count:
        notices.append(f"표시 필드가 비어 있는 {excluded_count}행은 세부 표에서 제외했습니다.")
    if unknown_unit_count:
        notices.append(
            f"원천 단위 미상 {unknown_unit_count}행의 금액은 표시하지 않았습니다."
        )
    if implausible_count:
        notices.append(
            "검산 경고: 1인당 보험자부담금이 1,000원 미만 또는 10억원 초과인 "
            f"{implausible_count}행이 있습니다. 원천값은 수정하지 않았습니다."
        )
    cost_notice = _hira_cost_availability_notice(visible_records)
    projected = evidence_set.model_copy(update={"records": table_records})
    if table_records:
        nodes = _table_nodes(
            projected,
            block="hira-statistics",
            heading="환자수·비용",
            headers=(
                (
                    "상병코드",
                    "상병명",
                    "기간",
                    "구분",
                    "성별",
                    "연령",
                    "환자수",
                    "보험자부담금(원)",
                )
                if requested_axes
                else (
                    "상병코드",
                    "상병명",
                    "기간",
                    "구분",
                    "환자수",
                    "보험자부담금(원)",
                )
            ),
            rows=rows,
            fields=fields,
        )
    else:
        nodes = [
            RenderNode(
                block_id="hira-statistics:records",
                record_ids=tuple(record.evidence_id for record in evidence_set.records),
                surface_fields=fields,
                text="## 환자수·비용",
            )
        ]
    for index, node in enumerate(nodes):
        if node.block_id != "hira-statistics:records":
            continue
        node_text = node.text
        if cost_notice:
            heading, separator, table_text = node_text.partition("\n")
            node_text = (
                f"{heading}\n{cost_notice}\n{table_text}"
                if separator
                else node_text
            )
        nodes[index] = node.model_copy(
            update={
                "record_ids": tuple(
                    record.evidence_id for record in evidence_set.records
                ),
                "text": "\n".join((node_text, *notices)).strip(),
            }
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
    if nodes and requested_axes and truncated_gender_age:
        details = "\n".join(
            f"성별·연령 통계는 원천 {total}건 중 {received}건 표시했습니다."
            for total, received in truncated_gender_age
        )
        nodes[0] = nodes[0].model_copy(update={"text": f"{nodes[0].text}\n{details}"})
    return nodes, fields


def _hira_total_rows(records: Sequence[EvidenceRecord]) -> list[tuple[str, ...]]:
    patient_counts: dict[tuple[str, ...], Decimal] = {}
    costs: dict[tuple[str, ...], Decimal] = {}
    for record in records:
        payload = record.payload
        patient_count = _decimal(
            _first(payload, "patient_count", "ptntCnt", "value")
        )
        if patient_count is None:
            continue
        key = (
            display(_first(payload, "sickCd", "sick_code")),
            display(_first(payload, "sickNm", "sick_name")),
            display(_first(payload, "period", "year", "month")),
            hira_dimension_display(payload, "inpatOpat", "patient_type"),
        )
        patient_counts[key] = patient_counts.get(key, Decimal(0)) + patient_count
        cost = _decimal(payload.get("cost_krw"))
        if cost is not None:
            costs[key] = costs.get(key, Decimal(0)) + cost
    return [
        (
            *key,
            _number(patient_count),
            _number(costs.get(key)),
        )
        for key, patient_count in sorted(patient_counts.items())
    ]


def _select_hira_table_records(
    records: Sequence[EvidenceRecord],
    row_limit: int,
) -> tuple[EvidenceRecord, ...]:
    """Reserve one visible row per received disease code before value filling."""

    population = tuple(records)
    if len(population) <= row_limit:
        selected = population
    else:
        anchors: list[EvidenceRecord] = []
        seen_codes: set[str] = set()
        for record in population:
            code = text(_first(record.payload, "sickCd", "sick_code")).upper()
            if code and code not in seen_codes:
                seen_codes.add(code)
                anchors.append(record)
        selected_list = anchors[:row_limit]
        selected_ids = {record.evidence_id for record in selected_list}
        selected_list.extend(
            record
            for record in population
            if record.evidence_id not in selected_ids
        )
        selected = tuple(selected_list[:row_limit])
    return tuple(
        sorted(
            selected,
            key=lambda record: (
                -_period_sort_value(_first(record.payload, "period", "year", "month")),
                text(_first(record.payload, "sickCd", "sick_code")).upper(),
                -(hira_patient_value(record.payload) or 0),
                record.evidence_id,
            ),
        )
    )


def _period_sort_value(value: object) -> int:
    digits = re.sub(r"\D", "", text(value))
    return int(digits) if digits else 0


def _hira_cost_availability_notice(records: Sequence[EvidenceRecord]) -> str:
    by_period: dict[str, list[bool]] = {}
    for record in records:
        period = text(_first(record.payload, "period", "year", "month"))
        if not period:
            continue
        # Only normalized amounts are renderable. A raw HIRA amount without a
        # verified unit remains unavailable on the user-facing table.
        has_cost = record.payload.get("cost_krw") not in (None, "")
        by_period.setdefault(period, []).append(has_cost)
    available = sorted(period for period, flags in by_period.items() if any(flags))
    missing = sorted(period for period, flags in by_period.items() if not any(flags))
    if not available or not missing:
        return ""
    available_text = "·".join(available)
    missing_text = "·".join(missing)
    return (
        f"보험자부담금은 {available_text}년 기준만 원천 제공되며 "
        f"{missing_text}년 행은 '-'로 표시했습니다."
    )


def _implausible_hira_cost(payload: Mapping[str, object]) -> bool:
    patients = _decimal(_first(payload, "patient_count", "ptntCnt"))
    cost = _decimal(payload.get("cost_krw"))
    if patients is None or patients <= 0 or cost is None:
        return False
    per_patient = cost / patients
    return per_patient < Decimal("1000") or per_patient > Decimal("1000000000")


def render_nedrug(
    evidence_set: EvidenceSet,
    *,
    observed_on: date | None = None,
) -> tuple[list[RenderNode], tuple[str, ...]]:
    fields = ("item_name", "company", "active_ingredient", "approval_date")
    visible_records: list[EvidenceRecord] = []
    seen_display_keys: set[tuple[str, ...]] = set()
    for record in evidence_set.records:
        display_key = tuple(display(record.payload.get(field)) for field in fields)
        if display_key in seen_display_keys:
            continue
        seen_display_keys.add(display_key)
        visible_records.append(record)
    rows = [
        tuple(
            normalize_surface_dates(record.payload.get(field))
            if field == "approval_date"
            else display(record.payload.get(field))
            for field in fields
        )
        for record in visible_records
    ]
    visible_evidence_set = evidence_set.model_copy(
        update={"records": tuple(visible_records)}
    )
    nodes = _table_nodes(
        visible_evidence_set,
        block="nedrug",
        heading="의약품 허가 정보",
        headers=("품목명", "업체", "성분", "허가일"),
        rows=rows,
        fields=fields,
    )
    all_record_ids = tuple(record.evidence_id for record in evidence_set.records)
    rendered_nodes = [
        node.model_copy(update={"record_ids": all_record_ids})
        if node.block_id == "nedrug:records"
        else node
        for node in nodes
    ]
    rendered_nodes.append(
        _nedrug_time_axis_node(evidence_set, observed_on or datetime.now(UTC).date())
    )
    return rendered_nodes, fields


def _nedrug_time_axis_node(
    evidence_set: EvidenceSet,
    observed_on: date,
) -> RenderNode:
    aggregate_axis = next(
        (
            manifest.get("temporal_axis")
            for manifest in evidence_set.query_manifest
            if manifest.get("lane") == "surface_full_temporal"
            and manifest.get("source") == "nedrug"
        ),
        None,
    )
    axis = (
        aggregate_axis
        if isinstance(aggregate_axis, Mapping)
        else nedrug_time_axis(evidence_set.records, observed_on)
    )
    lines = ["## 의약품 허가·재심사 시간축", f"{observed_on.isoformat()} 기준"]
    for item in axis["approvals"][:5]:
        lines.append(
            f"{item['item_name']}: 허가 {item['approval_date']} "
            f"(경과 {item['elapsed_years']}년)"
        )
    for item in axis["reexaminations"][:5]:
        remaining = int(item["remaining_months"])
        remaining_text = (
            f"만료 후 {int(item.get('elapsed_months', 0))}개월 경과"
            if item.get("is_expired")
            else f"잔여 {remaining}개월"
        )
        lines.append(
            f"{item['item_name']}: 재심사 {item['reexam_end_date']} 만료 "
            f"({remaining_text})"
        )
    for item in axis["latest_changes"]:
        lines.append(f"{item['item_name']}: 최근 변경 {item['change_date']}")
    if axis["imprecise_date_count"]:
        lines.append(
            f"부분·불명확 날짜 {axis['imprecise_date_count']}건은 기간 계산에서 제외했습니다."
        )
    return RenderNode(
        block_id="nedrug:time-axis",
        record_ids=tuple(record.evidence_id for record in evidence_set.records),
        surface_fields=("approval_date", "REEXAM_DATE", "change_date"),
        text="\n".join(lines),
    )


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
