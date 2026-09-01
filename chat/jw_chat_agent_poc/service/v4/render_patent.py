from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from jw_chat_agent_poc.service.v4.lossless_contracts import (
    EvidenceRecord,
    EvidenceSet,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.patent import patent_record_sort_key
from jw_chat_agent_poc.service.v4.render_common import display, link, table, text
from jw_chat_agent_poc.service.v4.temporal_analysis import patent_time_axis

PATENT_REQUIRED_FIELDS = (
    "patent_no",
    "invention_title",
    "patent_type",
    "listed_status",
    "expiration_date",
    "jurisdiction",
    "as_of_date",
)
US_PATENT_REQUIRED_FIELDS = tuple(
    field for field in PATENT_REQUIRED_FIELDS if field != "patent_type"
)
MAX_DOMESTIC_PATENT_ROWS = 2_147_483_647  # Compatibility export; rendering is uncapped.


def render_patent(
    evidence_set: EvidenceSet,
    observed_on: date,
) -> tuple[list[RenderNode], tuple[str, ...]]:
    by_lane: dict[str, list[EvidenceRecord]] = defaultdict(list)
    for record in evidence_set.records:
        by_lane[text(record.payload.get("lane"))].append(record)
    all_kr_records = sorted(
        (
            record
            for record in by_lane["kr_primary"]
            if text(record.payload.get("page_group")) in ("", "제품특허")
        ),
        key=lambda record: patent_record_sort_key(record.payload),
    )
    kr_manifests = [
        item
        for item in evidence_set.query_manifest
        if text(item.get("lane")) == "kr_primary"
    ]
    kr_manifest = _merge_kr_manifests(kr_manifests)
    brand_scope_applied = any(
        record.payload.get("brand_scope_match") in {True, False}
        for record in all_kr_records
    )
    kr_records = [
        record
        for record in all_kr_records
        if not brand_scope_applied or record.payload.get("brand_scope_match") is True
    ]
    selected_kr = _fold_domestic_patent_records(kr_records)
    product_item_count = (
        len(
            {
                (
                    text(record.payload.get("patent_no")).casefold(),
                    text(record.payload.get("product_item_seq")).casefold(),
                )
                for record in kr_records
                if text(record.payload.get("patent_no"))
            }
        )
        if brand_scope_applied
        else _manifest_count(
            kr_manifest,
            "product_item_patent_count",
            fallback=len(kr_records),
        )
    )
    product_patent_number_count = (
        len({text(record.payload.get("patent_no")).casefold() for record in kr_records if text(record.payload.get("patent_no"))})
        if brand_scope_applied
        else _manifest_count(
            kr_manifest,
            "product_patent_number_count",
            fallback=sum(bool(text(group[0].payload.get("patent_no"))) for group in selected_kr),
        )
    )
    render_manifest = dict(kr_manifest)
    if brand_scope_applied:
        render_manifest.update(
            {
                "product_item_patent_count": product_item_count,
                "product_patent_number_count": product_patent_number_count,
                "product_patent_rows": product_item_count,
                "other_patent_rows": 0,
                "patent_type_counts": dict(
                    Counter(
                        text(record.payload.get("patent_type")) or "원천 미제공"
                        for record in kr_records
                    )
                ),
                "patent_type_denominator": product_item_count,
            }
        )
    received_population = _manifest_count(
        render_manifest,
        "records_received",
        fallback=evidence_set.coverage.records_received,
    )
    dual_population_caption = (
        f"수신 전체 {received_population}건 중 직접 관련 {product_item_count}건 · "
        "직접 관련 전건 제품특허"
    )
    nodes: list[RenderNode] = [
        RenderNode(
            block_id="patent:coverage",
            surface_fields=("records_received", "records_unique", "records_rendered"),
            text=_coverage_surface(
                evidence_set,
                rendered=len(selected_kr),
                manifest=render_manifest,
            ),
        )
    ]
    nodes.extend(
        _classification_nodes(
            product_records=kr_records,
            manifest=render_manifest,
        )
    )
    nodes.append(
        _time_axis_node(
            kr_records,
            observed_on,
            population_count=product_item_count,
            manifests=evidence_set.query_manifest,
            allow_full_population=not kr_manifests,
        )
    )
    kr_rows = []
    for records in selected_kr:
        payloads = tuple(record.payload for record in records)
        statuses = _group_values(payloads, "status")
        expirations = _group_values(payloads, "expiration_date")
        kr_rows.append(
            [
                _group_values(payloads, "product"),
                _item_sequence_cell(records),
                _group_values(payloads, "company"),
                _group_values(payloads, "ingredient"),
                _group_values(payloads, "composition", multi_value=True),
                _group_values(payloads, "patent_no"),
                _group_values(payloads, "invention_title"),
                _group_values(payloads, "patent_type"),
                _group_values(payloads, "page_group"),
                statuses,
                _group_pms_values(payloads, field="start"),
                _group_pms_values(payloads, field="end"),
                _group_pms_raw_values(payloads),
                expirations,
                _group_values(payloads, "owner", multi_value=True),
                (
                    f"{observed_on.isoformat()} 조회 기준 NeDrug 특허목록상 상태 "
                    f"'{statuses}' · 존속기간 만료일 {expirations}"
                ),
            ]
        )
    registered_count = sum(
        any(text(record.payload.get("status")) == "등록" for record in records)
        for records in selected_kr
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
            record_ids=tuple(record.evidence_id for record in kr_records),
            surface_fields=(
                (
                    "patent_no",
                    "product_item_seq",
                    "invention_title",
                    "patent_type",
                    "page_group",
                    "listed_status",
                    "company",
                    "composition",
                    "pms_period_start",
                    "pms_period_end",
                    "pms_period_raw",
                    "expiration_date",
                    "owner",
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
                + dual_population_caption
                + "\n"
                + _patent_table_caption(
                    product_item_count=product_item_count,
                    table_row_count=len(selected_kr),
                )
                + "\n"
                + table(
                    (
                        "제품",
                        "품목번호",
                        "업체명",
                        "성분",
                        "조성",
                        "특허번호",
                        "발명명",
                        "특허구분",
                        "구분",
                        "목록상 상태",
                        "재심사 시작일",
                        "재심사 종료일",
                        "재심사 기간 원문",
                        "존속기간 만료일",
                        "특허권자",
                        "판독",
                    ),
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
    required = tuple(
        dict.fromkeys(
            (
                *(PATENT_REQUIRED_FIELDS if kr_records else ()),
                *(US_PATENT_REQUIRED_FIELDS if by_lane["us_secondary"] else ()),
            )
        )
    )
    return nodes, required


def _coverage_surface(
    evidence_set: EvidenceSet,
    *,
    rendered: int,
    manifest: Mapping[str, Any],
) -> str:
    product_patent_rows = manifest.get("product_patent_rows", rendered)
    patent_number_count = manifest.get("product_patent_number_count", rendered)
    item_patent_count = manifest.get("product_item_patent_count", rendered)
    other_patent_rows = manifest.get(
        "other_patent_rows", manifest.get("non_product_exclusions", 0)
    )
    inspection_displayed = manifest.get("inspection_displayed_count")
    query_scope = (
        f"{len(evidence_set.query_spec)}개 질의 병합 기준"
        if len(evidence_set.query_spec) > 1
        else "현재 질의 기준"
    )
    lines = [
        "## 국내 특허 조회 범위",
        (
            f"직접 관련 제품특허 조합 {item_patent_count}건 · "
            f"특허번호 {patent_number_count}건 · 표 {rendered}행"
        ),
        (
            f"직접 관련 정본({query_scope}): 제품특허 {product_patent_rows}건 → "
            f"조합별 본문 {rendered}행"
        ),
        (
            f"계수 사다리: 직접 관련 제품특허 {item_patent_count}건 → "
            f"품목×특허 조합별 본문 {rendered}행"
            + (
                f" · 조회 상세 표시 {inspection_displayed}건"
                if isinstance(inspection_displayed, int)
                else " · 조회 상세 표시 건수는 조회 상세에서 별도 확인"
            )
        ),
        (
            f"제품특허 식별 기준: 특허번호 {patent_number_count}건 · "
            f"품목 기준 {item_patent_count}건"
        ),
    ]
    non_product = other_patent_rows
    if isinstance(non_product, int) and non_product:
        lines.append(
            f"기타특허 {non_product}건은 본문 표에서 제외하고 조회 상세에 보존했습니다."
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


def _classification_nodes(
    *,
    product_records: list[EvidenceRecord],
    manifest: Mapping[str, Any],
) -> list[RenderNode]:
    record_type_counts = dict(
        Counter(
            _patent_type_display(record.payload.get("patent_type"))
            for record in product_records
        )
    )
    manifest_type_counts = {
        _patent_type_display(label): count
        for label, count in _count_mapping(manifest.get("patent_type_counts")).items()
    }
    manifest_type_total = manifest.get("product_item_patent_count")
    if (
        isinstance(manifest_type_total, int)
        and manifest_type_total >= 0
        and sum(manifest_type_counts.values()) == manifest_type_total
    ):
        type_counts = manifest_type_counts
        type_total = manifest_type_total
    else:
        type_counts = record_type_counts
        type_total = len(product_records)
    nodes: list[RenderNode] = []
    provided_type_counts = {
        label: count
        for label, count in type_counts.items()
        if label != "원천 미제공"
    }
    if provided_type_counts and type_total:
        rows = [
            [label, str(count), _percentage(count, type_total)]
            for label, count in sorted(type_counts.items())
        ]
        nodes.append(
            RenderNode(
                block_id="patent:type-summary",
                surface_fields=("patent_type",),
                text=(
                    "## 직접 관련 제품특허 구분 분포\n"
                    f"직접 관련 제품특허 {type_total}건 기준 구분 분포\n"
                    + table(("구분", "건수", "비율"), rows)
                ),
            )
        )
    elif type_counts and type_total:
        nodes.append(
            RenderNode(
                block_id="patent:type-summary-unavailable",
                surface_fields=("patent_type",),
                text=(
                    "## 직접 관련 제품특허 구분 분포\n"
                    "직접 관련 제품특허에서 특허구분이 전건 원천 미제공이라 "
                    "근거 없는 구분 집계표를 만들지 않았습니다."
                ),
            )
        )
    pms_counts = dict(
        Counter(
            text(record.payload.get("pms_period_format")) or "invalid"
            for record in product_records
        )
    )
    invalid_pms = pms_counts.get("invalid", 0)
    if invalid_pms:
        nodes.append(
            RenderNode(
                block_id="patent:pms-format-notice",
                surface_fields=("pms_period_raw",),
                text=(
                    f"재심사 기간 {invalid_pms}건은 파싱 불가 형식이라 "
                    "시작일이나 종료일 한쪽을 임의 선택하지 않고 원문을 보존했습니다."
                ),
            )
        )
    return nodes


def _merge_kr_manifests(manifests: list[Mapping[str, Any]]) -> dict[str, Any]:
    unique_manifests: list[Mapping[str, Any]] = []
    counted_results: set[str] = set()
    for manifest in manifests:
        fingerprint = text(manifest.get("result_fingerprint"))
        if fingerprint and fingerprint in counted_results:
            continue
        if fingerprint:
            counted_results.add(fingerprint)
        unique_manifests.append(manifest)

    merged: dict[str, Any] = {}
    scalar_keys = (
        "records_received",
        "records_unique",
        "product_patent_rows",
        "other_patent_rows",
        "non_product_exclusions",
        "product_records_unique",
        "product_patent_number_count",
        "product_item_patent_count",
        "missing_item_seq_fallback_count",
        "patent_type_denominator",
        "identifier_exclusions",
    )
    for key in scalar_keys:
        values = [
            item.get(key)
            for item in unique_manifests
            if isinstance(item.get(key), int)
        ]
        if values:
            merged[key] = sum(values)
    canonical_products: dict[tuple[str, ...], dict[str, str]] = {}
    for manifest in unique_manifests:
        identities = manifest.get("canonical_product_identities")
        if not isinstance(identities, list):
            continue
        for identity in identities:
            if not isinstance(identity, Mapping):
                continue
            evidence_id = text(identity.get("evidence_id"))
            item_seq = text(identity.get("product_item_seq"))
            patent_no = text(identity.get("patent_no"))
            if patent_no:
                key = (
                    "patent",
                    patent_no.casefold(),
                    item_seq.casefold() or "__missing_item_seq__",
                )
            elif evidence_id:
                key = ("record", evidence_id)
            else:
                continue
            canonical_products.setdefault(
                key,
                {
                    "evidence_id": evidence_id,
                    "product_item_seq": item_seq,
                    "patent_no": patent_no,
                },
            )

    canonical_edges: dict[tuple[str, str], dict[str, str]] = {}
    for manifest in unique_manifests:
        edges = manifest.get("product_patent_edges")
        if not isinstance(edges, list):
            continue
        for edge in edges:
            if not isinstance(edge, Mapping):
                continue
            item_seq = text(edge.get("product_item_seq"))
            patent_no = text(edge.get("patent_no"))
            if not item_seq or not patent_no:
                continue
            key = (patent_no.casefold(), item_seq.casefold())
            canonical_edges.setdefault(
                key,
                {"product_item_seq": item_seq, "patent_no": patent_no},
            )
    if canonical_products:
        merged["canonical_product_identities"] = [
            canonical_products[key] for key in sorted(canonical_products)
        ]
        merged["product_records_unique"] = len(canonical_products)
        merged["product_item_patent_count"] = len(canonical_products)
        merged["product_patent_number_count"] = len(
            {
                identity["patent_no"].casefold()
                for identity in canonical_products.values()
                if identity["patent_no"]
            }
        )
    elif canonical_edges:
        merged["product_patent_edges"] = [
            canonical_edges[key] for key in sorted(canonical_edges)
        ]
        merged["product_records_unique"] = len(canonical_edges)
        merged["product_item_patent_count"] = len(canonical_edges)
        merged["product_patent_number_count"] = len(
            {patent_no for patent_no, _item_seq in canonical_edges}
        )
    inspection_counts = [
        item.get("inspection_displayed_count")
        for item in unique_manifests
        if isinstance(item.get("inspection_displayed_count"), int)
    ]
    if inspection_counts:
        merged["inspection_displayed_count"] = sum(inspection_counts)
    for key in (
        "page_group_counts",
        "patent_type_counts",
        "pms_period_format_counts",
    ):
        counts: dict[str, int] = defaultdict(int)
        for manifest in unique_manifests:
            for label, count in _count_mapping(manifest.get(key)).items():
                counts[label] += count
        if counts:
            merged[key] = dict(counts)
    merged["source_limit_reached"] = any(
        item.get("source_limit_reached") is True for item in unique_manifests
    )
    limits = [
        item.get("source_limit")
        for item in unique_manifests
        if isinstance(item.get("source_limit"), int)
    ]
    if limits:
        merged["source_limit"] = max(limits)
    return merged


def _count_mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(label): int(count)
        for label, count in value.items()
        if isinstance(count, int) and count >= 0
    }


def _manifest_count(
    manifest: Mapping[str, Any],
    key: str,
    *,
    fallback: int,
) -> int:
    value = manifest.get(key)
    return value if isinstance(value, int) and value >= 0 else fallback


def _fold_domestic_patent_records(
    records: list[EvidenceRecord],
) -> list[tuple[EvidenceRecord, ...]]:
    groups: dict[tuple[str, ...], list[EvidenceRecord]] = {}
    for record in records:
        patent_no = text(record.payload.get("patent_no"))
        item_seq = text(record.payload.get("product_item_seq"))
        key = (
            ("combination", patent_no.casefold(), item_seq.casefold())
            if patent_no and item_seq
            else ("record", record.evidence_id)
        )
        groups.setdefault(key, []).append(record)
    return [tuple(group) for group in groups.values()]


def _group_values(
    payloads: tuple[Mapping[str, Any], ...],
    field: str,
    *,
    multi_value: bool = False,
) -> str:
    values: list[str] = []
    for payload in payloads:
        value = (
            _patent_type_display(payload.get(field))
            if field == "patent_type"
            else _multi_value_display(payload.get(field))
            if multi_value
            else display(payload.get(field))
        )
        if value not in values:
            values.append(value)
    return "<br>".join(values) if values else "원천 미제공"


def _item_sequence_cell(
    records: tuple[EvidenceRecord, ...],
) -> str:
    values = tuple(
        dict.fromkeys(
            text(record.payload.get("product_item_seq")) or "원천 미제공"
            for record in records
        )
    )
    joined = "<br>".join(values)
    return f"품목 {len(values)}건: {joined}" if len(values) > 1 else joined


def _group_pms_values(
    payloads: tuple[Mapping[str, Any], ...],
    *,
    field: str,
) -> str:
    values: list[str] = []
    for payload in payloads:
        value = _pms_cell(
            payload,
            field=field,
            pms_format=text(payload.get("pms_period_format")),
        )
        if value not in values:
            values.append(value)
    return "<br>".join(values) if values else "원천 미제공"


def _group_pms_raw_values(payloads: tuple[Mapping[str, Any], ...]) -> str:
    values: list[str] = []
    for payload in payloads:
        value = _pms_raw_cell(
            payload,
            pms_format=text(payload.get("pms_period_format")),
        )
        if value not in values:
            values.append(value)
    return "<br>".join(values) if values else "원천 미제공"


def _patent_table_caption(
    *,
    product_item_count: int,
    table_row_count: int,
) -> str:
    return f"직접 관련 조합 {product_item_count}건 · 표 {table_row_count}행"


def _patent_type_inline(
    records: list[EvidenceRecord],
    *,
    manifest: Mapping[str, Any],
    expected_total: int,
) -> str:
    record_counts = Counter(
        _patent_type_display(record.payload.get("patent_type"))
        for record in records
    )
    manifest_counts = {
        _patent_type_display(label): count
        for label, count in _count_mapping(manifest.get("patent_type_counts")).items()
    }
    counts = (
        Counter(manifest_counts)
        if sum(manifest_counts.values()) == expected_total
        else record_counts
    )
    if not counts:
        return ""
    return " (" + " · ".join(
        f"{label} {count}건" for label, count in sorted(counts.items())
    ) + ")"


def _time_axis_node(
    records: list[EvidenceRecord],
    observed_on: date,
    *,
    population_count: int,
    manifests: tuple[Mapping[str, Any], ...] = (),
    allow_full_population: bool = False,
) -> RenderNode:
    direct_axis = patent_time_axis(records, observed_on)
    aggregate_axis = next(
        (
            manifest.get("temporal_axis")
            for manifest in manifests
            if manifest.get("lane") == "surface_full_temporal"
            and manifest.get("source") == "patent"
        ),
        None,
    )
    aggregate_population = (
        len(aggregate_axis.get("expirations", ()))
        + int(aggregate_axis.get("imprecise_date_count", 0))
        if isinstance(aggregate_axis, Mapping)
        else 0
    )
    use_aggregate = isinstance(aggregate_axis, Mapping) and (
        allow_full_population or aggregate_population <= population_count
    )
    axis = aggregate_axis if use_aggregate else direct_axis
    axis_population = aggregate_population if use_aggregate else population_count
    lines = [
        "## 국내 특허 만료 지형",
        f"직접 관련 제품특허 {axis_population}건 기준 · {observed_on.isoformat()} 기준",
    ]
    longest = axis["longest_expiration"]
    material = axis["material_expiration"]
    if longest:
        remaining = longest["remaining_months"]
        remaining_text = (
            f"만료 후 {int(longest.get('elapsed_months', 0))}개월 경과"
            if longest.get("is_expired")
            else f"잔여 {remaining}개월"
        )
        parts = []
        if material:
            material_remaining = int(material["remaining_months"])
            material_remaining_text = (
                f"만료 후 {int(material.get('elapsed_months', 0))}개월 경과"
                if material.get("is_expired")
                else f"잔여 {material_remaining}개월"
            )
            parts.append(
                f"물질특허 {material['expiration_date'][:7]} 만료 "
                f"({material_remaining_text})"
            )
        parts.append(
            f"현재 유효 {axis['active_count']}건 · 최장 "
            f"{longest['expiration_date'][:7]} ({remaining_text})"
        )
        lines.append(" · ".join(parts))
    if axis["imprecise_date_count"]:
        lines.append(
            f"판독 불능 또는 부분 만료일 {axis['imprecise_date_count']}건은 계산에서 제외했습니다."
        )
    return RenderNode(
        block_id="patent:time-axis",
        record_ids=tuple(record.evidence_id for record in records),
        surface_fields=("patent_no", "patent_type", "status", "expiration_date"),
        text="\n".join(lines),
    )


def _ordered_page_groups(counts: Mapping[str, int]) -> list[tuple[str, int]]:
    priority = {"제품특허": 0, "기타특허": 1, "원천 미제공": 2}
    return sorted(counts.items(), key=lambda item: (priority.get(item[0], 3), item[0]))


def _percentage(count: int, total: int) -> str:
    value = (Decimal(count) * Decimal(100) / Decimal(total)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    return f"{value}%"


def _multi_value_display(value: object) -> str:
    values = [part.strip() for part in text(value).split("|") if part.strip()]
    return "<br>".join(values) if values else "원천 미제공"


_PATENT_TYPE_TOKEN_RE = re.compile(r"물질\(염\)|물질|용도|기타")


def _patent_type_display(value: object) -> str:
    raw = text(value).strip()
    if not raw:
        return "원천 미제공"
    if re.search(r"[|,·]", raw):
        values = [part.strip() for part in re.split(r"[|,·]", raw) if part.strip()]
        return " · ".join(dict.fromkeys(values))

    normalized = re.sub(r"\s+", "", raw)
    tokens = _PATENT_TYPE_TOKEN_RE.findall(normalized)
    if len(tokens) >= 3 and "".join(tokens) == normalized:
        return " · ".join(dict.fromkeys(tokens))
    return raw


def _pms_cell(
    payload: Mapping[str, Any],
    *,
    field: str,
    pms_format: str,
) -> str:
    if pms_format == "invalid":
        return "파싱 불가"
    return display(payload.get(f"pms_period_{field}"))


def _pms_raw_cell(payload: Mapping[str, Any], *, pms_format: str) -> str:
    if pms_format == "unprovided":
        return "원천 미제공"
    return display(payload.get("pms_period_raw"))


def _asks_for_expiry_forecast(evidence_set: EvidenceSet) -> bool:
    query = " ".join(evidence_set.query_spec)
    return any(
        signal in query
        for signal in ("만료 예정", "만료예정", "언제 만료", "만료일")
    )
